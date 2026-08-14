---
name: code-index-discovery
description: Use this skill only when performing structural repository indexing (Stage A of the reverse-engineering pipeline — whether run as a single Code Index Agent pass or as one batch of the split Index Batch Agent flow) — enumerating coverage units and their entry points via get_architecture, get_graph_schema, search_graph, and search_code, before any Task, User Story, Feature, Epic, or Architecture documentation stage. This is not for behavioral investigation, call-chain tracing, or writing narrative documentation.
---

# Code Index Discovery

## Purpose

Produce a complete structural index: every coverage unit, and every entry point within it. Completeness fails in two ways — stopping a paginated query after one page, and treating a summary/heuristic field as if it were the full, final answer. Both are avoidable by following the steps below.

---

## 1. Orient with `get_architecture` and `get_graph_schema` (once each)

`get_architecture` is a genuinely useful map — use it actively, not just skeptically:

- **`packages`** (with node counts) is a coarse, top-level cross-check — a package with real product code must end up with coverage units, and a package that is clearly test code (by name or by containing mostly test files) should be excluded from coverage units entirely, since you're indexing the implemented system, not its test suite.
- **`layers`** classifies each package by role (e.g. `api`, `core`, `entry`, `internal`) with a stated reason. Treat any package classified `api` or `entry` as confirmed entry-point-rich and prioritize thorough coverage there — this classification is a direct signal, not a guess, and applies equally to a backend API layer and a frontend layer that "has entry points."
- **`routes`** and **`entry_points`** are useful as seed/spot-check lists — everything they mention should surface somewhere in your final index — but neither is complete or authoritative on its own (route entries can be missing their handler name; the entry-points list is a small curated sample, not the full set). Use them to sanity-check your own enumeration, not to replace it.
- **`file_tree`** is a useful map for orientation, but it is not where your checklist comes from (see Section 5 — that's `seed_checklist`'s job) and its own `children` count is not reliable: a low or zero value can mean "not expanded within the tool's own output budget," not "verified empty."
- **`hotspots`** and **`boundaries`** give useful architectural context (what's central, how areas call into each other) but are not entry-point sources — don't spend enumeration effort on them.

`get_graph_schema` tells you which node labels and properties this project's graph actually exposes (this varies by stack — don't assume). It also reports an `is_entry_point` boolean property. Do not use it: it has been found unreliable in practice, including on confirmed real entry points. Identify entry points the way Step 3 describes instead.

---

## 2. Enumerate structure and entry points with `search_graph` (authoritative)

`search_graph` results are paginated (`limit`, `offset`, `has_more`). Always check `has_more`; if `true`, re-issue the query with `offset` advanced by `limit` and keep going until it's `false`. Never treat one page as a complete list or a real count — this is the single most common way a repository ends up under-covered.

