---
name: codebase-memory-investigation
description: Use this skill whenever you need to understand an existing codebase, reverse engineer functionality, or gather evidence from the Codebase Memory knowledge graph before generating documentation.
---

# Codebase Memory Investigation

## Overview

This skill defines the standard investigation process for analyzing a repository using Codebase Memory tools. Its purpose is to collect accurate, evidence-based information while minimizing assumptions and hallucinations.

Always use this workflow before generating Tasks, User Stories, Epics, Architecture documentation, or answering implementation-related questions.

## Instructions

### 1. Understand the Repository

Begin by understanding the overall repository before investigating specific functionality.

Use:

- `get_architecture`
- `get_graph_schema`

Identify:

- Entry points
- Major modules
- Project structure
- Layers
- Technologies
- Architectural style

Do not begin documenting until you understand the overall structure.

---

### 2. Discover Relevant Components

Locate the implementation related to the current objective.

Use:

- `search_graph`

Search for relevant:

- Routes
- Controllers
- Services
- Repositories
- Entities
- Classes
- Interfaces
- Modules
- Configuration

Prefer graph navigation over source code search.

---

### 3. Trace Relationships

Once important components are identified, determine how they interact.

Use:

- `trace_path`

Trace:

- Callers
- Callees
- Dependencies
- Data flow
- Execution flow

Continue tracing until the complete implementation responsibility is understood.

---

### 4. Read the Source Code

The knowledge graph describes relationships.

The source code defines behavior.

Whenever implementation details are incomplete, ambiguous, or require validation, use:

- `get_code_snippet`

Read the implementation to understand:

- Business logic
- Validation
- Conditional flows
- Error handling
- Side effects
- Important implementation details

Never infer behavior without reading the relevant code.

---

### 5. Search the Code When Necessary

If the graph cannot locate the required information, perform targeted source code searches.

Use:

- `search_code`

Typical searches include:

- Configuration values
- Constants
- Middleware
- Dependency injection
- Framework annotations
- Feature flags
- SQL
- DTOs

Only use code search when graph exploration is insufficient.

---

### 6. Cross Validate Findings

Before documenting any conclusion:

Verify the implementation using multiple sources whenever possible.

Cross-check information between:

- Knowledge Graph
- Call Traces
- Source Code
- Architecture
- Configuration

If evidence conflicts:

- Prefer source code.
- Report uncertainty.
- Do not guess.

---

### 7. Produce Evidence-Based Results

Every documented statement must be supported by evidence.

Evidence may include:

- Routes
- Controllers
- Services
- Repositories
- Entities
- Classes
- Methods
- Configuration
- Source code
- Call traces

If sufficient evidence cannot be found:

Return:

> Insufficient evidence.

Never fabricate implementation details.

---

## Investigation Principles

- The knowledge graph is the primary navigation mechanism.
- Source code is the source of truth.
- Investigate before concluding.
- Continue exploring until no significant information remains.
- Prefer evidence over assumptions.
- It is better to report incomplete evidence than incorrect information.

## Tool Usage Priority

1. `get_architecture`
2. `get_graph_schema`
3. `search_graph`
4. `trace_path`
5. `get_code_snippet`
6. `search_code`

Only move to a lower-priority tool when higher-priority tools cannot answer the question.

## Final Verification

Before completing your work, verify:

- Every conclusion has supporting evidence.
- Important dependencies have been traced.
- Relevant source code has been reviewed when necessary.
- No unsupported assumptions remain.
- Confidence reflects the available evidence.