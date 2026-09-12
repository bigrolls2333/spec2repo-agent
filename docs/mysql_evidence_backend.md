# MySQL Evidence Backend

Paper2Repo keeps every run self-contained by default:

- `planning/evidence_store.jsonl`
- `planning/evidence_refs.jsonl`
- `planning/evidence.sqlite`

The optional MySQL backend mirrors the same evidence data into a central database for production deployment, cross-run search, and Web/API querying. SQLite remains the local audit artifact, so benchmark and offline reproduction flows do not require MySQL.

## Install

```bash
pip install -e ".[mysql]"
```

## Local MySQL

```bash
docker compose -f docker-compose.mysql.yml up -d
```

Set `MYSQL_ROOT_PASSWORD` and `MYSQL_PASSWORD` in the local `.env` file before starting the container. The Compose file reads those values through variable interpolation.

## Environment

```bash
PAPER2REPO_EVIDENCE_BACKEND=mysql
PAPER2REPO_MYSQL_URI=mysql+pymysql://paper2repo:<password>@127.0.0.1:3306/paper2repo?charset=utf8mb4
PAPER2REPO_MYSQL_REQUIRED=false
```

`PAPER2REPO_MYSQL_REQUIRED=false` keeps local planning resilient: if MySQL is unavailable, Paper2Repo writes JSONL + SQLite and records a warning. Set it to `true` in production when a missing MySQL write should fail the run.

## Schema

The backend stores two tables:

- `evidence`: paper chunks and extracted evidence records.
- `evidence_ref`: field-level references from specs, validation results, plans, and human review items back to evidence.

Rows use Paper2Repo stable IDs as primary keys and MySQL `ON DUPLICATE KEY UPDATE` for idempotent re-runs and approval revisions.

## Web Query Behavior

The Web API checks `run_manifest.json` for `database_refs.evidence_mysql`. When present and `PAPER2REPO_MYSQL_URI` is configured, evidence queries use MySQL first. If MySQL is unavailable, the Web API falls back to the run-local SQLite or JSONL artifacts.
