# Paper2Repo Agent

Paper2Repo Agent is an auditable local workflow for turning a machine-learning paper into an evidence-backed reproduction plan and an executable research workspace.

The project demonstrates how an agent can combine document parsing, structured extraction, human approval, tool-driven execution, durable checkpoints, validation, repair, and artifact reporting in one inspectable flow.

> Portfolio scope: this repository is a local reference implementation for research automation. It does not claim to provide authentication, a multi-tenant control plane, an OS-level sandbox, or a distributed worker queue.

## Core workflow

```mermaid
flowchart LR
    A[Paper input] --> B[Parse and extract]
    B --> C[Evidence store]
    C --> D[Plan and validate]
    D --> E[Human review]
    E --> F[Approved plan]
    F --> G[Agent execution loop]
    G --> H[Tools and generated workspace]
    H --> I[Verify, repair, and report]
    G <--> J[Checkpoint and resume]
```

Each run keeps its planning, approval, execution, verification, and provenance artifacts under one run directory. The default evidence backend is local SQLite plus JSONL; an optional MySQL backend supports shared evidence persistence.

## What this project shows

- Evidence references that connect extracted paper claims to plans, validation results, and review items.
- A human-in-the-loop approval gate before execution.
- A bounded execution loop with file contracts, command results, verification, failure digests, repair context, and durable checkpoints.
- A CLI for repeatable runs and a FastAPI console for inspecting runs and submitting approval or execution jobs.
- Deterministic fake-LLM mode for local smoke tests without provider credentials.
- Optional MySQL evidence persistence through environment configuration and Docker Compose.

## Quick start

Requirements: Python 3.10 or newer. A real paper run also needs an OpenAI-compatible LLM provider. Markdown and text inputs can use the deterministic fake LLM for a local smoke test.

### 1. Create an environment

PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
Copy-Item .env.example .env
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

macOS or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
cp .env.example .env
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

`.env` is local-only and is ignored by Git. Fill in `OPENAI_API_KEY` and `OPENAI_BASE_URL` for a real LLM run. PDF parsing through MinerU also requires `MINERU_BASE_URL` and `MINERU_API_TOKEN`.

### 2. Run a deterministic planning smoke test

```bash
paper2repo plan examples/sample_paper.md --source-type markdown --out runs/sample --fake-llm
paper2repo inspect-run runs/sample
paper2repo export-review runs/sample
```

The command writes the parsed paper, evidence, extracted specification, validation result, draft plan, and human review artifacts below `runs/sample`. Runtime output is intentionally ignored by Git.

### 3. Approve and execute a run

Review `runs/sample/planning/human_review.md` and the generated decision template. After editing the JSON decisions, approve the plan and execute it:

```bash
paper2repo approve runs/sample \
  --decision-file runs/sample/planning/approval_decisions.template.json \
  --approved-by local-review
paper2repo execute runs/sample
```

`execute` requires an approved plan. Use `--resume` to continue from the latest durable checkpoint when a run has an execution checkpoint.

### 4. Open the web console

