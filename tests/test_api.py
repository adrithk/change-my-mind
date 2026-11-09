import time

from fastapi.testclient import TestClient

from change_my_mind.api import create_app
from change_my_mind.config import Settings


def test_local_api_complete_demo_and_exports(tmp_path):
    application = create_app(Settings(data_dir=tmp_path, OPENAI_API_KEY=None, _env_file=None))
    with TestClient(application) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        assert not client.get("/api/settings").json()["openai_configured"]
        real = client.post("/api/runs", json={"question": "A real topic without credentials"})
        assert real.status_code == 409 and "Continue with ChatGPT" in real.text
        paid = client.post(
            "/api/runs", json={"question": "Do not spend API funds", "model_config": {"provider": "openai"}}
        )
        assert paid.status_code == 403
        created = client.post(
            "/api/runs", json={"question": "Synthetic demonstration", "model_config": {"provider": "demo"}}
        )
        assert created.status_code == 201
        run_id = created.json()["id"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            run = client.get(f"/api/runs/{run_id}").json()
            if run["status"] in {"completed", "failed", "needs_review"}:
                break
            time.sleep(0.05)
        assert run["status"] == "completed", run.get("error")
        for format_ in ["html", "markdown", "json"]:
            result = client.get(f"/api/runs/{run_id}/report?format={format_}")
            assert result.status_code == 200 and "attachment" in result.headers["content-disposition"]
        events = client.get(f"/api/runs/{run_id}/events").text
        assert "data:" in events and '"status":"completed"' in events
        assert client.get(f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "nope"}).status_code == 400
        assert client.post(f"/api/runs/{run_id}/resume").status_code == 409
        assert (
            client.post(
                "/api/runs",
                json={"question": "External request"},
                headers={"Origin": "https://malicious.example"},
            ).status_code
            == 403
        )


def test_resume_recovers_legacy_research_loop_but_preserves_final_review_gates(tmp_path):
    from change_my_mind.schemas import RunCreate

    application = create_app(Settings(data_dir=tmp_path, OPENAI_API_KEY=None, _env_file=None))
    with TestClient(application) as client:
        # Exercise the recovery API without dispatching any jobs, even synthetic ones.
        client.portal.call(application.state.jobs.stop)

        async def saved_run(stage):
            run_id = await application.state.store.create_run(
                RunCreate(question="Saved recovery fixture", model_config={"provider": "demo"})
            )
            await application.state.store.set_status(
                run_id,
                "needs_review",
                stage=stage,
                error="Specialist still needs source details after the bounded retrieval allowance",
            )
            return run_id

        research = client.portal.call(saved_run, "research")
        judgment = client.portal.call(saved_run, "judgment")
        assert client.post(f"/api/runs/{research}/resume").status_code == 200
        assert client.post(f"/api/runs/{judgment}/resume").status_code == 409
