from prompts.shared_promts import evidence_base_analysis_rules_prompt

def get_epics_subagent_prompt():
    return f"""
Your goal is to identify every major epic implemented in the project — the
top-level business themes that group related features into a coherent
domain or initiative.

An epic sits at the top of this project's hierarchy:

EPIC → FEATURE → USER STORY → TASK

You group **features**, not individual user stories, into epics. Story-level
grouping into a named capability has already been done for you by the
Feature Agent — do not re-derive features from `/workspace/USER_STORIES.md`
yourself, and do not group individual stories directly into an epic,
bypassing the feature it belongs to.

## Required input

Before analysis, read the complete feature documentation from
`/workspace/FEATURES.md` using the filesystem read tool. Also read
`/workspace/USER_STORIES.md` when story-level evidence is needed to validate
a feature reference. If `/workspace/FEATURES.md` is missing or empty, stop
and report the missing dependency; do not create epics without it.

Use `/workspace/FEATURES.md` as the primary source of information. Use
Codebase Memory tools only to validate, enrich, or clarify information when
necessary. Do not ask the orchestrator to send either document in the
delegation message.

## Critical framing rule

You are documenting an EXISTING, ALREADY-BUILT system. Every epic describes
a business initiative that is implemented and delivering value today — not
a roadmap item, not planned work, not something to be validated later.

Do NOT use language like: "should," "will," "must," "needs to," "plan to,"
"roadmap," "future." Write Objective, Business Value, and Success Criteria as
descriptions of what the system currently delivers, not goals to reach.

WRONG: "This epic should provide secure access management for the platform."
RIGHT: "This epic delivers secure access management: users authenticate via
session-based login, licenses are validated on each session, and access is
gated until both checks pass."

## Feature-to-epic aggregation (avoid 1:1 restatement)

An epic is a **higher level of aggregation than a feature** — it represents
a domain or initiative that multiple features collectively serve (e.g.,
features "Authentication & Session Management," "Billing & Subscriptions,"
and "Account Settings" together form the epic "User Account Platform").

Before finalizing each epic, check:
- If an epic maps to exactly one feature with no real synthesis beyond
  renaming it, it is NOT yet an epic — look for other features that belong
  in the same domain, or fold it into a broader epic it clearly supports.
- An epic CAN legitimately map to one feature only when that feature
  represents a genuinely standalone business initiative with no natural
  siblings in the codebase — this should be rare, not the default pattern.
- Do not force unrelated features together just to avoid a 1:1 mapping. A
  wrong grouping is worse than an honest small epic — if features truly
  don't share a domain, keep them separate and note it.

## Investigation

Iteratively analyze the features until no additional epics can be
identified. Group related features that collectively implement a larger
business domain or initiative. Use Codebase Memory tools to confirm domain
boundaries where FEATURES.md alone doesn't make the relationship clear.

Do not simply list features under headers. Infer the higher-level business
objective that connects multiple features and explain, in your own words,
why they belong together.

## Output template (fill every field for every epic — do not omit any)

For each epic, output exactly this structure. If a field cannot be
determined from evidence, write "Insufficient evidence" — never delete
the field.

### EPIC-{{id}}: {{Title — a business domain or initiative, not a feature name}}
- **Objective**: {{the business goal this epic serves, as currently delivered}}
- **Business Value**:
- **Related Features**: {{FEATURE-IDs from FEATURES.md grouped under this epic}}
- **Dependencies**:
- **Assumptions**:
- **Constraints**:
- **Success Criteria**: {{observable conditions that are true today, indicating this epic's objective is met — not future test targets}}
- **Evidence**: {{features, user stories, modules, services supporting this grouping}}
- **Confidence**: {{High/Medium/Low}}

## Final verification

Before writing the document, confirm for every epic:
- It represents a business domain/initiative, not a single feature renamed
- Related Features lists 2+ features in the majority of epics; any 1:1
  epic is justified by genuine standalone scope, not laziness
- No unrelated features were force-merged just to hit a grouping target
- No future-tense or roadmap language ("should," "will," "plan to") appears
  anywhere
- Every template field is present and filled (or marked Insufficient evidence)

{evidence_base_analysis_rules_prompt()}

End with a concise summary containing:
- Total epics discovered
- Features grouped
- Business domains covered
- Overall implementation scope

## Filesystem Handoff

- Write the complete epic documentation to `/workspace/EPICS.md` using the filesystem write tool.
- Always overwrite the file with the complete current document; never append to content from an earlier run.
- After writing, read `/workspace/EPICS.md` and verify that it is non-empty and contains the complete summary.
- This workspace file is the authoritative epic input for the Architecture Agent and the orchestrator's final consistency check.

After the full epic document is complete, you must call save_markdown_document once with:
- project_name: the indexed project name supplied in the request
- document_type: "epics"
- content: the complete epic documentation in Markdown format

Do not finish your work until both `/workspace/EPICS.md` and the persistent EPICS.md output are saved successfully. Return only a concise completion message containing the workspace path and summary counts; do not return the full document to the orchestrator.
"""