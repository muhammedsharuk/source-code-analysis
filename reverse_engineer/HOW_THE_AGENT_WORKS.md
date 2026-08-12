# How the Reverse Engineer Agent Works (Detailed)

This document explains the **reverse_engineer** multi-agent system in full detail: purpose, architecture, filesystem design, skills, tools, prompts, data flow, configuration, and runtime behavior.

Audience: engineers who need to operate, debug, or extend the system.

---

## 1. Purpose

The system reverse-engineers a software repository (given only its local path — indexing happens automatically at run start) and produces four Markdown documents that describe what the codebase already implements:

| Document | Audience | Answers |
|---|---|---|
| **TASKS.md** | Developers / tech leads | What implementation capabilities exist |
| **USER_STORIES.md** | Product / BA | What business features users get |
| **EPICS.md** | Managers / PMs | How features group into initiatives |
| **ARCHITECTURE.md** | Architects / new engineers | How the system is designed |

Important framing used across all agents:

- Documents describe an **existing, already-built** system.
- Language is present-tense and observational (`does`, `validates`), not future/prescriptive (`should`, `will`, `add tests`).
- Claims must be evidence-based; inventing modules, endpoints, or frameworks is forbidden.

---

## 2. High-Level Architecture

```text
┌────────────────────────────────────────────────────────────────────┐
│                         main.py                                    │
│  Loads REPO_PATH from .env                                         │
│  Creates orchestrator                                              │
│  Sends: "Index the repository at path '<REPO_PATH>', then          │
│          discover the codebase and the features of the project."   │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────────────┐
│                 Orchestrator (create_deep_agent)                   │
│  Model: openai:gpt-5-mini                                          │
│  Tools: Codebase Memory tools                                      │
│  Backend: CompositeBackend                                         │
│    default  → StateBackend()                                       │
│    /skills/ → FilesystemBackend(reverse_engineer/skills)           │
│    /workspace/ → FilesystemBackend(reverse_engineer/temp/<repo>)   │
│  Subagents: task, user_stories, epics, architecture                │
│                                                                      │
│  Step 0: index_repository(repo_path) → resolves indexed name       │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
          delegates via Deep Agents `task` tool (by name)
                               │
       ┌───────────────────────┼───────────────────────┐
       ▼                       ▼                       ▼
  Task Agent            User Stories Agent        Epics Agent
  (skills + tools)      (tools)                   (tools)
       │                       │                       │
       │ writes                │ writes                │ writes
       ▼                       ▼                       ▼
 /workspace/TASKS.md   /workspace/USER_STORIES.md  /workspace/EPICS.md
       │                       │                       │
       └───────────┬───────────┴───────────┬───────────┘
                   │                       │
                   ▼                       ▼
            Architecture Agent      save_markdown_document
            (skills + tools)        → OUTPUT_DIR/<project>/*.md
                   │
                   ▼
         /workspace/ARCHITECTURE.md
```

### Design principle

The orchestrator **coordinates**. Specialists **analyze and write**. Large documents are **not** passed through chat messages between agents. Instead, each specialist writes to a shared filesystem path (`/workspace/*.md`), and the next agent reads that path.

---

## 3. Why Filesystem Storage Is Required

### Problem without shared filesystem

Deep Agents subagents receive a short delegation message from the orchestrator. They do **not** automatically inherit another agent's full conversation or full document content.

If the Task Agent produces a long TASKS.md (often hundreds of lines), and the orchestrator tries to paste that entire document into the User Stories Agent request:

1. Context windows fill quickly.
2. Cost and latency rise.
3. Details get truncated or paraphrased.
4. Downstream agents lose exact identifiers (routes, class names, fields).

### Solution: workspace handoff files

The orchestrator uses Deep Agents `CompositeBackend` so virtual paths map to real directories:

| Virtual path prefix | Physical directory | Purpose |
|---|---|---|
| `/workspace/` | `reverse_engineer/temp/<safe-repo-name>/` | Shared agent-to-agent handoff Markdown, scoped to the repository being analyzed |
| `/skills/` | `reverse_engineer/skills/` | Skill folders (`SKILL.md`) loaded from disk |
| *(everything else)* | `StateBackend` (in-memory/state) | Default ephemeral virtual FS |

`<safe-repo-name>` is derived from `REPO_PATH` by `utils/naming.py::safe_directory_name` (the same sanitizer `save_markdown_document` uses for `OUTPUT_DIR`), so each repository gets its own isolated workspace directory and running the pipeline against repo B never overwrites repo A's `/workspace/TASKS.md` etc.

