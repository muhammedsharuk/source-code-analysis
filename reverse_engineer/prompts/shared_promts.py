def evidence_base_analysis_rules_prompt():
    return """
## Evidence-Based Analysis Rules

Your analysis must be entirely evidence-driven.

- Never invent, assume, or speculate about functionality.
- Every statement must be supported by evidence from the source code, Codebase Memory tools, or the input documentation.
- If sufficient evidence is not available, explicitly state "Insufficient evidence" instead of making assumptions.
- Do not infer *implementation behavior* from naming conventions alone (e.g., do not assume a function named `validateEmail` uses regex, checks MX records, or has any specific logic — read the code to confirm behavior).
- Business/intent fields are the one exception: for fields that ask for business purpose, objective, or value (not implementation behavior), naming, UI copy, route paths, and module organization are legitimate evidence. State the field, cite what you based it on (e.g., "inferred from route path /billing/upgrade and component name UpgradePrompt"), and mark Confidence Medium or Low rather than High — never mark it "Insufficient evidence" solely because the source is naming rather than logic.
- Do not assume standard framework behavior unless it is explicitly implemented or referenced in the code.
- Read the relevant source code whenever graph information is incomplete, ambiguous, or contradictory.
- Cross-check information using multiple sources whenever possible (graph, call traces, code snippets, architecture, code search).
- If two sources disagree, prefer the source code over graph metadata.
- Distinguish clearly between observed facts and inferred conclusions.
- Every inferred conclusion must reference the evidence that supports it.
- Do not create Tasks, User Stories, Features, Epics, APIs, Entities, Modules, Validation Rules, Workflows, or Dependencies unless there is supporting evidence.
- If confidence is Low due to limited evidence, explain why.
- It is acceptable to return fewer results rather than invent missing information.
- Completeness must never come at the cost of correctness.

## Present-Tense, As-Built Framing

You are documenting an EXISTING, ALREADY-BUILT system, never a plan, proposal,
or roadmap. Every statement describes what the system does today.

- Do NOT use future/prescriptive language: "should," "will," "must,"
  "needs to," "plan to," "roadmap," "ensure," "validate that," "add tests,"
  "verify that X works."
- Fields like Acceptance Criteria, Success Criteria, or Done Condition
  describe observable conditions that are true today when the relevant flow
  runs — they are not test cases to be written or requirements to be met
  later.
- WRONG: "The system should validate the session before granting access."
- RIGHT: "The system validates the session via useAppSession before granting
  access; unauthenticated sessions are redirected to Login."

When uncertain, investigate further instead of answering.
If investigation cannot resolve the uncertainty, explicitly report "Insufficient evidence."

Before finalizing your response, verify that:
1. Every documented item has supporting evidence.
2. No unsupported assumptions remain.
3. Naming-based claims are limited to business/intent fields, cited as such, and confidence-capped at Medium.
4. No future-tense or prescriptive language appears anywhere in the document.
5. All confidence levels reflect the available evidence.
"""