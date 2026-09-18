"""3D force-directed graph layout — Barnes-Hut octree + ForceAtlas2-style local
optimization, ring-by-cluster-key + BFS-call-depth seeding.

A line-for-line Python port of codebase-memory-mcp's own `src/ui/layout3d.c` (the engine
behind its `GET /api/layout`), so `RunManager.compute_graph` can produce the exact same
kind of real physics-based 3D positions server-side, without depending on that project's
native C binary or its embedded-UI build.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

BH_THETA = 1.2
OCTREE_MAX_DEPTH = 26
OCTREE_MIN_HALF = 1e-4

LOCAL_REPULSION = 8.0
LOCAL_ATTRACTION = 1.0
LOCAL_ANCHOR_K = 0.25
LOCAL_ITERATIONS = 40
Z_DEPTH_SPACING = 50.0


class OctreeNode:
    __slots__ = (
        "ox", "oy", "oz", "half_size",
        "cx", "cy", "cz", "total_mass",
        "body_index", "body_mass", "children",
    )

    def __init__(self, ox: float, oy: float, oz: float, half: float):
        self.ox, self.oy, self.oz = ox, oy, oz
        self.half_size = half
        self.cx = self.cy = self.cz = 0.0
        self.total_mass = 0.0
        self.body_index = -1
        self.body_mass = 0.0
        self.children: List[Optional["OctreeNode"]] = [None] * 8


def _octant(n: OctreeNode, x: float, y: float, z: float) -> int:
    return (1 if x >= n.ox else 0) | (2 if y >= n.oy else 0) | (4 if z >= n.oz else 0)


def _child_center(n: OctreeNode, o: int) -> Tuple[float, float, float]:
    q = n.half_size * 0.5
    return (
        n.ox + (q if o & 1 else -q),
        n.oy + (q if o & 2 else -q),
        n.oz + (q if o & 4 else -q),
    )


def octree_insert(n: OctreeNode, idx: int, x: float, y: float, z: float, mass: float, depth: int = 0) -> None:
    if n.total_mass == 0.0 and n.body_index == -1:
        n.body_index, n.body_mass = idx, mass
        n.cx, n.cy, n.cz = x, y, z
        n.total_mass = mass
        return

    # OOM guard: once bodies (nearly) coincide, subdivision never separates
    # them and half_size shrinks toward zero forever. Stop and fold into an
    # aggregate mass-weighted centroid instead.
    if depth >= OCTREE_MAX_DEPTH or n.half_size < OCTREE_MIN_HALF:
        nm = n.total_mass + mass
        n.cx = (n.cx * n.total_mass + x * mass) / nm
        n.cy = (n.cy * n.total_mass + y * mass) / nm
        n.cz = (n.cz * n.total_mass + z * mass) / nm
        n.total_mass = nm
        n.body_index = -1
        return

    if n.body_index >= 0:
        oi, ox, oy, oz, om = n.body_index, n.cx, n.cy, n.cz, n.body_mass
        n.body_index = -1
        o = _octant(n, ox, oy, oz)
        if n.children[o] is None:
            cx, cy, cz = _child_center(n, o)
            n.children[o] = OctreeNode(cx, cy, cz, n.half_size * 0.5)
        octree_insert(n.children[o], oi, ox, oy, oz, om, depth + 1)

    nm = n.total_mass + mass
    n.cx = (n.cx * n.total_mass + x * mass) / nm
    n.cy = (n.cy * n.total_mass + y * mass) / nm
    n.cz = (n.cz * n.total_mass + z * mass) / nm
    n.total_mass = nm

    o = _octant(n, x, y, z)
    if n.children[o] is None:
        cx, cy, cz = _child_center(n, o)
        n.children[o] = OctreeNode(cx, cy, cz, n.half_size * 0.5)
    octree_insert(n.children[o], idx, x, y, z, mass, depth + 1)


def octree_repulse(
    n: Optional[OctreeNode], px: float, py: float, pz: float, mm: float, si: int, kr: float
) -> Tuple[float, float, float]:
    if n is None or n.total_mass == 0.0 or n.body_index == si:
        return 0.0, 0.0, 0.0

    dx, dy, dz = px - n.cx, py - n.cy, pz - n.cz
    d = math.sqrt(dx * dx + dy * dy + dz * dz)

    # Leaf, or far enough away relative to its size (Barnes-Hut theta test):
    # treat the whole subtree as one point mass at its center.
    if n.body_index >= 0 or (n.half_size * 2.0 / (d + 0.001)) < BH_THETA:
        if d < 0.01:
            d = 0.01
        f = kr * mm * n.total_mass / d
        return f * dx / d, f * dy / d, f * dz / d

    fx = fy = fz = 0.0
    for child in n.children:
        cfx, cfy, cfz = octree_repulse(child, px, py, pz, mm, si, kr)
        fx += cfx
        fy += cfy
        fz += cfz
    return fx, fy, fz


@dataclass
class Body:
    x: float
    y: float
    z: float
    ax: float  # anchor position (from ring layout)
    ay: float
    az: float
    mass: float = 1.0
    fx: float = 0.0
    fy: float = 0.0
    fz: float = 0.0


def local_optimize(bodies: List[Body], edges: Sequence[Tuple[int, int]], iterations: int = LOCAL_ITERATIONS) -> None:
    n = len(bodies)
    if n > 500_000:
        iterations = 10
    elif n > 100_000:
        iterations = 20

    for _ in range(iterations):
        for b in bodies:
            b.fx = b.fy = b.fz = 0.0

        mnx = mny = mnz = 1e9
        mxx = mxy = mxz = -1e9
        for b in bodies:
            mnx, mxx = min(mnx, b.x), max(mxx, b.x)
            mny, mxy = min(mny, b.y), max(mxy, b.y)
            mnz, mxz = min(mnz, b.z), max(mxz, b.z)
        half = max(mxx - mnx, mxy - mny, mxz - mnz) * 0.5 + 1.0

        # Repulsion via Barnes-Hut
        root = OctreeNode((mnx + mxx) * 0.5, (mny + mxy) * 0.5, (mnz + mxz) * 0.5, half)
        for i, b in enumerate(bodies):
            octree_insert(root, i, b.x, b.y, b.z, b.mass)
        for i, b in enumerate(bodies):
            fx, fy, fz = octree_repulse(root, b.x, b.y, b.z, b.mass, i, LOCAL_REPULSION)
            b.fx += fx
            b.fy += fy
            b.fz += fz

        # Attraction along edges (spring)
        for s, t in edges:
            bs, bt = bodies[s], bodies[t]
            dx, dy, dz = bt.x - bs.x, bt.y - bs.y, bt.z - bs.z
            bs.fx += dx * LOCAL_ATTRACTION
            bs.fy += dy * LOCAL_ATTRACTION
            bs.fz += dz * LOCAL_ATTRACTION
            bt.fx -= dx * LOCAL_ATTRACTION
            bt.fy -= dy * LOCAL_ATTRACTION
            bt.fz -= dz * LOCAL_ATTRACTION

        # Anchor spring: pull back toward the initial ring position
        for b in bodies:
            b.fx += (b.ax - b.x) * LOCAL_ANCHOR_K * b.mass
            b.fy += (b.ay - b.y) * LOCAL_ANCHOR_K * b.mass
            b.fz += (b.az - b.z) * LOCAL_ANCHOR_K * b.mass

        # Apply with capped displacement (max step length 8.0)
        for b in bodies:
            fm = math.sqrt(b.fx * b.fx + b.fy * b.fy + b.fz * b.fz)
            speed = 1.0
            if fm > 8.0:
                speed = 8.0 / (fm + 0.001)
            b.x += b.fx * speed
            b.y += b.fy * speed
            b.z += b.fz * speed


def _fnv1a(s: str) -> int:
    h = 2166136261
    for ch in s.encode("utf-8"):
        h ^= ch
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def _rand_float(seed: int) -> Tuple[float, int]:
    seed = (seed * 1103515245 + 12345) & 0xFFFFFFFF
    return (((seed >> 16) & 0x7FFF) / 32768.0 - 0.5), seed


def compute_call_depth(n: int, edges: Sequence[Tuple[int, int]], entry_indices: Sequence[int]) -> List[int]:
    """BFS depth from entry nodes (or in-degree-0 nodes if none given)."""
    depth = [-1] * n
    q: deque = deque()

    entries = list(entry_indices)
    if not entries:
        indeg = [0] * n
        for _, t in edges:
            indeg[t] += 1
        entries = [i for i in range(n) if indeg[i] == 0]

    for i in entries:
        depth[i] = 0
        q.append(i)

    adj: Dict[int, List[int]] = {}
    for s, t in edges:
        adj.setdefault(s, []).append(t)

    while q:
        c = q.popleft()
        for t in adj.get(c, []):
            if depth[t] == -1:
                depth[t] = depth[c] + 1
                q.append(t)

    return [d if d != -1 else 0 for d in depth]


def seed_ring_layout(
    cluster_keys: Sequence[str], qualified_names: Sequence[str], depths: Sequence[int]
) -> List[Body]:
    """Place each node on a ring keyed by its cluster (top-3 dir
    components), jittered by a per-node hash, with z from call depth."""
    bodies = []
    for ck, qn, d in zip(cluster_keys, qualified_names, depths):
        h = _fnv1a(ck)
        angle = ((h & 0xFFFF) / 65535.0) * 6.2832
        r = 500.0 + (((h >> 16) & 0xFF) / 255.0) * 250.0

        seed = _fnv1a(qn)
        jx, seed = _rand_float(seed)
        jy, seed = _rand_float(seed)
        jitter = 40.0

        px = r * math.cos(angle) + jx * jitter
        py = r * math.sin(angle) + jy * jitter
        pz = -float(d) * Z_DEPTH_SPACING

        bodies.append(Body(x=px, y=py, z=pz, ax=px, ay=py, az=pz))
    return bodies


def stellar_color(degree: int) -> str:
    if degree <= 1:
        return "#ff6050"
    if degree <= 3:
        return "#ff8855"
    if degree <= 5:
        return "#ffa060"
    if degree <= 8:
        return "#ffc070"
    if degree <= 12:
        return "#ffe080"
    if degree <= 18:
        return "#fff0c0"
    if degree <= 25:
        return "#fff8e8"
    if degree <= 35:
        return "#e8e8ff"
    if degree <= 50:
        return "#c0d0ff"
    return "#80a0ff"


def size_for_label(label: str) -> float:
    return {
        "Project": 20.0, "Package": 15.0, "Module": 15.0,
        "Folder": 12.0, "File": 8.0,
        "Class": 6.0, "Struct": 6.0, "Interface": 6.0,
        "Function": 4.0, "Method": 4.0,
    }.get(label, 4.0)


def cluster_key(file_path: str) -> str:
    """First 3 dir components of file_path (same rule as layout3d.c)."""
    slashes, buf = 0, []
    for ch in file_path:
        if ch == "/":
            slashes += 1
            if slashes >= 3:
                break
        buf.append(ch)
    return "".join(buf)
