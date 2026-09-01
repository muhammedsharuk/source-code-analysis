"""Deterministic seeding and verification of the Code Index Agent's checklist.

The Code Index Agent tracks the areas it still needs to cover in
`/workspace/code_index_checklist.json`. Letting the agent decide the
checklist's own entry set turned out not to work: across real runs it
collapsed fine-grained areas into one broad ancestor entry (e.g. a single
`frontend/src` entry covering all 839 files, `covered_by` pointing at a unit
with 4 entry points) whenever that was the easiest way to satisfy the
per-entry rules asked of it. No amount of instructing the agent to keep the
checklist fine-grained held up, because the agent controlled the entry set
the rules were being checked against — collapse the entries and every rule
evaporates at once.

`seed_checklist` removes that discretion: it queries the real file tree
itself (via `search_graph(label="File")`, paginated) and mechanically
computes the checklist's entry set by recursively splitting the tree at
every point with more than one sufficiently-large sibling and collapsing
single-child chains and small subtrees, rather than trusting the agent to
decide where the boundaries are. The size threshold itself is not a fixed
constant tuned to one project — it is searched for so the resulting entry
count scales with the repo's real size, so the same mechanism behaves
sensibly on a 50-file repo and a 50,000-file monorepo. `verify_checklist_coverage`
then checks the agent's `covered_by`/`reason` claims against that fixed
entry set — every entry, including ones with narrower entries nested under
their own path, since `seed_checklist` only ever creates an entry when it
has real, distinct content its nested entries don't already account for (a
real run tried to close a broad entry using an entry point that actually
belonged to one of its own nested entries, which this also catches).
"""

import json
from pathlib import Path
from typing import Any

from utils.codebase_memory import CodebaseMemoryCLI
from utils.tool_safety import tool_safe
from utils.workspace import CHECKLIST_FILENAME, CODE_INDEX_FILENAME, resolve_workspace_dir

# A size-based "is this reason suspiciously large" threshold was tried and
# removed: an ancestor entry's own leftover content is, by construction,
# usually *below* whatever threshold seeded the checklist (that's exactly why
# it didn't qualify for its own separate entry) — so a size heuristic almost
# never fires on precisely the entries most likely to carry a false `reason`.
# A real run's false `reason` on such a leftover entry ("no indexed callable
# outside nested feature areas", closing a directory with 7 real, exported
# functions) is exactly what slipped through that heuristic. Re-checking the
# claim directly against the graph, below, replaces it with ground truth
# instead of a proxy.
_CANDIDATE_CALLABLE_LABELS = ("Function", "Method", "Class", "Route", "Interface")
_RECHECK_PAGE_SIZE = 100
_RECHECK_MAX_PAGES = 15


def _recheck_reason(
    cli: CodebaseMemoryCLI,
    project_name: str,
    path: str,
    narrower_paths: list[str],
) -> tuple[str, str] | None:
    """Independently check whether a path's own scope really has zero callable candidates.

    Tries each of a fixed set of common callable-bearing labels (safe even if
    a label doesn't exist in this project's schema — the tool just returns an
    empty result, not an error) and returns the first (label, file_path) found
    whose result genuinely falls in this entry's own scope — i.e. not also
    under one of `narrower_paths` (another checklist entry nested inside this
    one, which already accounts for that content) — or None if nothing
    qualifying turns up. For an entry with no narrower children this is one
    cheap bounded call per label; for one with children, results are paged
    (bounded by `_RECHECK_MAX_PAGES`) so a large sibling area's real content
    doesn't get mistaken for this entry's own. This costs nothing from the
    agent's own budget — these are calls made directly in Python, not agent
    tool calls.
    """
    own_scope_prefix = _own_scope_prefix(path)
    normalized_narrower = [_normalize(p) for p in narrower_paths]

    def is_own_scope(file_path: str) -> bool:
        if _is_test_path(file_path):
            return False
        normalized_fp = _normalize(file_path)
        if not normalized_fp.startswith(own_scope_prefix):
            return False
        return not any(normalized_fp == n or normalized_fp.startswith(n + "/") for n in normalized_narrower)

    for label in _CANDIDATE_CALLABLE_LABELS:
        offset = 0
        for _ in range(_RECHECK_MAX_PAGES):
            raw = cli.search_graph(
                project=project_name,
                label=label,
                file_pattern=_scope_file_pattern(path),
                limit=_RECHECK_PAGE_SIZE,
                offset=offset,
            )
            try:
                data = _unwrap_cli_result(raw)
            except ChecklistVerificationError:
                break
            for result in data.get("results", []):
                file_path = result.get("file_path", "")
                if is_own_scope(file_path):
                    return label, file_path
            if not data.get("has_more") or not normalized_narrower:
                # No children to exclude, so one page already answered the
                # question either way — no need to keep paging.
                break
            offset += _RECHECK_PAGE_SIZE
    return None


