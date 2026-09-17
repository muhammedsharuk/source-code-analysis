# Reverse Engineer

AI multi-agent pipeline that reverse-engineers a codebase into structured documentation. It runs as a small HTTP service: hand it a repository (as a zip upload or a repo URL to clone), and it indexes it, analyzes it, and produces:

| Deliverable | Description |
|---|---|
| **Tasks** | Implementation work breakdown, traced through the codebase |
| **User Stories** | Business features inferred from tasks |
| **Features** | Groupings of user stories |
| **Epics** | Strategic groupings of features |
| **Architecture** | System design documentation |

The documentation hierarchy this pipeline builds, highest to lowest level of aggregation, is **Epic → Feature → User Story → Task**.

This service does not own job records, status, or history — a caller (e.g. [`asdlc-assistant`](../asdlc-assistant)) does. It answers exactly two questions over HTTP: "run this" (`POST /jobs`) and "give me the files it produced" (`GET /jobs/{id}/files*`). Everything about a job's live progress is published to NATS as it happens, not polled back from here.

---

## Prerequisites

- Python 3.12+
- An LLM API key matching whatever models you configure (see [Environment variables](#environment-variables)) — typically an [OpenAI API key](https://platform.openai.com/) and/or an [Anthropic API key](https://console.anthropic.com/)
- [`codebase-memory-mcp`](https://github.com/DeusData/codebase-memory-mcp) CLI installed and available on `PATH` (the Docker image pins **v0.8.1** — see [Docker](#docker) for why)
- `git` on `PATH`, only if you want to use the repo-URL-clone job source (not needed for zip uploads)

Optional:

- [LangSmith](https://smith.langchain.com/) for run tracing
- NATS, for live progress streaming to a caller
- Postgres, for direct job-status write-back into a caller's own table

You do **not** need a local checkout of the repository you want analyzed — a job supplies its own source (uploaded zip or cloned repo URL), extracted into a private, temporary directory that's deleted once the job finishes.

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

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | Yes* | LLM calls, if any configured model is an OpenAI model |
| `ANTHROPIC_API_KEY` | Yes* | LLM calls, if any configured model is an Anthropic model |
| `COMPACT_LLM_MODEL` | Yes | Model id for lightweight/high-volume calls (e.g. per-batch subagents) |
| `STANDARD_LLM_MODEL` | Yes | Model id for standard-weight calls |
| `LARGE_LLM_MODEL` | Yes | Model id for the orchestrator itself |
| `LANGSMITH_TRACING` | No | Set `true` to enable tracing |
| `LANGSMITH_API_KEY` | No | LangSmith API key |
| `LANGSMITH_PROJECT` | No | LangSmith project name |
| `NATS_URL` | No | Live progress publishing during a job. Unset → publishing is silently skipped, not an error. When integrated with asdlc-assistant, point this at their NATS instance |
| `NATS_AUTH_TOKEN` | No | NATS auth token, if the server requires one |
| `DATABASE_URL` | No | Direct write-back of job completion status into the caller's own `code_analysis_jobs` table (see `server/job_db.py`). Unset → skipped, not an error. This service only ever `UPDATE`s a row the caller already created — it never `INSERT`s |
| `GIT_CLONE_ALLOWED_HOSTS` | No | Comma-separated allowlist of hosts a repo-URL job may clone from (e.g. `github.com,gitlab.com`). Unset → any public host is allowed; the private-IP/SSRF check in `utils/git_clone.py` always applies regardless of this setting |

\* Whichever provider(s) your `*_LLM_MODEL` values actually use.

There is no `REPO_PATH`/`OUTPUT_DIR` to configure — a job's source arrives per-request (see [API](#api)), and its output always lands under `output/<indexed-project-name>/`.

---

## Run

```powershell
cd reverse_engineer
uvicorn server.app:app --reload
```

Or with uv:

```powershell
uv run uvicorn server.app:app --reload
```

The API is then available at `http://localhost:8000`. See [Docker](#docker) for the containerized way to run it instead.

---

## API

### `POST /jobs`

Starts a job. `multipart/form-data`, always includes `name`, optionally `job_id` (a caller-supplied id, e.g. so it matches a row the caller already created — omit it to have one generated here), and **exactly one** of:

- `file` — a zip of the repository
- `repo_url` — an `https://` URL to clone, plus optionally `git_username` / `git_token` for a private repository

The clone path never accepts embedded credentials in the URL itself (`https://user:pass@host/...` is rejected) — use the separate `git_username`/`git_token` fields, which are used once (via a short-lived `GIT_ASKPASS` script, never written to disk or passed as a process argument) and discarded; `.git` itself is stripped from the clone before analysis ever starts.

```bash
# zip upload
curl -X POST http://localhost:8000/jobs \
  -F name=my-project -F file=@./my-project.zip

# public repo URL
curl -X POST http://localhost:8000/jobs \
  -F name=my-project -F repo_url=https://github.com/org/repo.git

# private repo URL
curl -X POST http://localhost:8000/jobs \
  -F name=my-project -F repo_url=https://github.com/org/repo.git \
  -F git_username=someone -F git_token=ghp_xxx
```

Returns immediately with `{ "job_id", "name", "status": "active" }` — the actual pipeline runs as a background task. Everything about its progress from here is published to NATS (see [Live progress & job status](#live-progress--job-status)), not polled from this endpoint.

### `GET /jobs/{job_id}/files`

Returns the file tree of a completed job's output (currently a flat list of `.md` files under one `output` folder).

### `GET /jobs/{job_id}/files/content?path=<name>`

Returns one file's raw content as plain text.

---

## How it works

```
Job source (zip upload, or repo URL to clone)
              │
              ▼
   Orchestrator: index_repository
              │  (resolves exact indexed project name)
              ▼
          Orchestrator
              │
              ▼
  1. Repository Indexing
  2. Code Index Build              (batched Index Batch Agent, deterministic seed + merge)
  3. Task Generation Loop          (batched Task Agent)             ──► /workspace/TASKS.md
  4. Task Output Merge & Finalization
  5. User Story Generation                                          ──► /workspace/USER_STORIES.md
  6. Feature Generation                                             ──► /workspace/FEATURES.md
  7. Epic Generation                                                ──► /workspace/EPICS.md
  8. Architecture Generation                                        ──► /workspace/ARCHITECTURE.md
  9. Final Consistency Check
              │
              ▼
   output/<indexed-project-name>/
       TASKS.md · USER_STORIES.md · FEATURES.md · EPICS.md · ARCHITECTURE.md
```

Externally, this collapses to 4 macro-steps: Stage 1 is `index`, Stage 2 is `code-index`, Stages 3–4 are `tasks`, and Stages 5–8 collapse into `docs` (Stage 9 doesn't get its own step). These four (`index`, `code-index`, `tasks`, `docs`) are what a caller's UI actually renders as a progress bar (see `server/events.py`'s `default_steps`) — the 9-stage breakdown above is the orchestrator's own internal plan, one level more detailed.

### Agents

| Agent | Role |
|---|---|
| **Orchestrator** | Coordinates pipeline order; passes file paths, not full document contents |
| **Index Batch Agent** | Analyzes the codebase in small batches, dispatched one at a time (Stage 2) |
| **Task Agent** | Explores the codebase via Codebase Memory tools, one batch at a time; produces implementation tasks |
| **User Stories Agent** | Infers business features from `/workspace/TASKS.md` |
| **Feature Agent** | Groups user stories into features from `/workspace/USER_STORIES.md` |
| **Epics Agent** | Groups features into epics from `/workspace/FEATURES.md` |
| **Architecture Agent** | Builds architecture docs from workspace docs + Codebase Memory tools |
| **Code Index Agent** | Legacy single-shot version of Stage 2, kept registered but no longer driven by the orchestrator prompt — see `agents/orchestrator.py`'s comment |

### Live progress & job status

- **NATS** (optional, `NATS_URL`): as a job runs, `log` / `step_progress` / `end` events are published to `stream.<job_id>.*`, matching the subject convention an integrating caller's own NATS stream expects.
- **Postgres** (optional, `DATABASE_URL`): on completion, failure, or stop, this service `UPDATE`s the caller's own `code_analysis_jobs.status` column directly — it never creates that row, only updates one the caller already created.

Neither is required to run a job — both are silent no-ops when unset.

### Shared filesystem

The orchestrator uses a `CompositeBackend`:

- `/workspace/` → `temp/<safe-repo-name>/` (agent-to-agent handoff files, scoped per job so concurrent jobs never overwrite each other's intermediate docs)
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
├── config.py                     # Loads .env, builds the three LLM model clients
├── requirements.txt
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── server/
│   ├── app.py                    # FastAPI app: POST /jobs, GET /jobs/{id}/files*
│   ├── run_manager.py            # Job lifecycle: start, execute, stop, ephemeral cleanup
│   ├── run_store.py              # In-memory registry of runs active in this process
│   ├── history_store.py          # Durable record of completed runs (server/runs/history.json)
│   ├── event_handler.py          # Translates orchestrator events into log/progress events
│   ├── events.py                 # Job/DTO models, the 4 macro-steps
│   ├── job_db.py                 # Optional Postgres status write-back
│   └── nats_publisher.py         # Optional NATS live-progress publishing
├── agents/
│   ├── orchestrator.py           # Deep agent + filesystem backend
│   └── subagents/                # Index batch, task, user stories, feature, epics, architecture
├── prompts/                      # System prompts per agent + shared evidence rules
├── tools/
│   ├── codebase_memory_tools.py  # Codebase Memory CLI wrappers
│   ├── batch_queue_tools.py      # Deterministic task-batch queue tools
│   ├── index_batch_queue_tools.py # Deterministic index-batch queue tools
│   ├── checklist_tools.py        # seed_checklist and friends
│   └── markdown_file_tools.py    # Persist final docs under output/<project>/
├── utils/
│   ├── codebase_memory.py        # Subprocess bridge to codebase-memory-mcp
│   ├── zip_extract.py            # Safe extraction of an uploaded repo zip
│   ├── git_clone.py              # Safe cloning of a repo URL (SSRF guard, credential hygiene)
│   └── naming.py                 # Safe directory names from a repo path
├── skills/                       # Deep Agents skills (SKILL.md folders)
├── uploads/<job_id>/              # A job's private, ephemeral copy (zip or clone) — deleted after the run
├── temp/<repo>/                   # Workspace handoff files (/workspace/), one subfolder per job
└── output/<project>/              # Final Markdown destination, named by the indexer
```

---

## Outputs

After a successful run:

**Workspace (intermediate):** `temp/<safe-repo-name>/TASKS.md`, `USER_STORIES.md`, `FEATURES.md`, `EPICS.md`, `ARCHITECTURE.md`

**Persistent (final):**

```
output/<indexed-project-name>/
├── TASKS.md
├── USER_STORIES.md
├── FEATURES.md
├── EPICS.md
└── ARCHITECTURE.md
```

The `<indexed-project-name>` is whatever `index_repository` assigns when it indexes the job's source; it is not configured by you.

Review confidence levels and evidence before treating results as official documentation.

---

## Docker

```powershell
docker compose up --build
```

Builds and runs the service on port `8000`. The `cbm-cache` volume persists knowledge-graph indexes across container restarts so a re-analyzed repo doesn't re-index from scratch, and `./output` is bind-mounted so generated docs are browsable straight from the host. Rebuild (`--build`) after any source change — the Dockerfile bakes the code into the image; nothing under `server/`, `agents/`, `tools/`, or `utils/` is live-mounted.

The image installs `git` specifically for the repo-URL-clone job source — the zip-upload path doesn't need it.

---

## Dependencies

From `requirements.txt`:

- `deepagents` — multi-agent orchestration
- `langchain-openai` — OpenAI model access
- `python-dotenv` — env loading
- `langsmith` — optional tracing
- `fastapi`, `uvicorn`, `python-multipart` — the HTTP API
- `nats-py` — optional live-progress publishing
- `psycopg[binary]` — optional Postgres status write-back

External:

- `codebase-memory-mcp` — knowledge graph over the target source code
- `git` — only for the repo-URL-clone job source
