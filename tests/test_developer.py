import httpx

from change_my_mind.config import Settings
from change_my_mind.developer import create_developer_app


async def test_developer_can_read_private_outputs_without_changing_agent_visibility(store, run_id, tmp_path):
    private = await store.put(
        run_id,
        "invocation",
        "research:A",
        {"role": "A_debater_1", "output": {"synthesis": "Private research draft"}},
        team="A",
        stage="invocations",
    )
    app = create_developer_app(Settings(data_dir=tmp_path, _env_file=None))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get(f"/api/runs/{run_id}/trace")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert private.id in {r["id"] for r in response.json()["records"]}
            assert "Private research draft" in response.text
            assert not await store.records(run_id, audience="observer", kinds=["invocation"])
            assert not await store.records(run_id, audience="B", kinds=["invocation"])
            assert (await client.get("/", headers={"Origin": "https://elsewhere.example"})).status_code == 403
            assert (await client.get("/", headers={"Host": "public.example"})).status_code == 403
            assert (await client.get("/api/runs/not-a-run/trace")).status_code == 404
            assert "Developer trace" in (await client.get("/")).text