Agents use Deep Agents built-in filesystem tools (`read_file`, `write_file`, etc.) against `/workspace/...`.

### Two output layers (do not confuse them)

| Layer | Where | Who writes | Purpose |
|---|---|---|---|
| **Workspace / temp** | `temp/<safe-repo-name>/TASKS.md` etc. via `/workspace/` | Agents via FS tools | Intermediate handoff between agents |
| **Persistent output** | `OUTPUT_DIR/<project>/` | `save_markdown_document` tool | Final deliverables for humans |

Both happen for each document type. Workspace is for the pipeline; `OUTPUT_DIR` is for delivery.

---

## 4. Runtime Entry Point

### File: `main.py`

```python
orchestrator = create_orchestrator_agent(repo_path)
request = (
    f"Index the repository at path '{repo_path}', "
    "then discover the codebase and the features of the project."
)
orchestrator.invoke({"messages": [{"role": "user", "content": request}]})
```

### Config: `config.py`

Loads `.env` via `python-dotenv`:

| Variable | Required | Meaning |
|---|---|---|
| `REPO_PATH` | Yes | Local path to the repository to analyze; indexed automatically via `index_repository` at run start |
| `OUTPUT_DIR` | No | Absolute/relative path for final Markdown (default: `reverse_engineer/outputs`) |
| `OPENAI_API_KEY` | Yes | LLM access |
| `LANGSMITH_*` | No | Optional tracing |

No project name is configured anywhere. The orchestrator always calls `index_repository(repo_path)` and lets the indexer assign the project name, then resolves and reuses that exact name for every subsequent tool call and delegation.

`OUTPUT_DIR` is read as a **string** from the environment, then converted to a `Path`:

```python
output_dir = Path(os.getenv("OUTPUT_DIR", str(default_output_dir))).expanduser().resolve()
```

Example:

```env
OUTPUT_DIR=C:\Users\MuhammedSharukPM\focaloidProjects\sourceCodeAnalysis\output
```

---

## 5. Orchestrator Details

### File: `agents/orchestrator.py`

Creates one Deep Agent with:

- **Model:** `openai:gpt-5-mini`
- **System prompt:** `prompts/orchestrator.py`
- **Tools:** Codebase Memory tools (for coordination / checks if needed)
- **Subagents:** architecture, epics, task, user_stories
- **Backend:** CompositeBackend as described above

### Orchestrator workflow (prompt-enforced order)

0. Call `index_repository(repo_path)`; resolve the exact indexed project name from the response (or via `list_projects` matching on root path); optionally confirm with `index_status` / `check_index_coverage`. Use this name verbatim for everything that follows.
1. Invoke **Task Agent**
2. Confirm `/workspace/TASKS.md` exists → invoke **User Stories Agent** (tell it to read that path)
3. Confirm `/workspace/USER_STORIES.md` exists → invoke **Epics Agent**
4. Confirm `/workspace/EPICS.md` exists → invoke **Architecture Agent**
5. Final consistency check by reading all four workspace files

### Filesystem Handoff Contract (orchestrator prompt)

Exact paths:

- `/workspace/TASKS.md`
- `/workspace/USER_STORIES.md`
- `/workspace/EPICS.md`
- `/workspace/ARCHITECTURE.md`

Rules:

- Shared filesystem is the **only** channel for transferring generated documents between agents.
- Delegation messages pass **project name + paths + action**, not full document bodies.
- Do not continue if a required file is missing/empty; re-invoke the responsible agent.
- The project is indexed by the orchestrator itself in Step 0; `index_repository` is not called again once it succeeds.

---

## 6. Subagents

Each subagent is a dictionary passed into Deep Agents:

```python
{
  "name": "...",
  "description": "...",
  "system_prompt": "...",
  "model": "openai:gpt-5-mini",
  "tools": [...],
  "skills": ["/skills/"]   # optional; only some agents set this
}
```

### 6.1 Task Agent

**Files:** `agents/subagents/task_agent.py`, `prompts/task_subagent.py`

**Tools:** Codebase Memory + `save_markdown_document`  
**Skills:** `["/skills/"]` (loads `codebase-memory-investigation`)

**Job:** Reverse-engineer feature-level implementation tasks from the codebase.

Key prompt rules:

- Task = a **feature/capability**, not one function/file.
- Three-pass investigation:
  - Pass 0: repository coverage mapping (mandatory)
  - Pass 1: feature discovery per coverage unit
  - Pass 2: deepen and document each task with evidence
