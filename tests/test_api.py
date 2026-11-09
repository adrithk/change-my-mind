import time

from fastapi.testclient import TestClient

from debate_bro.api import create_app
from debate_bro.config import Settings


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
