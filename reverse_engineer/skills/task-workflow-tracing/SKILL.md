---
name: task-workflow-tracing
description: Use this skill only when performing deep, per-workflow investigation for the Task Agent (Stage C of the reverse-engineering pipeline) — tracing one entry point at a time, from the payload get_batch_details already handed you, through to its terminal effect via trace_path and source reading. This is not for structural repository indexing (see code-index-discovery) and not for aggregating already-generated documentation (User Story/Feature/Epic stages read TASKS.md/USER_STORIES.md/FEATURES.md directly, not this skill).
---

# Task Workflow Tracing

## Purpose

Produce evidence for one workflow at a time: the path from an entry point you were already handed by `get_batch_details`, through every internal call required to fulfill it, to its terminal effect(s). Unlike Stage A's structural indexing — which stops at one shallow hop by design — deep, multi-hop tracing and full source reading are your primary method here, not something to avoid or minimize.

## Method

1. **Trace first.** Call `trace_path` on the entry point, deep enough to actually reach a terminal effect — a database read/write, an external API/service call, a rendered response, a file write, an emitted event — not stopped at the first hop. This gives you the real call structure to read against, not a guess.
2. **Read source as your primary method, not a fallback.** `trace_path` shows you *what* calls *what* — it does not show branching, validation, error handling, or side effects. Read the actual implementation with `get_code_snippet`/`search_code` for every task whose behavior isn't already fully evident from the trace. Reach for this proactively, not only after graph tools "fail" — for this stage, graph tools alone are never sufficient by design.

   `get_code_snippet`/`search_code` are the ONLY tools that can reach this repository's real source — they run against the indexed codebase directly, wherever it actually lives on disk. Any generic filesystem tool you may also have access to (a plain read/write/ls/edit tool) is scoped only to this pipeline's own `/workspace/` output directory and `/skills/` — it has no access whatsoever to the repository being analyzed, and calling it with a source file's path (relative or absolute) will always fail with a path error, never partially succeed. If `get_code_snippet` returns nothing useful for a given entry point — this happens for declarative/config entries (e.g. an XML resource element) that aren't a real code symbol — do not try another way to open the file; treat the entry point's existing `signature`/`purpose` metadata plus whatever `search_code` turns up as the evidence ceiling for that entry, and write "Insufficient evidence" for anything beyond that rather than attempting unsupported file access.
3. **Cross-validate when sources disagree.** If the graph's structure and the source code's actual behavior tell different stories, the source code wins — it's ground truth; the graph is a navigation aid.
4. **Widen scope only when your assigned batch genuinely requires it** (e.g. confirming a shared dependency's behavior) — use `get_graph_schema`/`search_graph` for that, sparingly. You were already handed your scope; you're not re-discovering repository structure.

## Evidence discipline

- Never invent, assume, or speculate about behavior — every claim needs a specific trace result, code snippet, or search result behind it.
- Do not infer implementation behavior from a name alone (a function called `validateEmail` might not use regex, might not check MX records — read it to find out).
- If evidence genuinely isn't available after investigating, say "Insufficient evidence" — never fill the gap with a plausible guess.
- It is better to document fewer, verified workflows than to pad with invented detail.

## Final verification

Before writing up a task, confirm:
- It was traced end-to-end via `trace_path`, not documented from its signature or graph edges alone.
- Every non-obvious behavior claim (branching, validation, error handling, side effects) is backed by an actual source read, not inferred from the entry point's name or purpose label.
- Where the graph and the source disagreed, the source's account is what you documented.
- Nothing is asserted beyond what you actually investigated — gaps are marked "Insufficient evidence," not guessed.