- Follow the `codebase-memory-investigation` skill workflow.
- Write complete doc to `/workspace/TASKS.md`
- Persist via `save_markdown_document(document_type="tasks")`
- Return a short completion summary to the orchestrator (not the full document)

### 6.2 User Stories Agent

**Files:** `agents/subagents/user_stories_agent.py`, `prompts/user_stories_subagent.py`

**Tools:** Codebase Memory + `save_markdown_document`  
**Skills:** none by default

**Job:** Aggregate tasks into business user stories.

Key rules:

- Must read `/workspace/TASKS.md` first; stop if missing.
- Stories are a **higher aggregation** than tasks (avoid 1:1 restatement).
- Copy identifiers (routes, fields, classes) from task evidence when possible.
- Write `/workspace/USER_STORIES.md`
- Persist with `document_type="user_stories"`

### 6.3 Epics Agent

**Files:** `agents/subagents/epics_agent.py`, `prompts/epics_subagent.py`

**Tools:** Codebase Memory + `save_markdown_document`  
**Skills:** none by default

**Job:** Group user stories into epics/initiatives.

Key rules:

- Must read `/workspace/USER_STORIES.md` (and optionally `/workspace/TASKS.md`)
- Epics are higher aggregation than stories (avoid 1:1 renaming)
- Write `/workspace/EPICS.md`
- Persist with `document_type="epics"`

### 6.4 Architecture Agent

**Files:** `agents/subagents/architecture_agent.py`, `prompts/architecture_subagent.py`

**Tools:** Codebase Memory + `save_markdown_document`  
**Skills:** `["/skills/"]`

**Job:** Produce architecture documentation grounded in code + upstream docs.

Key rules:

- Must read all three upstream workspace docs first.
- Prefer source code over workspace docs when they conflict; report the conflict.
- Each section includes inline `Evidence` and `Confidence`.
- Write `/workspace/ARCHITECTURE.md`
- Persist with `document_type="architecture"`

---

## 7. Skills System (Deep Agents)

### What skills are

Skills are on-demand instruction packs. At startup, Deep Agents loads only each skill's **name + description** into the system prompt. When the model decides a skill is relevant, it reads the full `SKILL.md` via filesystem tools.

### Location and routing

Physical:

```text
reverse_engineer/skills/codebase-memory-investigation/SKILL.md
```

Virtual:

```text
/skills/codebase-memory-investigation/SKILL.md
```

Because orchestrator routes `/skills/` → `FilesystemBackend(root_dir=.../skills)`, listing `/skills/` discovers skill directories on disk.

### Correct skills configuration

Subagent field must be a **list of source directories** (parents of skill folders), not the skill folder itself:

```python
"skills": ["/skills/"]
```

Not:

```python
"skills": "/skills/codebase-memory-investigation/"   # wrong: string + points at skill itself
```

Deep Agents scans each source path for **subdirectories** containing `SKILL.md`.

### Frontmatter requirement

`SKILL.md` must start with YAML frontmatter:

```yaml
---
name: codebase-memory-investigation
description: ...
---
```

`name` should match the parent directory name.

### Which agents use skills today

| Agent | Skills |
|---|---|
| Task | `/skills/` |
| Architecture | `/skills/` |
| User Stories | none |
| Epics | none |
| Orchestrator | none (unless configured on `create_deep_agent`) |

### Current skill content

`codebase-memory-investigation` defines the investigation order:

1. Understand repo (`get_architecture`, `get_graph_schema`)
2. Discover components (`search_graph`)
3. Trace relationships (`trace_path`)
4. Read source (`get_code_snippet`, `search_code`)
5. Cross-check and only then document

Discovery alone does not force usage: the agent must choose to `read_file` the skill. Task/architecture prompts reinforce this by referencing the skill workflow.

---

## 8. Tools

### 8.1 Codebase Memory tools

**Files:** `tools/codebase_memory_tools.py`, `utils/codebase_memory.py`

Each tool wraps a subprocess call:

```text
codebase-memory-mcp cli <command> '<json-payload>' --json
```

Available tools (non-exhaustive of args):