```bash
paper2repo serve --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The console reads run artifacts, displays evidence and review information, and exposes approval, revision, and execution jobs for the local process.

## CLI reference

| Command | Purpose |
|---|---|
| `paper2repo plan PAPER_PATH --out RUN_DIR` | Parse a PDF, Markdown, or text input and create a planning run. |
| `paper2repo review RUN_DIR` | Ask the configured LLM to revise a reviewable plan. |
| `paper2repo inspect-run RUN_DIR` | Print a compact run summary and artifact references. |
| `paper2repo export-review RUN_DIR` | Export the current human review as Markdown. |
| `paper2repo approve RUN_DIR --decision-file FILE` | Materialize human decisions and write the approved plan. |
| `paper2repo execute RUN_DIR` | Execute an approved plan, verify results, and write the final report. |
| `paper2repo serve` | Start the local FastAPI console. |

Add `--fake-llm` to `plan` or `review` for deterministic local tests. Add `--quiet` to `plan`, `review`, or `execute` to suppress progress output.

## Architecture and module map

| Area | Implementation | Responsibility |
|---|---|---|
| Configuration and redaction | [`src/paper2repo_agent/config.py`](src/paper2repo_agent/config.py), [`src/paper2repo_agent/security.py`](src/paper2repo_agent/security.py) | Load environment variables and redact provider or database credentials in diagnostics. |
| Parsing and evidence | [`src/paper2repo_agent/parser.py`](src/paper2repo_agent/parser.py), [`src/paper2repo_agent/evidence.py`](src/paper2repo_agent/evidence.py) | Normalize paper inputs, store evidence chunks, and resolve field-level references. |
| Planning graph | [`src/paper2repo_agent/graphs/plan/`](src/paper2repo_agent/graphs/plan/) | Extract a structured specification, validate support, generate a plan, and build the review payload. |
| Human approval | [`src/paper2repo_agent/hitl.py`](src/paper2repo_agent/hitl.py), [`src/paper2repo_agent/review.py`](src/paper2repo_agent/review.py) | Represent review items, decisions, revisions, and approval state. |
| Execution loop | [`src/paper2repo_agent/graphs/execute/`](src/paper2repo_agent/graphs/execute/) | Generate or update files, run commands, verify contracts, collect failures, and schedule bounded repairs. |
| Runtime durability | [`src/paper2repo_agent/runtime/`](src/paper2repo_agent/runtime/) | Hold execution context and SQLite-backed LangGraph checkpoints for resume support. |
| Artifact contract | [`src/paper2repo_agent/artifacts.py`](src/paper2repo_agent/artifacts.py) | Define stable run paths, manifest references, iteration records, and reports. |
| Web console | [`src/paper2repo_agent/web/app.py`](src/paper2repo_agent/web/app.py), [`src/paper2repo_agent/web/static/`](src/paper2repo_agent/web/static/) | Serve the local UI and JSON API over run artifacts. |

## HTTP API

The `serve` command exposes these routes:

| Method | Route | Use |
|---|---|---|
| `GET` | `/api/status` | Report the console status and configured run root. |
| `GET` | `/api/runs` | List available runs. |
| `GET` | `/api/runs/{run_name}` | Read a run summary and artifact references. |
| `GET` | `/api/runs/{run_name}/artifact?path=...` | Read an allow-listed run artifact. |
| `GET` | `/api/runs/{run_name}/evidence` | Query run evidence with optional filters. |
| `POST` | `/api/plan` | Start a planning job. |
| `POST` | `/api/runs/{run_name}/approve` | Submit human approval decisions. |
| `POST` | `/api/runs/{run_name}/revise` | Request a plan revision. |
| `POST` | `/api/runs/{run_name}/execute` | Start execution for an approved run. |
| `GET` | `/api/jobs` and `/api/jobs/{job_id}` | Inspect local background jobs. |

The root route `/` serves the bundled web console. The API is designed for a trusted local environment and has no authentication layer.

## Configuration

Copy `.env.example` to `.env` and set only the services used by your workflow.

| Variable | Required when | Example |
|---|---|---|
| `OPENAI_API_KEY` | Using a real LLM | `your-provider-key` |
| `OPENAI_BASE_URL` | Using a real LLM | `https://api.openai.com/v1` |
| `MODEL_NAME` | Selecting a model | `gpt-4.1-mini` |
| `LLM_DEFAULT_TEMPERATURE` | Tuning generation | `0` |
| `MINERU_BASE_URL` | Parsing PDFs with MinerU | `https://mineru.net/api/v4` |
| `MINERU_API_TOKEN` | Parsing PDFs with MinerU | `your-mineru-token` |
| `MINERU_MODEL_SOURCE` | Selecting MinerU model source | `modelscope` |
| `MODELSCOPE_OFFLINE` | Controlling local model lookup | `0` or `1` |
| `MODELSCOPE_CACHE` | Choosing a local model cache | `.cache/modelscope` |
| `PAPER2REPO_EVIDENCE_BACKEND` | Selecting `sqlite` or `mysql` | `sqlite` |
| `PAPER2REPO_MYSQL_URI` | Using the MySQL evidence backend | `mysql+pymysql://user:password@host:3306/db` |
| `PAPER2REPO_MYSQL_REQUIRED` | Making MySQL writes mandatory | `false` |
| `PAPER2REPO_EXECUTE_LLM_CODEGEN` | Enabling file-level LLM code generation | `true` |
| `PAPER2REPO_EXECUTE_LLM_REPAIR` | Enabling LLM-assisted repair | `true` |
| `PAPER2REPO_EXECUTE_LLM_REPAIR_AGENT` | Allowing the ReAct repair agent | `true` |
| `PAPER2REPO_EXECUTE_REGEN_FALLBACK` | Allowing regeneration as a repair fallback | `true` |

The execution feature flags are opt-in. Inspect the generated execution policy before enabling them for a real run.

For the optional MySQL setup, see [`docs/mysql_evidence_backend.md`](docs/mysql_evidence_backend.md) and [`docker-compose.mysql.yml`](docker-compose.mysql.yml).

## Evaluation and checks

Run the repository checks from the project root:

```bash
pytest -q
ruff check src tests
ruff format --check src tests
python -m compileall -q src tests bench scripts
```

The batch evaluation utilities accept an external, local-only PDF corpus in `test_paper/` and write all results to ignored directories:

```bash
python -m bench.run_all --dir test_paper --runs runs --limit 1 --source-type auto
python -m bench.report --runs runs --label-round r1 --out runs/BENCH_REPORT.r1.md
```

`scripts/run_pilot_benchmark.py --help` documents the named pilot cases used by the optional paper corpus. The repository does not publish those input papers or generated benchmark outputs.

## Production boundary

The code is intentionally honest about its current boundary. The default web job store is in-process, run artifacts live on the local filesystem, and the execution environment is trusted. A production deployment would add authenticated API access, a sandboxed command runner, distributed job execution, a shared checkpoint store, secret management, structured telemetry, and a stronger isolation policy around generated code.

## License

No license has been declared yet. Add an explicit license before accepting external contributions or redistributing the project.
