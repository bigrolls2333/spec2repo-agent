import hashlib
import hmac
import os
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader
from sqlalchemy.exc import SQLAlchemyError

from spec2repo.config import Settings
from spec2repo.domain.agent import ApprovalDecision, ClarificationDecision
from spec2repo.domain.lifecycle import LifecycleError
from spec2repo.domain.spec import CoreSpec
from spec2repo.storage.agent import AgentRepository
from spec2repo.storage.database import make_engine
from spec2repo.storage.runs import IdempotencyConflict, RunNotFound, RunRepository
from spec2repo.storage.verification import VerificationRepository
from spec2repo.storage.workspaces import SnapshotError, Workspaces


def create_app(settings: Settings | None = None, *, engine=None, workspace_root=None) -> FastAPI:
    settings = settings or Settings.from_env()
    database = engine if engine is not None else make_engine(settings.database_url)
    repository = RunRepository(database)
    agent = AgentRepository(database)
    # Only fixed-length digests are retained in the authentication closure.
    identities = {
        owner: hashlib.sha256(key.encode()).digest() for owner, key in settings.api_keys.items()
    }
    header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def authenticate(key: Annotated[str | None, Depends(header)]):
        digest = hashlib.sha256((key or "").encode()).digest()
        for owner, expected in identities.items():
            if key is not None and hmac.compare_digest(digest, expected):
                return owner
        raise HTTPException(401, "Invalid API key")

    Owner = Annotated[str, Depends(authenticate)]

    @asynccontextmanager
    async def lifespan(_app):
        yield
        if engine is None:
            database.dispose()

    app = FastAPI(title="Spec2Repo", version="0.2.0", lifespan=lifespan)
    app.state.repository = repository

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, exc):
        # Avoid echoing request bodies, headers or Pydantic custom error contexts.
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_request",
                "fields": [
                    {"location": list(error["loc"]), "type": error["type"]}
                    for error in exc.errors()
                ],
            },
        )

    @app.exception_handler(RunNotFound)
    async def not_found(_request, _exc):
        return JSONResponse(status_code=404, content={"error": "run_not_found"})

    @app.exception_handler(IdempotencyConflict)
    async def idempotency_conflict(_request, _exc):
        return JSONResponse(status_code=409, content={"error": "idempotency_conflict"})

    @app.exception_handler(LifecycleError)
    async def lifecycle_conflict(_request, _exc):
        return JSONResponse(status_code=409, content={"error": "state_conflict"})

    @app.exception_handler(SQLAlchemyError)
    async def unavailable(_request, _exc):
        return JSONResponse(status_code=503, content={"error": "storage_unavailable"})

    @app.post("/api/runs", status_code=202)
    def create_run(
        spec: CoreSpec,
        owner: Owner,
        response: Response,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=128,
                pattern=r"^[A-Za-z0-9_.:-]+$",
            ),
        ],
    ):
        run, replayed = repository.create(owner, idempotency_key, spec)
        response.headers["Location"] = f"/api/runs/{run['id']}"
        response.headers["Idempotency-Replayed"] = str(replayed).lower()
        return run

    @app.get("/api/runs")
    def list_runs(owner: Owner, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
        return {"items": repository.list(owner, limit=limit, offset=offset)}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: UUID, owner: Owner):
        return repository.get(owner, str(run_id))

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: UUID, owner: Owner):
        return repository.cancel(owner, str(run_id))

    @app.get("/api/runs/{run_id}/events")
    def get_events(
        run_id: UUID,
        owner: Owner,
        after: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=200),
    ):
        rows = repository.events(owner, str(run_id), after=after, limit=limit)
        return {"items": rows, "next_after": rows[-1]["id"] if rows else after}

    @app.get("/api/runs/{run_id}/review")
    def review(run_id: UUID, owner: Owner):
        return agent.review(owner, str(run_id))

    @app.get("/api/runs/{run_id}/verifications")
    def verifications(run_id: UUID, owner: Owner):
        return {"items": VerificationRepository(database).reports(owner, str(run_id))}

    @app.get("/api/runs/{run_id}/artifacts")
    def artifacts(run_id: UUID, owner: Owner):
        return VerificationRepository(database).artifact(owner, str(run_id))

    @app.get("/api/runs/{run_id}/artifacts/repository.zip")
    def download(run_id: UUID, owner: Owner):
        artifact = VerificationRepository(database).artifact(owner, str(run_id))
        spaces = Workspaces(workspace_root or os.environ.get("SPEC2REPO_WORKSPACE_ROOT", ".runtime"))
        try:
            path = spaces.verify(artifact["snapshot_hash"])
        except (OSError, SnapshotError):
            raise HTTPException(503, "Artifact unavailable or integrity check failed") from None
        return FileResponse(path, media_type="application/zip", filename=f"{run_id}.zip")

    @app.post("/api/runs/{run_id}/approval")
    def approval(run_id: UUID, owner: Owner, decision: ApprovalDecision):
        return agent.decide(owner, str(run_id), decision)

    @app.post("/api/runs/{run_id}/clarification")
    def clarification(run_id: UUID, owner: Owner, decision: ClarificationDecision):
        return agent.clarify(owner, str(run_id), decision)

    return app