# Target band for the TOTAL number of checklist entries, regardless of repo
# size — this is what actually needs to hold across projects, not any one
# min_leaf_size value. Too few entries under-covers a big repo the same way a
# single broad entry did; too many re-creates the original budget-exhaustion
# failure (round 1 of this whole effort) from a different direction. The
# band's center is scaled modestly with repo size (bigger repos legitimately
# have more distinct areas) but clamped so neither a tiny repo nor a huge
# monorepo forces an unreasonable entry count.
_TARGET_ENTRIES_MIN = 15
_TARGET_ENTRIES_MAX = 60
_TARGET_ENTRIES_PER_FILE = 1 / 25
_MIN_LEAF_SIZE_SEARCH_ITERATIONS = 12

# A directory whose real (non-test) file count, direct or nested, is below
# this is folded into its parent's entry rather than seeded as its own
# checklist item. This is only the *default*/starting point for the search
# above — `seed_checklist` adjusts it per-project rather than using this
# value directly, so it stays reasonable whether the repo has 50 files or
# 50,000.
DEFAULT_MIN_LEAF_SIZE = 15

# Once a directory has at least one child clearing `min_leaf_size` (making
# that level "split-worthy"), any *other* child at that same level whose own
# subtree reaches this much smaller, fixed floor still gets its own entry
# too, rather than being folded into the parent's leftover bucket alongside
# genuinely tiny/loose content. This is deliberately NOT scaled with
# `min_leaf_size` the way `DEFAULT_MIN_LEAF_SIZE` is: `min_leaf_size` is
# searched per-repo to hit a total *entry-count* target (see
# `_find_min_leaf_size`), so on a monorepo with several large sibling
# services it lands well above the real size of smaller-but-still-distinct
# sibling services — those would otherwise vanish into the ancestor's
# leftover entry, indistinguishable from stray top-level files.
_MIN_SIBLING_ENTRY_SIZE = 5

# Common test-directory conventions across languages/frameworks, excluded
# from coverage the same way the skill excludes test packages from units.
# `androidtest`/`sharedtest` are Android's Gradle-mandated instrumented/shared
# test source sets (`src/androidTest`, `src/sharedTest`), distinct from the
# generic `test` set — a real run leaked an `androidTest` file into a parent
# entry's own scope because only `test`/`tests` were recognized, and a batch
# agent then covered that entry with an unrelated sibling file instead.
_TEST_PATH_SEGMENTS = {"test", "tests", "__tests__", "spec", "specs", "__mocks__", "androidtest", "sharedtest"}

# Common test-*file* conventions for stacks that colocate tests with source
# rather than using a separate test directory (e.g. Go's `_test.go`, several
# Java/Kotlin setups) — directory-segment matching alone misses these.
_TEST_FILENAME_SUFFIXES = (
    "_test.go", "_test.py", ".test.ts", ".test.tsx", ".test.js", ".test.jsx",
    ".spec.ts", ".spec.tsx", ".spec.js", ".spec.jsx", "test.java", "tests.java",
)

# Real repos can have many thousands of files; this bounds how many pages of
# File-labeled results seed_checklist will fetch before giving up and
# reporting what it found as (possibly) truncated, rather than looping
# indefinitely on an unexpectedly huge project.
_MAX_FILE_FETCH_PAGES = 100
_FILE_FETCH_PAGE_SIZE = 200


