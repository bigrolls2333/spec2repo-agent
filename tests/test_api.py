from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from spec2repo.api.app import create_app
from spec2repo.domain.lifecycle import RunStatus
from spec2repo.storage.models import IdempotencyKey, OutboxEvent, Run, RunEvent


def headers(settings, owner="alice", key="request-1"):
    return {"X-API-Key": settings.api_keys[owner], "Idempotency-Key": key}


def test_idempotent_create_and_atomic_records(db_engine, api_settings, spec_payload):
    with TestClient(create_app(api_settings, engine=db_engine)) as client:
        first = client.post("/api/runs", json=spec_payload, headers=headers(api_settings))
        assert first.status_code == 202, first.text
        assert first.headers["Idempotency-Replayed"] == "false"
        for _ in range(9):
            replay = client.post("/api/runs", json=spec_payload, headers=headers(api_settings))
            assert replay.status_code == 202
            assert replay.json()["id"] == first.json()["id"]
            assert replay.headers["Idempotency-Replayed"] == "true"
        spec_payload["title"] += " changed"
        assert (
            client.post("/api/runs", json=spec_payload, headers=headers(api_settings)).status_code
            == 409
        )
    with Session(db_engine) as session:
        for model in [Run, IdempotencyKey, RunEvent, OutboxEvent]:
            assert session.scalar(select(func.count()).select_from(model)) == 1


def test_identity_isolation_on_all_run_routes(db_engine, api_settings, spec_payload):
    with TestClient(create_app(api_settings, engine=db_engine)) as client:
        run = client.post("/api/runs", json=spec_payload, headers=headers(api_settings)).json()
        bob = headers(api_settings, "bob")
        for path in [f"/api/runs/{run['id']}", f"/api/runs/{run['id']}/events"]:
            assert client.get(path, headers=bob).status_code == 404
        assert client.post(f"/api/runs/{run['id']}/cancel", headers=bob).status_code == 404
        assert client.get("/api/runs", headers=bob).json()["items"] == []
        other = client.post("/api/runs", json=spec_payload, headers=bob)
        assert other.status_code == 202 and other.json()["id"] != run["id"]
        assert len(client.get("/api/runs", headers=headers(api_settings)).json()["items"]) == 1


def test_app_recreation_does_not_lose_task(db_engine, api_settings, spec_payload):
    with TestClient(create_app(api_settings, engine=db_engine)) as client:
        run = client.post("/api/runs", json=spec_payload, headers=headers(api_settings)).json()
    db_engine.dispose()  # New connections, no JobStore or object identity persistence.
    with TestClient(create_app(api_settings, engine=db_engine)) as restarted:
        fetched = restarted.get(f"/api/runs/{run['id']}", headers=headers(api_settings))
        assert fetched.status_code == 200 and fetched.json()["spec_hash"] == run["spec_hash"]


def test_cancel_is_repeatable_and_events_are_cursor_based(db_engine, api_settings, spec_payload):
    with TestClient(create_app(api_settings, engine=db_engine)) as client:
        run = client.post("/api/runs", json=spec_payload, headers=headers(api_settings)).json()
        path = f"/api/runs/{run['id']}"
        first = client.post(path + "/cancel", headers=headers(api_settings))
        second = client.post(path + "/cancel", headers=headers(api_settings))
        assert first.json() == second.json()
        assert first.json()["status"] == "CANCELLED" and first.json()["version"] == 1
        page1 = client.get(path + "/events?limit=1", headers=headers(api_settings)).json()
        page2 = client.get(
            path + f"/events?after={page1['next_after']}", headers=headers(api_settings)
        ).json()
        assert len(page1["items"]) == len(page2["items"]) == 1
        assert page2["items"][0]["event_type"] == "run.status_changed"


def test_completed_run_cannot_be_cancelled(db_engine, api_settings, spec_payload):
    app = create_app(api_settings, engine=db_engine)
    with TestClient(app) as client:
        run = client.post("/api/runs", json=spec_payload, headers=headers(api_settings)).json()
        app.state.repository.change_status(
            "alice", run["id"], RunStatus.RUNNING, expected_version=0
        )
        app.state.repository.change_status(
            "alice",
            run["id"],
            RunStatus.SUCCEEDED,
            expected_version=1,
            verification_passed=True,
        )
        assert (
            client.post(f"/api/runs/{run['id']}/cancel", headers=headers(api_settings)).status_code
            == 409
        )


def test_blocked_spec_persists_without_scheduling(db_engine, api_settings, spec_payload):
    spec_payload["open_questions"] = [{"id": "Q-1", "question": "允许取消已完成预约吗？"}]
    with TestClient(create_app(api_settings, engine=db_engine)) as client:
        response = client.post("/api/runs", json=spec_payload, headers=headers(api_settings))
        assert response.status_code == 202
        assert response.json()["status"] == "WAITING_CLARIFICATION"
    with Session(db_engine) as session:
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_bad_input_auth_and_pagination(db_engine, api_settings, spec_payload):
    with TestClient(create_app(api_settings, engine=db_engine)) as client:
        assert client.get("/api/runs").status_code == 401
        assert client.get("/api/runs", headers={"X-API-Key": "incorrect"}).status_code == 401
        assert client.get("/api/runs?limit=101", headers=headers(api_settings)).status_code == 422
        assert client.get("/api/runs?offset=-1", headers=headers(api_settings)).status_code == 422
        assert (
            client.post(
                "/api/runs",
                json=spec_payload,
                headers={"X-API-Key": api_settings.api_keys["alice"]},
            ).status_code
            == 422
        )
        spec_payload["owner_id"] = "bob"
        rejected = client.post("/api/runs", json=spec_payload, headers=headers(api_settings))
        assert rejected.status_code == 422
        assert "input" not in rejected.json()


def test_idempotency_keys_are_case_sensitive(db_engine, api_settings, spec_payload):
    with TestClient(create_app(api_settings, engine=db_engine)) as client:
        a = client.post(
            "/api/runs", json=spec_payload, headers=headers(api_settings, key="Request-A")
        )
        b = client.post(
            "/api/runs", json=spec_payload, headers=headers(api_settings, key="request-a")
        )
        assert a.json()["id"] != b.json()["id"]


def test_no_unrestricted_state_update_route(db_engine, api_settings):
    with TestClient(create_app(api_settings, engine=db_engine)) as client:
        paths = client.get("/openapi.json").json()["paths"]
        assert all("status" not in path for path in paths)
        assert "/api/runs" in paths