- Use `label="Folder"` queries to confirm and complete the directory structure for each top-level area from Step 1.
- Use label queries scoped by `file_pattern`/`qn_pattern` per unit (`Route`, `Function`, `Method`, `Class`, `Module`, `Interface` — whichever labels this project's schema actually has) to enumerate entry-point candidates.
- Don't rely on the `fields` parameter to narrow a response or to unlock a specific property — expect the label's full property set back regardless of what you request.

---

## 3. Identify true entry points

In order of reliability:

1. A `Route`-labeled result, or a `Function`/`Method` with a populated `route_method`/`route_path`, is an entry point.
2. Otherwise, a result is a real entry-point candidate only if it's genuinely callable/usable from outside its own file — judge this from its `is_exported` property, its position relative to the module's public surface (e.g. a component wired into app routing, an exported hook, an exported service function), and, if still unclear, a targeted `search_code` check for external usage.

`is_exported` alone is not enough to pick *which* of several exported results in a directory are its real entry points — a directory can easily have a dozen exported symbols where only one or two are genuinely reachable from outside it, and the rest are internal building blocks that merely happen to be exported (a common pattern, not a bug in the codebase). A real run picked several deeply-nested, incidentally-exported sub-components as a feature's "entry points" while missing the feature's actual top-level file entirely — every one of the wrong picks had `is_exported: true`, so that property alone did not distinguish them. Before finalizing a directory's entry points:

- Check specifically for a file sitting at that directory's own root whose name matches or closely resembles the directory's own name (e.g. a `checkout/` directory with a `Checkout.tsx`/`checkout.py`/`index.ts` at its top level, not three folders deep). This pattern — a module's own root file being its real public entry point — is common across languages and frameworks, not specific to any one stack. If such a file exists, explicitly confirm or rule it out as an entry point before accepting any of its nested children as one instead — but confirm or rule it out with an actual query result, never by assuming it must be "just internal" without checking.
- Treat "I'm not sure which of these exported results is the real entry point" as the *default* state for a directory with more than one or two exported candidates, not an edge case — that is exactly when the `search_code` external-usage check in point 2 above is for, and it should be reached for actively, not only when something already feels ambiguous.

None of this is license to conclude a directory has no entry points at all. A real run, told to prefer root files and be skeptical of incidentally-exported nested ones, over-corrected into dismissing an entire multi-hundred-file directory as "internal components" — including its actual root page component, which was exported and genuinely callable the whole time. Being unsure *which* exported result is the real entry point is a reason to check more carefully (root file first, then `search_code` usage), never a reason to write none at all. If a directory's query for callables returns anything that qualifies as exported/callable under points 1-2 above, at least one of those results must become a real entry point in the index — picking the wrong one among several real candidates is a recoverable mistake; writing zero when real ones exist is not, because nothing downstream of this index will ever notice or add them back.

Apply this identically across every part of the stack. Backend routes, CLI commands, and frontend/UI entry points (exported page components, hooks, service functions) are all equally real and equally required — don't let one area's investigation style anchor how thoroughly you treat a differently-shaped area.

---

## 4. Expand composite entry points — the same "composite" judgment, one level down

A unit can be composite (see the main prompt's unit-splitting rule). An individual entry point can be composite too, in a way that's easy to miss: one externally-callable function or route that internally dispatches to several genuinely independent workflows, rather than implementing one. This is common wherever a single generic handler serves many distinct operations — a command-pattern dispatcher, a message-queue consumer routing on a message type, a single catch-all API/IPC endpoint that branches on an action field, or, in UI code, a top-level component or hook whose internal logic implements several unrelated user actions rather than one. This is not only a frontend concern — a backend can have just as few externally-visible routes as it has real workflows.

You cannot read the implementation to check this (still out of scope), but you don't need to — two structural signals, both available from properties `search_graph` already returns, are usually enough:

- **Complexity/size outlier.** If this project's schema reports `complexity`, `cognitive`, `lines`, or `out_degree` for the relevant label, and one entry point is a clear outlier against its sibling entry points in the same unit (multiple times higher, not marginally), that's a signal it may be doing more than one job.
- **Generic dispatcher shape.** A name like `dispatch`/`handle`/`process`/`route`/`execute`/`invoke`/`proxy` combined with a generic parameter (`request`/`action`/`command`/`event`/`payload`/`type`) rather than a specific business noun is a second, independent signal.

When either signal fires, confirm with exactly one targeted `trace_path` call (`direction="downstream"`, `depth=1`) on that entry point — this returns its immediate callees' qualified names, not their source, so it stays within your mechanical, structural mandate. If those callees are a family of independently-named handler functions (not a linear pipeline of shared utility calls — that's still one workflow), the entry point is confirmed composite: record each real callee as its own entry point in the same unit, using its actual qualified name from the trace result and a one-line purpose inferred from its name, with `kind: "dispatched_handler"`. Do this expansion before counting entry points for the unit-splitting decision — an expanded count is the real count; an entry point that hides ten workflows behind one callable boundary and gets counted as "one" is exactly as under-covering as missing an entry point outright.

---

## 5. Cover every area — the checklist's entry set comes from `seed_checklist`, not from you

Building the checklist by hand from `file_tree` didn't hold up: across real runs, whenever staying fine-grained meant more work, the entry set quietly collapsed into one broad area instead — the checklist agreed with itself that an area was "done" while the real index was silently incomplete. `seed_checklist` removes that discretion: before any investigation begins, it queries the real file tree itself and writes `/workspace/code_index_checklist.json` with a fixed set of entries, each carrying a real `size_hint`. You resolve that entry set; you don't get to decide what's in it. (Whether *you* personally call `seed_checklist`, or it was already called for you before you were ever dispatched, depends on this pipeline's current wiring — check your own prompt if you're unsure which applies to you; don't assume you must call it just because this skill describes it.)

