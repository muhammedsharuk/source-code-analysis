# Reverse Engineer

AI multi-agent pipeline that reverse-engineers an indexed codebase into structured documentation:

| Deliverable | Description |
|---|---|
| **Tasks** | Implementation work breakdown |
| **User Stories** | Business features inferred from tasks |
| **Epics** | Strategic groupings of user stories |
| **Architecture** | System design documentation |

Agents share intermediate docs through a Deep Agents filesystem backend, then persist final Markdown under the directory configured by `OUTPUT_DIR`.

---

## Prerequisites

- Python 3.12+
- [OpenAI API key](https://platform.openai.com/)
- [`codebase-memory-mcp`](https://github.com/) CLI installed and available on `PATH`
- A local path to the repository you want analyzed (it does **not** need to be indexed beforehand — the orchestrator indexes it automatically)

Optional:

- [LangSmith](https://smith.langchain.com/) for run tracing

---

## Setup

```powershell
cd reverse_engineer
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Copy the env template and fill in values:

```powershell
copy .env.example .env
```

Edit `.env`:

| Variable | Required | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | Yes | LLM calls |
| `REPO_PATH` | Yes | Local path to the repository to analyze; indexed automatically at run start, and the indexer assigns the project name |
| `OUTPUT_DIR` | No | Final Markdown destination; defaults to `reverse_engineer/output` |
| `LANGSMITH_TRACING` | No | Set `true` to enable tracing |
| `LANGSMITH_API_KEY` | No | LangSmith API key |
| `LANGSMITH_PROJECT` | No | LangSmith project name |

---

## Run

```powershell
cd reverse_engineer
python main.py
```

Or with uv:

```powershell
uv run python main.py
```

---

## How it works

```
Repository Path (REPO_PATH)
              │
              ▼
   Orchestrator: index_repository
              │  (resolves exact indexed project name)
              ▼
        Orchestrator
              │
    ┌─────────┴─────────┐
    │  1. Task Agent    │──► /workspace/TASKS.md
    │  2. User Stories  │──► /workspace/USER_STORIES.md  (reads TASKS)
    │  3. Epics         │──► /workspace/EPICS.md         (reads USER_STORIES)
    │  4. Architecture  │──► /workspace/ARCHITECTURE.md  (reads all three)
    └───────────────────┘
              │
              ▼
   <OUTPUT_DIR>/<project>/TASKS.md
                          USER_STORIES.md
                          EPICS.md
                          ARCHITECTURE.md
```

### Agents

| Agent | Role |
|---|---|
| **Orchestrator** | Coordinates pipeline order; passes file paths, not full document contents |
| **Task Agent** | Explores the codebase via Codebase Memory tools; produces implementation tasks |
| **User Stories Agent** | Infers business features from `/workspace/TASKS.md` |
| **Epics Agent** | Groups user stories into epics from `/workspace/USER_STORIES.md` |
| **Architecture Agent** | Builds architecture docs from workspace docs + Codebase Memory tools |

### Shared filesystem

The orchestrator uses a `CompositeBackend`:

- `/workspace/` → `temp/<safe-repo-name>/` (agent-to-agent handoff files, scoped per repository so different repos never overwrite each other's intermediate docs)
- `/skills/` → `skills/` (Deep Agents skills loaded from disk)

Agents write intermediate Markdown to `/workspace/*.md` so the next agent can read it without receiving a large payload from the orchestrator.

### Skills

Task and Architecture agents can load skills from `skills/` (for example `codebase-memory-investigation`). Skills are discovered as directories under `/skills/` that contain a `SKILL.md`.

### Evidence rules

All subagents share evidence-based analysis rules from `prompts/shared_promts.py`: statements must be grounded in Codebase Memory tool output or input docs; unsupported claims should be marked as insufficient evidence rather than invented.

---

## Project structure

```
reverse_engineer/
├── main.py                 # Entry point
├── config.py               # Loads .env (REPO_PATH, OUTPUT_DIR)
├── requirements.txt
├── .env.example
├── agents/
│   ├── orchestrator.py     # Deep agent + filesystem backend
│   └── subagents/          # Task, user stories, epics, architecture
├── prompts/                # System prompts per agent
├── tools/
│   ├── codebase_memory_tools.py   # Codebase Memory CLI wrappers
│   └── markdown_file_tools.py     # Persist final docs to OUTPUT_DIR
├── utils/
│   └── codebase_memory.py  # Subprocess bridge to codebase-memory-mcp
├── skills/                 # Deep Agents skills (SKILL.md folders)
├── temp/<repo>/            # Workspace handoff files (/workspace/), one subfolder per repo
└── output/<project>/       # Default destination when OUTPUT_DIR is unset
```

---

## Outputs

After a successful run:

**Workspace (intermediate):** `temp/<safe-repo-name>/TASKS.md`, `USER_STORIES.md`, `EPICS.md`, `ARCHITECTURE.md`

**Persistent (final):**

```
<OUTPUT_DIR>/<indexed-project-name>/
├── TASKS.md
├── USER_STORIES.md
├── EPICS.md
└── ARCHITECTURE.md
```

The `<indexed-project-name>` is whatever `index_repository` assigns when it indexes `REPO_PATH`; it is not configured by you.

Review confidence levels and evidence before treating results as official documentation.

---

## Dependencies

From `requirements.txt`:

- `deepagents` — multi-agent orchestration
- `langchain-openai` — OpenAI model access
- `python-dotenv` — env loading
- `langsmith` — optional tracing

External:

- `codebase-memory-mcp` — knowledge graph over the target source code