class ChecklistVerificationError(RuntimeError):
    """Raised when the checklist or code index cannot be read."""


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ChecklistVerificationError(f"Expected file not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ChecklistVerificationError(f"File is empty: {path}")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ChecklistVerificationError(f"File is not valid JSON: {path} ({exc})") from exc


def _normalize(value: str) -> str:
    return value.strip().strip("/").replace("\\", "/").lower()


def _is_root_path(path: str) -> bool:
    """True for the repository-root checklist entry (`seed_checklist` labels it "." )."""
    return _normalize(path) in ("", ".")


def _own_scope_prefix(path: str) -> str:
    """The prefix a real file_path must start with to even be considered under `path`.

    The root entry's own path normalizes to the literal string "." — but no
    real file_path starts with "." (confirmed against the real CLI: a root
    file like `README.md` and a nested one like `tauri/src-tauri/src/lib.rs`
    neither start with the character "."). Treating the root's own prefix as
    "" instead makes `file_path.startswith(prefix)` trivially true for every
    real file, which is correct — the root's real scope is everything, before
    `excluded_subpaths`/`narrower_paths` narrows it down to its own leftover
    content.
    """
    return "" if _is_root_path(path) else _normalize(path)


def _scope_file_pattern(path: str) -> str:
    """The `search_graph` file_pattern that actually matches everything under `path`.

    `f"{path}/**"` is right for a real subdirectory, but for the root entry
    it becomes the literal pattern `"./**"`, which was confirmed against the
    real CLI to match zero real files (no stored file_path has a `./` prefix)
    — silently hiding the root entry's real content (e.g. a `tauri/` or other
    top-level area too small to earn its own checklist entry) behind an
    always-empty query. A bare `"**"` is what actually matches everything.
    """
    return "**" if _is_root_path(path) else f"{path}/**"


def _is_ancestor(ancestor_path: str, other_path: str) -> bool:
    a, b = _normalize(ancestor_path), _normalize(other_path)
    if a == b:
        return False
    if _is_root_path(ancestor_path):
        # Every other real checklist path is nested under the repository
        # root, even though no real file_path literally starts with "." —
        # so this can't reuse the same string-prefix check used below.
        return bool(b)
    return b.startswith(a + "/")


def _is_test_path(file_path: str) -> bool:
    lowered = file_path.lower()
    if any(segment.lower() in _TEST_PATH_SEGMENTS for segment in file_path.split("/")):
        return True
    return lowered.endswith(_TEST_FILENAME_SUFFIXES)


def _unwrap_cli_result(raw: Any) -> dict[str, Any]:
    """CodebaseMemoryCLI wraps results in an MCP-style {"content": [{"text": "<json>"}]} envelope."""
    content = raw.get("content") if isinstance(raw, dict) else None
    if not content:
        raise ChecklistVerificationError(f"Unexpected search_graph response shape: {raw!r}")
    return json.loads(content[0]["text"])


def _fetch_all_file_paths(cli: CodebaseMemoryCLI, project_name: str) -> tuple[list[str], bool]:
    paths: list[str] = []
    offset = 0
    for _ in range(_MAX_FILE_FETCH_PAGES):
        raw = cli.search_graph(project=project_name, label="File", limit=_FILE_FETCH_PAGE_SIZE, offset=offset)
        data = _unwrap_cli_result(raw)
        paths.extend(r.get("file_path", "") for r in data.get("results", []))
        if not data.get("has_more"):
            return paths, False
        offset += _FILE_FETCH_PAGE_SIZE
    return paths, True


def _build_tree(file_paths: list[str]) -> dict[str, Any]:
    root: dict[str, Any] = {"files": 0, "children": {}}
    for file_path in file_paths:
        parts = file_path.split("/")[:-1]
        node = root
        for part in parts:
            node = node["children"].setdefault(part, {"files": 0, "children": {}})
        node["files"] += 1
    return root


def _subtree_total(node: dict[str, Any]) -> int:
    return node["files"] + sum(_subtree_total(c) for c in node["children"].values())


def _seed_recursive(node: dict[str, Any], prefix: str, min_leaf_size: int, out: list[tuple[str, int]]) -> None:
    children = node["children"]
    qualifying = {name: c for name, c in children.items() if _subtree_total(c) >= min_leaf_size}

    if len(qualifying) >= 1:
        # This level is "split-worthy": at least one child is large enough to
        # earn its own entry on `min_leaf_size` alone. A real run showed that
        # folding every *other* child into this node's own leftover bucket in
        # that case is wrong when one of those other children is itself a
        # distinct, non-trivial subtree (e.g. a monorepo with ten ~100-file
        # services plus two ~20-file services and a 1-file config folder,
        # all direct siblings) — `min_leaf_size` is searched to hit a
        # repo-wide *entry-count* target, so it lands well above what the
        # smaller-but-still-real services need, and they silently vanish into
        # a generic ancestor entry indistinguishable from stray top-level
        # files. Promoting any such sibling with real, non-trivial content of
        # its own gives it a real entry instead — but only above
        # `_MIN_SIBLING_ENTRY_SIZE`, so a genuinely tiny/loose sibling (a
        # single Dockerfile, an empty stub folder) still folds into the
        # parent rather than bloating the checklist with noise entries.
        promoted = {
            name: c
            for name, c in children.items()
            if name not in qualifying and _subtree_total(c) >= _MIN_SIBLING_ENTRY_SIZE
        }
        folded_total = sum(
            _subtree_total(c)
            for name, c in children.items()
            if name not in qualifying and name not in promoted
        )
        own_total = node["files"] + folded_total
        if own_total > 0:
            out.append((prefix or ".", own_total))
        for name, child in qualifying.items():
            _seed_recursive(child, f"{prefix}/{name}" if prefix else name, min_leaf_size, out)
        for name, child in promoted.items():
            _seed_recursive(child, f"{prefix}/{name}" if prefix else name, min_leaf_size, out)
        return

    total = _subtree_total(node)
    if total > 0:
        out.append((prefix or ".", total))


def _seed_at(tree: dict[str, Any], min_leaf_size: int) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    _seed_recursive(tree, "", max(1, min_leaf_size), out)
    return out


def _target_entry_band(total_files: int) -> tuple[int, int]:
    center = total_files * _TARGET_ENTRIES_PER_FILE
    center = min(_TARGET_ENTRIES_MAX, max(_TARGET_ENTRIES_MIN, center))
    return round(center * 0.7), round(center * 1.4)


def _find_min_leaf_size(tree: dict[str, Any], total_files: int) -> tuple[int, list[tuple[str, int]]]:
    """Search for a min_leaf_size that lands the seeded entry count in a repo-size-scaled band.

    A fixed threshold works for exactly the repo size it was tuned against —
    too small for a huge monorepo (hundreds of entries, re-creating the
    original per-run budget-exhaustion failure), too large for a small repo
    (collapses real areas back into one broad entry, the failure this whole
    mechanism exists to prevent). Entry count is not perfectly monotonic in
    `min_leaf_size` (an ancestor's own leftover can appear or disappear as
    thresholds cross it), so this is a bounded expand/bisect search that
    tracks the closest result seen rather than assuming a clean binary search.
    """
    target_min, target_max = _target_entry_band(total_files)
    target_center = (target_min + target_max) / 2

    low, high = 1, None
    candidate = max(1, round(total_files / max(target_center, 1)))
    best: tuple[int, list[tuple[str, int]]] | None = None

    for _ in range(_MIN_LEAF_SIZE_SEARCH_ITERATIONS):
        seeded = _seed_at(tree, candidate)
        count = len(seeded)

        if best is None or abs(count - target_center) < abs(len(best[1]) - target_center):
            best = (candidate, seeded)

        if target_min <= count <= target_max:
            return candidate, seeded

        if count > target_max:
            low = candidate
            candidate = candidate * 2 if high is None else max(candidate + 1, (candidate + high) // 2)
        else:
            high = candidate
            candidate = max(1, candidate // 2) if low == 1 and count < target_min else max(1, (low + candidate) // 2)

        if high is not None and candidate >= high and low < high - 1:
            candidate = (low + high) // 2
        if candidate < 1:
            candidate = 1

    return best


@tool_safe
def seed_checklist(
    project_name: str,
    min_leaf_size: int | None = None,
    force_rebuild: bool = False,
) -> dict[str, Any]:
    """Deterministically seed code_index_checklist.json's entry set from the real file tree.

    Fetches every indexed file's path (paginated `search_graph(label="File")`
    calls, excluding common test-directory/test-file conventions), then
    recursively splits the resulting tree wherever at least one sibling
    subtree is individually large enough to matter. Once a level splits this
    way, any *other* sibling at that same level still keeps its own entry as
    long as it clears a much smaller, fixed floor (`_MIN_SIBLING_ENTRY_SIZE`)
    — only siblings below that floor (stray top-level files, a single config
    folder) fold into the nearest qualifying ancestor. This matters because
    the main size threshold is searched for a repo-wide entry-count target,
    not per-branch: on a monorepo with several large services plus a couple
    of smaller-but-still-real ones, that threshold lands well above what the
    smaller services need, and without this sibling floor they would vanish
    into the ancestor's leftover entry indistinguishable from stray files.
    The main size threshold is not a fixed number either: by default it is
    searched for so the resulting entry count lands in a band that scales
    with the repo's real file count, so this behaves sensibly on a 50-file
    repo and a 50,000-file monorepo alike rather than being calibrated to
    whichever project it was last tuned against. The result is written to
    `/workspace/code_index_checklist.json` with every entry `"pending"` and
    a real `size_hint` already filled in — call this once, first, before any
    other checklist work, so the entry set itself is not something the agent
    can narrow to dodge the completion checks in `verify_checklist_coverage`.

    Args:
        project_name: The exact indexed project name.
        min_leaf_size: Minimum real file count for a subtree to become its
            own checklist entry rather than being folded into its parent.
            Leave unset (default) to have this searched for automatically
            based on the repo's real size; pass an explicit value only to
            override that search.
        force_rebuild: When True, regenerate and overwrite an existing
            checklist, discarding any progress already recorded in it. When
            False (default) and a checklist already exists, it is left
            untouched.

    Returns:
        {"total_entries": int, "created": bool, "truncated": bool, "min_leaf_size_used": int}
        `truncated` is True only if the file fetch hit its page-count safety
        limit before exhausting the project — in that case the seed may be
        incomplete and should be treated with suspicion on very large repos.
    """
    workspace_dir = resolve_workspace_dir(project_name)
    checklist_path = workspace_dir / CHECKLIST_FILENAME

    if checklist_path.exists() and not force_rebuild:
        existing = _read_json(checklist_path)
        return {
            "total_entries": len(existing.get("entries", [])),
            "created": False,
            "truncated": False,
            "min_leaf_size_used": existing.get("min_leaf_size_used"),
        }

    cli = CodebaseMemoryCLI()
    all_paths, truncated = _fetch_all_file_paths(cli, project_name)
    product_paths = [p for p in all_paths if p and not _is_test_path(p)]

    tree = _build_tree(product_paths)
    if min_leaf_size is not None:
        seeded = _seed_at(tree, min_leaf_size)
        min_leaf_size_used = min_leaf_size
    else:
        min_leaf_size_used, seeded = _find_min_leaf_size(tree, len(product_paths))

    entries = [{"path": path, "size_hint": total, "status": "pending"} for path, total in seeded]
    checklist = {
        "project_name": project_name,
        "min_leaf_size_used": min_leaf_size_used,
        "entries": entries,
    }

    checklist_path.parent.mkdir(parents=True, exist_ok=True)
    checklist_path.write_text(json.dumps(checklist, indent=2), encoding="utf-8")

    return {
        "total_entries": len(entries),
        "created": True,
        "truncated": truncated,
        "min_leaf_size_used": min_leaf_size_used,
    }


@tool_safe
def verify_checklist_coverage(project_name: str) -> dict[str, Any]:
    """Cross-check code_index_checklist.json's claimed coverage against the real code_index.json.

    A checklist entry marked "done" is only accepted as genuinely covered if
    it names `covered_by` unit_ids that exist in `code_index.json` and at
    least one of those units has an entry point whose `file_path` falls
    under the entry's own `path` and NOT also under a narrower checklist
    entry nested inside it — or the entry carries a `reason` that is
    independently re-checked against the real graph (not just trusted): this
    tool queries the entry's own path itself, excluding any narrower nested
    entries' scope, and flags the `reason` if that turns up anything real.
    Every checklist entry is checked, including ones that have narrower
    entries nested under their own path: `seed_checklist` only ever creates
    such an entry when it has real, distinct leftover content that its
    nested entries do not already account for (e.g. a `features/` directory
    with two large sub-features seeded separately still gets its own entry
    for a third, smaller sub-feature that didn't qualify for one of its
    own), so it is never "just bookkeeping" and always needs its own
    evidence too.

    Args:
        project_name: The exact indexed project name.

    Returns:
        {
          "all_clear": bool,
          "total_entries": int,
          "problems": [{"path": str, "issue": str, "detail": str}],
        }
        `issue` is one of: "not_marked_done", "no_evidence_and_no_reason",
        "unit_id_not_found", "unit_has_no_matching_entry_point",
        "reason_contradicted_by_query" (an independent re-check of this
        `reason`-only entry's own scope found a real callable it claims
        doesn't exist). An empty `problems` list with `all_clear: true`
        means every checklist
        entry is either backed by a real, matching unit or has a reason
        that held up — only call `build_batch_queue` once this is true.
    """
    workspace_dir = resolve_workspace_dir(project_name)
    checklist = _read_json(workspace_dir / CHECKLIST_FILENAME)
    index = _read_json(workspace_dir / CODE_INDEX_FILENAME)

    entries = checklist.get("entries", [])
    all_paths = [e.get("path", "") for e in entries]
    units_by_id = {unit.get("unit_id"): unit for unit in index.get("units", [])}
    problems: list[dict[str, str]] = []
    cli = CodebaseMemoryCLI()

    for entry in entries:
        path = entry.get("path", "")
        status = entry.get("status")
        covered_by = entry.get("covered_by") or []
        reason = entry.get("reason")
        own_scope_prefix = _own_scope_prefix(path)
        # A file under a narrower checklist entry nested inside this one has
        # already been claimed by that entry — it can't also satisfy this
        # entry's own (necessarily distinct) leftover content, or a broad
        # entry could borrow a child's real entry point to cover itself.
        narrower_paths = [p for p in all_paths if p != path and _is_ancestor(path, p)]

        if status != "done":
            problems.append({"path": path, "issue": "not_marked_done", "detail": f"status is {status!r}"})
            continue

        if not covered_by:
            if reason:
                found = _recheck_reason(cli, project_name, path, narrower_paths)
                if found is not None:
                    label, file_path = found
                    problems.append(
                        {
                            "path": path,
                            "issue": "reason_contradicted_by_query",
                            "detail": (
                                f"reason {reason!r} claims nothing callable, but a direct {label} "
                                f"query for this path's own scope found a real result at {file_path!r} — "
                                "this reason does not hold; investigate this path for real and use "
                                "covered_by instead"
                            ),
                        }
                    )
                continue
            problems.append(
                {
                    "path": path,
                    "issue": "no_evidence_and_no_reason",
                    "detail": "marked done with no covered_by unit_ids and no reason given",
                }
            )
            continue

        normalized_narrower_paths = [_normalize(p) for p in narrower_paths]

        def _is_own_scope(file_path: str) -> bool:
            normalized_fp = _normalize(file_path)
            if not normalized_fp.startswith(own_scope_prefix):
                return False
            return not any(
                normalized_fp == n or normalized_fp.startswith(n + "/") for n in normalized_narrower_paths
            )

        for unit_id in covered_by:
            unit = units_by_id.get(unit_id)
            if unit is None:
                problems.append(
                    {
                        "path": path,
                        "issue": "unit_id_not_found",
                        "detail": f"covered_by references {unit_id!r}, which does not exist in code_index.json",
                    }
                )
                continue
            has_match = any(_is_own_scope(ep.get("file_path", "")) for ep in unit.get("entry_points", []))
            if not has_match:
                problems.append(
                    {
                        "path": path,
                        "issue": "unit_has_no_matching_entry_point",
                        "detail": (
                            f"unit {unit_id!r} has no entry point whose file_path falls under "
                            f"{path!r} outside of this entry's own narrower checklist entries"
                        ),
                    }
                )

    return {
        "all_clear": not problems,
        "total_entries": len(entries),
        "problems": problems,
    }


CHECKLIST_TOOLS = [seed_checklist, verify_checklist_coverage]
