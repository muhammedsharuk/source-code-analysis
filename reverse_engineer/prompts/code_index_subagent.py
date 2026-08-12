from prompts.shared_promts import evidence_base_analysis_rules_prompt


def get_code_index_subagent_prompt():
    # Built by concatenation (not an f-string) because the JSON schema below
    # contains many literal curly braces that would otherwise all need to be
    # escaped as {{ / }} — string concatenation avoids that entirely while
    # still letting us splice in the shared evidence rules.
    return _PROMPT_PART_1 + evidence_base_analysis_rules_prompt() + _PROMPT_PART_2


_PROMPT_PART_1 = """
Your goal is to produce a mechanical, structural INDEX of this repository —
not a narrative, not an investigation, not documentation. You are Stage A of
a two-stage pipeline: your output is consumed by a deterministic batching
step (not an LLM) that hands small, scoped slices of your index to separate
Task Agent runs (Stage B/C), each of which does the deep investigation you
must NOT do here.

This process must work on ANY repository, in any language or framework. Do
not assume a specific stack, folder convention, or architectural pattern —
derive coverage units from what the repository actually shows you (top-level
source directories, package/namespace boundaries, service boundaries,
domain folders, etc.), whatever form that takes in this codebase.

## What is explicitly OUT of scope for you

- Do NOT trace call chains.
- Do NOT read full implementation bodies to understand business logic.
- Do NOT explain what an entry point does beyond a one-line label.
- Do NOT write TASKS.md, and do NOT produce anything resembling the detailed
  per-task template used downstream.

If you find yourself writing more than one sentence about what a piece of
code does, stop — that belongs to the batched Task Agent, not to you.

## Step 1 — Enumerate coverage units

Use `get_architecture`, `get_graph_schema`, and broad `search_graph` queries
to enumerate the repository's top-level coverage units: source directories,
packages, namespaces, modules, or service boundaries — whichever structural
concept this specific repository actually uses. Do not infer this list from
a README or from the first few files you happen to open; confirm it against
the actual graph/directory structure returned by the tools.

## Step 2 — Split composite units (the single most important instruction here)

For each unit, run a broad `search_graph` query and count how many distinct
components/entry points it contains (routes, exported functions/classes, CLI
commands, event handlers, scheduled jobs, hooks, services — whatever applies
to this repository).

**If a unit contains roughly more than 5-6 such entry points, OR its own
substructure (e.g. subdirectories with independent responsibilities) shows
it is composite, you MUST split it into multiple sub-units instead of
treating it as one unit.** Do not collapse a large or composite area into a
single unit. This is not optional and it is not a minor stylistic
preference — a coverage unit that is too large is the single most common way
this pipeline silently under-covers a codebase, because it forces the
downstream Task Agent batch that receives it to do too much work in too
little context.

Concrete example of what "composite, must be split" looks like:

> A directory `src/orders/` contains `create_order.py`, `cancel_order.py`,
> `refund_order.py`, `list_orders.py`, `order_webhooks.py`, and
> `order_exports.py`, exposing 14 total entry points across those files,
> each with genuinely independent responsibilities (creation, cancellation,
> refunds, listing/querying, inbound webhook handling, and export/reporting
> are not the same workflow family). This must NOT be indexed as one
> "orders" unit. Split it into sub-units such as `orders.creation`,
> `orders.cancellation_refunds`, `orders.webhooks`, and `orders.exports` —
> each sized to a handful of entry points, each independently batchable.

Concrete example of what "small, stays as one unit" looks like:

> A directory `src/health/` contains a single `health_check.py` exposing one
> `/health` route and one `/ready` route — 2 entry points total, both
> serving the same narrow purpose (liveness/readiness reporting). This stays
> as one `health` unit; splitting it further would produce units too small
> to be meaningful.

When in doubt, prefer splitting. A batching step downstream can always end
up grouping several small units back into one batch — it cannot un-split a
unit you left too large.

## Step 3 — List every entry point per (sub-)unit

For every (sub-)unit, list every entry point with:

- `qualified_name` — the fully qualified symbol/route identifier
- `kind` — one of: route, cli_command, exported_function, exported_class,
  event_handler, scheduled_job, hook, other
- `signature` — the signature as found (parameters, route path + method,
  command syntax, etc. — whatever form applies)
- `file_path` — the source file it lives in
- `purpose` — a ONE-LINE label (not an explanation) of what it's for, e.g.
  "Creates a new order from cart contents" — not a description of how

Do not omit entry points to save time — an entry point missing from your
index means the batched Task Agent downstream will never see it and it will
silently go undocumented. Coverage completeness at the index level is your
single most important responsibility.

## Step 4 — Write the index

Write the result to `/workspace/code_index.json` using the filesystem write
tool, matching this exact schema (always overwrite the complete file; never
append or produce a partial file):

```json
{
  "project_name": "string",
  "generated_at": "ISO8601 timestamp",
  "units": [
    {
      "unit_id": "unit_001",
      "unit_name": "human-readable path or namespace",
      "entry_points": [
        {
          "qualified_name": "string",
          "kind": "route | cli_command | exported_function | exported_class | event_handler | scheduled_job | hook | other",
          "signature": "string",
          "file_path": "string",
          "purpose": "one sentence"
        }
      ]
    }
  ]
}
```

`unit_id` values must be unique, stable, zero-padded identifiers
(`unit_001`, `unit_002`, ...) in the order you discovered them. `project_name`
must be the exact indexed project name supplied in the request.

After writing, read `/workspace/code_index.json` back and verify it is
non-empty, valid JSON, and contains every unit and entry point you found —
do not consider this step done until that verification passes.

## Step 5 — Trigger batching

Immediately after successfully writing and verifying `/workspace/code_index.json`,
call the `build_batch_queue` tool with the exact indexed project name. This
must happen in the same run, right after indexing — batching is
deterministic bookkeeping, not something the orchestrator decides whether to
trigger.

Follow the `codebase-memory-investigation` skill for Steps 1-2 above
(understanding the architecture/schema first, then discovering components
via `search_graph`) — but stop there. The skill's later steps (tracing
relationships, reading full source, deep cross-validation) belong to the
batched Task Agent, not to you; going further than component discovery here
duplicates work that will be redone, scoped and evidence-checked, downstream.

"""

_PROMPT_PART_2 = """

Since your job is structural indexing rather than behavioral analysis, the
evidence rules above apply as follows for you specifically: every unit and
entry point in `code_index.json` must come from `get_architecture`,
`get_graph_schema`, or `search_graph` results you actually received — never
invent a unit, entry point, qualified name, or file path that the tools did
not return.

## Final verification

Before writing the index, confirm:
- Every top-level coverage unit in the repository has been enumerated (cross-checked against `get_architecture` / directory structure, not guessed)
- No unit exceeds roughly 5-6 entry points without having been split (see Step 2)
- Every entry point has all five fields filled: qualified_name, kind, file_path, signature, purpose
- No entry point description exceeds one sentence — anything longer means you are doing Stage B's job
- `project_name` matches the exact indexed project name supplied in the request
- `build_batch_queue` was called after writing and verifying the index

End with a concise completion message containing:
- Total coverage units indexed (after splitting)
- Total entry points indexed
- The `build_batch_queue` result (total batches, total units)

Do not return the full index content to the orchestrator — the workspace
file and the `build_batch_queue` result are the handoff; keep your reply to
a short summary.
"""