Some entries have narrower entries nested under them in the same checklist (e.g. a broad `frontend/src` alongside `frontend/src/app`, `frontend/src/features`, ...). Do not treat a broad entry as bookkeeping you can skip — `seed_checklist` only ever creates one when it has real, distinct leftover content its nested entries don't already cover (e.g. a `features/` folder with two large sub-features seeded separately still gets its own entry for a third, smaller sub-feature too small to qualify for one of its own). Every entry, broad or narrow, needs its own real evidence, and a broad entry cannot borrow an entry point that actually belongs to one of its nested entries to satisfy itself — `verify_checklist_coverage` checks for exactly that:

1. Work every entry largest-`size_hint`-first, not in whatever order you notice them — if a run has to stop early, this determines whether what's left uncovered is large or small.
2. Only set an entry's `status` to `"done"` once you can point at real evidence: a `covered_by` list naming the specific `unit_id`(s) in `code_index.json` whose entry points come from that path — genuinely from that path's own scope, not from one of its nested entries. `reason` is only available when a `search_graph` query scoped to that path, using a label likely to hold callables (`Function`, `Method`, `Class`, or whatever labels this project's schema exposes), comes back with **zero** results that qualify as an entry point under Section 3 — never from skimming a file/directory listing. If that query returns anything qualifying, `reason` is not valid for this entry — pick at least one real entry point from what it returned and use `covered_by` instead, even if you're unsure it's the single best one. Both `covered_by` and `reason` are checked mechanically in step 3 — `verify_checklist_coverage` independently re-queries a `reason`-only entry's own scope itself, so a false `reason` is caught the same way a false `covered_by` is, not just by asking you to hold yourself to a standard.
3. Do not consider your share of the index finished by re-reading the checklist yourself. Genuine completion means `verify_checklist_coverage` reports `all_clear: true` for every entry you were responsible for, with every problem it reports resolved by actually investigating that area — never by editing the checklist to make the problem disappear. Whether you personally call that tool, or a coordinating step calls it after your work and hands back any unresolved paths for another pass, again depends on your own prompt — either way, your evidence has to hold up to it.

---

## 6. Sweep for registration/config-only functionality (`search_code`)

Middleware, DI/service registrations, scheduled-job config, migrations/triggers, and feature-flag branches are often wired in by registration rather than being called from outside their own file — a graph traversal from routes/functions won't surface them. For each unit, after Steps 3-4, run a targeted `search_code` sweep for this pattern (skip it where a unit's stack clearly has none of it, e.g. a small CLI script). Anything genuinely found is a real coverage item — record it with `kind: "other"`.

---

## `kind` mapping

| Evidence | `kind` |
|---|---|
| `Route`-labeled node, or a route method/path | `route` |
| Standalone exported function | `exported_function` |
| Method on a class | `method` |
| Exported class | `exported_class` |
| CLI command | `cli_command` |
| Event/message handler | `event_handler` |
| Scheduled/cron job | `scheduled_job` |
| Framework hook (lifecycle, React hook, etc.) | `hook` |
| Confirmed internal handler behind a composite/dispatcher entry point (Section 4) | `dispatched_handler` |
| Found only via the Step 6 sweep, or no clean match above | `other` |

---

## Final verification

- Every enumeration query was paged to `has_more: false`, not stopped after one page.
- Every checklist entry you were responsible for — including broad ones with narrower entries nested under them — was resolved with its own real evidence, never left unresolved on the assumption that its nested entries already cover it.
- Each entry you resolved carries either a real `covered_by` (backed by an entry point genuinely its own, not one belonging to a nested entry) or a `reason` that would survive an independent re-query of that path — never just the word `"done"` and never a `reason` written from a file listing without actually querying it. `verify_checklist_coverage` is what performs that independent re-query in this pipeline; your evidence has to hold up to it whether you call it yourself or a coordinating step does.
- Entries were worked in `size_hint` order, largest first, not in whatever order they happened to surface.
- For every directory with more than one or two exported candidates, its own root-level file (one matching or resembling the directory's own name) was explicitly checked as a possible entry point before any of its nested children were accepted as one instead — `is_exported: true` alone does not tell you which of several candidates is the real entry point.
- `is_entry_point` was not used as a signal anywhere.
- Entry points with a complexity/size outlier or a generic dispatcher name were checked with a one-hop downstream `trace_path` and expanded into `dispatched_handler` sub-entries where confirmed composite, before unit-splitting counts were finalized.
- Every unit was swept for registration/config-only functionality where plausible.
- The count of items you transcribed into `code_index.json` for each query matches what the tool actually returned — recount rather than assume.
