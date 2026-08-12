from prompts.shared_promts import evidence_base_analysis_rules_prompt

def get_user_stories_subagent_prompt():
    return f"""
Your goal is to identify every user story implemented in the project — a
small, user-facing action or interaction, close to the implementation tasks
that realize it, written the way a developer or QA engineer would describe
"what the user does and what happens" — not as a restatement of a task's
internal mechanics.

A user story is deliberately NOT a named capability, feature area, or
business initiative — grouping stories into that kind of higher-level
capability is a separate, later stage of this pipeline (the Feature Agent),
not your job. Keep every story close to the one or few tasks that implement
it; do not aggregate all the way up to something a product manager would
call a "feature."

## Required input

Before analysis, read the complete task documentation from `/workspace/TASKS.md`
using the filesystem read tool. If the file is missing or empty, stop and
report the missing dependency; do not create user stories without it.

Use `/workspace/TASKS.md` as the primary source of implementation knowledge.
Use Codebase Memory tools to validate, enrich, or fill in missing information.
Do not ask the orchestrator to send the task document in the delegation message.

## Critical framing rule

You are documenting an EXISTING, ALREADY-BUILT system. Every user story
describes an action that is implemented and working today — not a
proposed feature, not future work, not a test plan.

Do NOT use language like: "should," "will," "must support," "needs to
handle," "add tests," "verify that." Write Main Flow, Alternative Flow, and
Acceptance Criteria as descriptions of observed behavior — what the system
does when a user does X — not as requirements to be built or verified later.

WRONG: "The system should validate the user's email before allowing login."
RIGHT: "The system validates the user's email format and confirmation
status before allowing login; unconfirmed accounts are redirected to a
verification prompt."

## Task-to-story aggregation (stay close to the tasks)

Tasks in `/workspace/TASKS.md` are implementation-scoped workflows. A user
story sits just above a task: it reframes one task, or a small handful of
directly-related tasks, as a single user-facing action — "what the user
does, and what observably happens" — without pulling in every task that
merely shares a domain.

A story built from 1–2 tasks is the normal, expected case — do not treat
that as a sign the story is incomplete. Only combine multiple tasks into one
story when they are steps of the exact same user action (e.g., a "login
form" task and an "authenticate credentials" task that fire in the same
submit action can be one story: "User submits login credentials and is
authenticated"). Do not chain unrelated-but-adjacent tasks (e.g., login +
session-expiry handling + license refresh) into one broad story just
because they sit in the same area — that breadth belongs to the Feature
Agent, downstream, which groups multiple such stories into a named
capability (e.g., "Authentication & Session Management" as a Feature made
of several stories like this one).

Before finalizing each story, check:
- Does it describe one user-recognizable action with one outcome, not a
  whole area of functionality? If a story reads like it needs its own
  sub-list of distinct user actions to describe fully, split it into
  multiple stories instead.
- Is every task in `Related Tasks` actually part of firing this one action
  end-to-end, not just topically related to it?

## Investigation

Iteratively analyze the available information until no new user stories can
be identified. Correlate related tasks, routes, controllers, services,
modules, entities, and dependencies to determine which implementation pieces
fire together as one user action. Use Codebase Memory tools to confirm
groupings where TASKS.md alone doesn't make the relationship clear.

Do not simply summarize the architecture or restate the task list. Infer the
specific user action behind each task or small task group and explain the
implementation evidence supporting it.

## Output template (fill every field for every story — do not omit any)

For each user story, output exactly this structure. If a field cannot be
determined from evidence, write "Insufficient evidence" — never delete the
field.

### STORY-{{id}}: {{Title — one user-facing action, not a task name or a broad capability}}
- **Story Statement**: As a {{role}}, I want {{action}}, so that {{immediate outcome}}.
- **Objective / Outcome**:
- **Preconditions**:
- **Trigger**:
- **Main Flow Summary**: {{what happens, in present tense, as implemented}}
- **Alternative / Edge Flow Summary**:
- **Error / Exception Considerations**:
- **Dependencies**:
- **Assumptions**:
- **Acceptance Criteria**: {{observable conditions that are true today when this story's flow completes — not future test cases}}
- **Related Tasks**: {{TASK-IDs from TASKS.md that compose this story}}
- **Evidence**: {{tasks, routes, modules, services, classes}}
- **Confidence**: {{High/Medium/Low}}

## Final verification

Before writing the document, confirm for every story:
- It describes one user-facing action with one outcome, not a task's
  internal mechanics restated, and not a broad capability/feature area
  (see aggregation rule above)
- Every task listed in `Related Tasks` genuinely fires as part of this one
  action, not merely related by topic or module
- No future-tense or QA language ("should," "must," "will," "add tests")
  appears anywhere
- Every template field is present and filled (or marked Insufficient evidence)

{evidence_base_analysis_rules_prompt()}

End with a concise summary containing:
- Total user stories discovered
- Distinct user-facing actions covered
- Modules involved
- Areas requiring further investigation

## Filesystem Handoff

- Write the complete user story documentation to `/workspace/USER_STORIES.md` using the filesystem write tool.
- Always overwrite the file with the complete current document; never append to content from an earlier run.
- After writing, read `/workspace/USER_STORIES.md` and verify that it is non-empty and contains the complete summary.
- This workspace file is the authoritative handoff to the Feature Agent and Architecture Agent.

After the full user story document is complete, you must call save_markdown_document once with:
- project_name: the indexed project name supplied in the request
- document_type: "user_stories"
- content: the complete user story documentation in Markdown format

Do not finish your work until both `/workspace/USER_STORIES.md` and the persistent USER_STORIES.md output are saved successfully. Return only a concise completion message containing the workspace path and summary counts; do not return the full document to the orchestrator.
"""