| Tool | Purpose |
|---|---|
| `list_projects` | List indexed projects |
| `index_repository` | Index a repo (normally not needed at run time) |
| `index_status` / `check_index_coverage` | Index health |
| `get_architecture` | Architectural overview from graph |
| `get_graph_schema` | Node/edge schema |
| `search_graph` | Symbol/relationship search |
| `trace_path` | Call/dataflow tracing |
| `query_graph` | Raw graph query |
| `get_code_snippet` | Source by qualified name |
| `search_code` | Text/regex search in source |
| `detect_changes` | Change blast radius |
| `manage_adr` | ADR get/set/update |
| `ingest_traces` | Ingest runtime traces |
| `delete_project` | Remove indexed project |

Prerequisite: none — the orchestrator indexes the repository itself via `index_repository` as Step 0 of every run, using the project name the indexer assigns.

### 8.2 Persistent Markdown tool

**File:** `tools/markdown_file_tools.py`

`save_markdown_document(project_name, document_type, content)`:

1. Validates `document_type` ∈ `{tasks, user_stories, epics, architecture}`
2. Sanitizes project name into a safe folder name
3. Writes to:

```text
<OUTPUT_DIR>/<safe-project-name>/<DOCUMENT>.md
```

Mapping:

| document_type | Filename |
|---|---|
| `tasks` | `TASKS.md` |
| `user_stories` | `USER_STORIES.md` |
| `epics` | `EPICS.md` |
| `architecture` | `ARCHITECTURE.md` |

---

## 9. Shared Prompt Rules

**File:** `prompts/shared_promts.py` → `evidence_base_analysis_rules_prompt()`

Embedded into task / user stories / epics prompts (and related evidence discipline elsewhere). Core rules:

- Never invent functionality.
- Prefer source code over graph metadata when they disagree.
- Naming alone is not enough for implementation behavior claims.
- Naming **can** support business-intent fields if cited and confidence-capped.
- Prefer fewer correct items over many invented ones.
- Present-tense as-built language only.
- On uncertainty: investigate further, else write `Insufficient evidence`.

---

## 10. End-to-End Sequence (Minute by Minute)

### Step 0 — Preconditions

1. `codebase-memory-mcp` is on PATH.
2. `.env` has `OPENAI_API_KEY`, `REPO_PATH`, optional `OUTPUT_DIR` / LangSmith. The target repo does **not** need to be indexed beforehand.
3. Dependencies installed from `requirements.txt`.

### Step 1 — Process start

1. `python main.py` runs.
2. `config.py` loads env and resolves `output_dir`.
3. Orchestrator agent is created with CompositeBackend routes.
4. Skills directories under `/skills/` become discoverable to subagents that declare `skills=["/skills/"]`.
5. Orchestrator receives the discovery user message with `REPO_PATH`.
6. Orchestrator calls `index_repository(repo_path)` and resolves the exact indexed project name for use in every subsequent step.

### Step 2 — Task Agent

1. Orchestrator delegates to `task_agent` with the resolved project name.
2. Task Agent may load skill metadata and read full skill instructions.
3. Task Agent explores Codebase Memory tools extensively.
4. Builds feature-level task catalog with evidence/confidence.
5. Writes `/workspace/TASKS.md` → physically `temp/<safe-repo-name>/TASKS.md`.
6. Calls `save_markdown_document(..., "tasks", ...)` → `OUTPUT_DIR/<project>/TASKS.md`.
7. Returns short completion message to orchestrator.

### Step 3 — User Stories Agent

1. Orchestrator confirms workspace tasks file exists (or instructs agent to read it).
2. User Stories Agent reads `/workspace/TASKS.md`.
3. Aggregates tasks into business stories; may validate with Codebase Memory tools.
4. Writes `/workspace/USER_STORIES.md`.
5. Persists `OUTPUT_DIR/<project>/USER_STORIES.md`.
6. Returns short completion summary.

### Step 4 — Epics Agent

1. Reads `/workspace/USER_STORIES.md` (and optionally tasks).
2. Groups stories into epics.
3. Writes `/workspace/EPICS.md`.
4. Persists `OUTPUT_DIR/<project>/EPICS.md`.

### Step 5 — Architecture Agent

1. Reads `/workspace/TASKS.md`, `/workspace/USER_STORIES.md`, `/workspace/EPICS.md`.
2. Uses Codebase Memory tools (+ skill) to verify/fill architecture.
3. Writes `/workspace/ARCHITECTURE.md`.
4. Persists `OUTPUT_DIR/<project>/ARCHITECTURE.md`.

### Step 6 — Orchestrator wrap-up

1. Reads all four workspace docs for consistency (stack, entities, endpoints, IDs).
2. May re-delegate if inconsistencies are found.
3. Run completes.

---

## 11. Directory Map

```text
reverse_engineer/
├── main.py                      # Entry point
├── config.py                    # REPO_PATH + OUTPUT_DIR
├── requirements.txt
├── .env / .env.example
├── README.md                    # Quick start
├── HOW_THE_AGENT_WORKS.md       # This document
├── agents/
│   ├── orchestrator.py          # Deep agent + CompositeBackend
│   └── subagents/
│       ├── task_agent.py
│       ├── user_stories_agent.py
│       ├── epics_agent.py
│       └── architecture_agent.py
├── prompts/
│   ├── orchestrator.py
│   ├── shared_promts.py
│   ├── task_subagent.py
│   ├── user_stories_subagent.py
│   ├── epics_subagent.py
│   └── architecture_subagent.py
├── tools/
│   ├── codebase_memory_tools.py
│   └── markdown_file_tools.py
├── utils/
│   └── codebase_memory.py       # CLI subprocess wrapper
├── skills/
│   └── codebase-memory-investigation/
│       └── SKILL.md
├── temp/<safe-repo-name>/       # /workspace/ physical store, one folder per repo
│   ├── TASKS.md
│   ├── USER_STORIES.md
│   ├── EPICS.md
│   └── ARCHITECTURE.md
└── outputs/                     # default OUTPUT_DIR if unset
```

Final deliverables (configurable):

```text
<OUTPUT_DIR>/<indexed-project-name>/
├── TASKS.md
├── USER_STORIES.md
├── EPICS.md
└── ARCHITECTURE.md
```

`<indexed-project-name>` comes from `index_repository`'s response, not from any environment variable.

---

## 12. Traceability Model

Intended hierarchy:

```text
Architecture (system structure)
        ↓
     Epics (business initiatives)
        ↓
  User Stories (user-facing capabilities)
        ↓
     Tasks (implementation features)
```

Cross-links are maintained through IDs referenced in each document (task IDs inside stories, story IDs inside epics, modules/components linked to architecture sections).

---

## 13. Technology Stack

| Piece | Role |
|---|---|
| Python | Runtime |
| Deep Agents (`deepagents`) | Multi-agent orchestration, filesystem middleware, skills |
| LangChain OpenAI | Model provider binding |
| OpenAI `gpt-5-mini` | Reasoning model for all agents |
| Codebase Memory MCP CLI | Knowledge graph over source |
| python-dotenv | Env loading |
| LangSmith (optional) | Traceability / debugging runs |

---

## 14. Operational Notes & Failure Modes

### Common failures

| Symptom | Likely cause |
|---|---|
| Empty / missing skill usage | Wrong `skills` path form, skills not routed to disk backend, or prompt never instructs agent to read skill |
| Downstream agent invents content | Upstream `/workspace/*.md` missing and agent continued anyway |
| Docs written inside repo unexpectedly | `OUTPUT_DIR` unset or relative; falls back to default under `reverse_engineer` |
| `TypeError` on path join | `OUTPUT_DIR` left as raw string without `Path(...)` conversion |
| Codebase Memory errors | CLI not on PATH, project name mismatch, or project not indexed |
| Architecture contradicts tasks | Architecture skipped tool verification / relied on project name alone |

### Debugging tips

1. Inspect `temp/<safe-repo-name>/` after each stage to see workspace handoffs.
2. Inspect `OUTPUT_DIR/<project>/` for final files.
3. Watch terminal for `Running: ['codebase-memory-mcp', ...]` lines.
4. Enable LangSmith tracing for full tool/subagent traces.
5. Confirm skill discovery by ensuring `/skills/` lists the skill directory and `SKILL.md` exists.

### Non-determinism

LLM agents are non-deterministic. Prompt contracts reduce hallucination but do not guarantee identical docs across runs. Always review evidence and confidence fields before treating outputs as authoritative.

---

## 15. Summary

The Reverse Engineer agent is a **prompt-orchestrated, tool-using, filesystem-backed pipeline**:

1. Orchestrator sequences four specialists.
2. Specialists investigate an indexed codebase via Codebase Memory.
3. They exchange large documents through `/workspace` (disk-backed `temp/<safe-repo-name>/`).
4. They persist final Markdown through `save_markdown_document` into `OUTPUT_DIR`.
5. Evidence rules and (for key agents) a shared investigation skill keep outputs grounded in real code.

That combination — **delegation + shared filesystem + persistent export + evidence discipline** — is the core of how the system works.
