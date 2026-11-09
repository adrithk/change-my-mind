import asyncio
import json
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .chatgpt_auth import ChatGPTAuth
from .config import PRICE_VERSION, PRICING, STAGES, TERMINAL, Settings
from .demo import DEMO_QUESTION
from .errors import EvidenceError, ProviderFailure
from .jobs import Jobs
from .model_policy import ROLE_PROFILES, freeze_roles
from .reports import html_report, markdown_report, report_data
from .schemas import (
    ChatGPTLogin,
    ChatGPTProfile,
    ClarificationRequest,
    RecordView,
    RunCreate,
    RunView,
    SuiteCreate,
)
from .store import Store


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    store = Store(settings.data_dir)
    auth = ChatGPTAuth(settings.data_dir)
    jobs = Jobs(store, settings, auth)

    @asynccontextmanager
    async def lifespan(app):
        await jobs.start()
        yield
        await jobs.stop()
        await auth.aclose()
        await store.close()

    app = FastAPI(title="Change My Mind", version="0.1.0", lifespan=lifespan)
    app.state.store, app.state.jobs = store, jobs
    app.state.chatgpt_auth = auth

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        if request.headers.get("host", "").split(":")[0] not in {"127.0.0.1", "localhost", "testserver"}:
            return Response("Local access only", status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
            return Response("Cross-origin access denied", status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(KeyError)
    async def missing(request, error):
        return Response("Topic not found", status_code=404)

    @app.exception_handler(EvidenceError)
    async def invalid_evidence(request, error):
        return Response(str(error), status_code=422)

    @app.exception_handler(ProviderFailure)
    async def provider_error(request, error):
        return Response(str(error), status_code=409)

    @app.get("/api/settings")
    async def get_settings():
        return {
            "openai_configured": settings.allow_paid_api and bool(settings.openai_api_key),
            "paid_api_enabled": settings.allow_paid_api,
            "chatgpt": auth.status(),
            "models": list(PRICING),
            "default_model": "gpt-6.1-sol",
            "role_profiles": ROLE_PROFILES,
            "default_budget_usd": 15,
            "target_cost": "$2–5 (unmeasured target)",
            "stages": STAGES,
            "pricing_version": PRICE_VERSION,
            "demo_question": DEMO_QUESTION,
            "subscription_credit_docs": "https://developers.openai.com/api/docs/guides/chatgpt-api-credits",
        }

    @app.post("/api/auth/chatgpt/start")
    async def chatgpt_start(body: ChatGPTLogin, request: Request):
        port = request.url.port or settings.port
        return await auth.begin(f"http://127.0.0.1:{port}/auth/callback", body.profile_id)

    @app.get("/auth/callback", include_in_schema=False)
    async def chatgpt_callback(request: Request):
        try:
            await auth.complete(dict(request.query_params))
            return RedirectResponse(
                "/?connected=1",
                status_code=303,
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
        except Exception as error:
            message = (
                str(error)
                if isinstance(error, ProviderFailure)
                else "ChatGPT sign-in could not complete. Return to the app and try the saved registration again."
            )
            return HTMLResponse(
                f"<h1>Connection needs attention</h1><p>{escape(message)}</p><a href='/'>Return to Change My Mind</a>",
                status_code=400,
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )

    @app.get("/api/auth/chatgpt/models")
    async def chatgpt_models():
        return {"models": await auth.models()}

    @app.post("/api/auth/chatgpt/select")
    async def chatgpt_select(request: ChatGPTProfile):
        if jobs.active_run_id:
            raise HTTPException(409, "Wait for or cancel the active run before switching accounts")
        return await auth.select(request.profile_id)

    @app.post("/api/auth/chatgpt/disconnect")
    async def chatgpt_disconnect(request: ChatGPTProfile):
        if jobs.active_run_id:
            await jobs.cancel(jobs.active_run_id)
        return await auth.disconnect(request.profile_id)

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.post("/api/runs", response_model=RunView, status_code=201)
    async def create_run(request: RunCreate):
        role_configs = freeze_roles(request.model_dump(by_alias=True))
        required_models = {values["model"] for values in role_configs.values()} or {
            request.model_config_.model
        }
        if request.model_config_.provider == "openai":
            if not settings.allow_paid_api:
                raise HTTPException(
                    403, "Separately billed API requests are disabled. Use Continue with ChatGPT."
                )
            if not settings.openai_api_key:
                raise HTTPException(
                    422, "Set OPENAI_API_KEY in the backend .env before starting real research."
                )
            if required_models - PRICING.keys():
                raise HTTPException(422, "Model has no verified price configuration")
        if request.model_config_.provider == "demo":
            request.question = DEMO_QUESTION
        extra = {}
        if request.model_config_.provider == "chatgpt":
            profile_id = auth.active_id()
            available = await auth.models(profile_id)
            missing = required_models - {m["slug"] for m in available}
            if missing:
                raise HTTPException(
                    422,
                    "This role configuration requires unavailable models: "
                    + ", ".join(sorted(missing))
                    + ". No model or billing fallback was attempted.",
                )
            extra = {"chatgpt_profile_id": profile_id, "billing": "chatgpt_plan"}
        run_id = await store.create_run(request, extra=extra)
        jobs.wake.set()
        return await store.get_run(run_id)

    @app.get("/api/runs", response_model=list[RunView])
    async def list_runs():
        return await store.list_runs()

    @app.get("/api/runs/{run_id}", response_model=RunView)
    async def get_run(run_id: str):
        return await store.get_run(run_id)

    @app.get("/api/runs/{run_id}/records", response_model=list[RecordView])
    async def records(run_id: str, kind: str | None = None):
        await store.get_run(run_id, records=False)
        return await store.records(run_id, audience="observer", kinds=[kind] if kind else None)

    @app.get("/api/runs/{run_id}/records/{record_id}", response_model=RecordView)
    async def record(run_id: str, record_id: str):
        return await store.get_record(run_id, record_id, audience="observer")

    @app.get("/api/runs/{run_id}/sources/{source_id}/text", response_class=Response)
    async def source_text(run_id: str, source_id: str):
        source = await store.get_record(run_id, source_id, audience="observer")
        if source.kind != "source" or not source.data.get("text_path"):
            raise HTTPException(404, "Full text unavailable")
        return Response(
            await asyncio.to_thread(Path(source.data["text_path"]).read_text), media_type="text/plain"
        )

    @app.get("/api/runs/{run_id}/events", response_model=None)
    async def events(run_id: str, request: Request, after: int = 0):
        await store.get_run(run_id, records=False)
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(400, "Invalid event cursor")

        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                batch = await store.events(run_id, cursor)
                for item in batch:
                    cursor = item.id
                    yield f"id: {item.id}\ndata: {item.model_dump_json()}\n\n"
                run = await store.get_run(run_id, records=False)
                if run.status in TERMINAL | {"interrupted"} and not batch:
                    return
                yield ": keepalive\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/runs/{run_id}/cancel", response_model=RunView)
    async def cancel(run_id: str):
        run = await store.get_run(run_id, records=False)
        if run.status == "completed":
            raise HTTPException(409, "A completed assessment cannot be cancelled")
        await jobs.cancel(run_id)
        return await store.get_run(run_id)

    @app.post("/api/runs/{run_id}/resume", response_model=RunView)
    async def resume(run_id: str):
        run = await store.get_run(run_id, records=False)
        framing_recovery = (
            run.status == "needs_review"
            and run.stage == "framing"
            and run.error == "Specialist still needs source details after the bounded retrieval allowance"
        )
        if run.status not in {"failed", "interrupted", "cancelled"} and not framing_recovery:
            raise HTTPException(
                409, "This run cannot be resumed; budget or evidence-review limits require a new run"
            )
        await store.set_status(run_id, "queued")
        jobs.wake.set()
        return await store.get_run(run_id)

    @app.post("/api/runs/{run_id}/clarify", response_model=RunView)
    async def clarify(run_id: str, request: ClarificationRequest):
        run = await store.get_run(run_id, records=False)
        frames = await store.records(run_id, audience="observer", kinds=["frame"])
        if run.status != "needs_review" or not frames or not frames[-1].data.get("clarification_needed"):
            raise HTTPException(409, "This assessment is not waiting for question clarification")
        from .store import uid

        await store.put(
            run_id, "clarification", uid("answer"), {"answer": request.answer}, stage="framing", public=True
        )
        await store.set_status(run_id, "queued")
        jobs.wake.set()
        return await store.get_run(run_id)

    @app.get("/api/runs/{run_id}/usage")
    async def usage(run_id: str):
        await store.get_run(run_id, records=False)
        return await store.usage(run_id)

    @app.get("/api/runs/{run_id}/report", response_model=None)
    async def report(run_id: str, format: str = "html"):
        data = await report_data(store, run_id)
        if format == "json":
            content, media, extension = (
                json.dumps(data, indent=2, ensure_ascii=False),
                "application/json",
                "json",
            )
        elif format == "markdown":
            content, media, extension = markdown_report(data), "text/markdown", "md"
        elif format == "html":
            content, media, extension = html_report(data), "text/html", "html"
        else:
            raise HTTPException(422, "Choose html, markdown, or json")
        return Response(
            content,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="{run_id}.{extension}"'},
        )

    @app.post("/api/runs/{run_id}/robustness")
    async def robustness(run_id: str, request: SuiteCreate):
        try:
            return {
                "suite_id": await jobs.create_suite(
                    run_id, "robustness", request.budget_usd, request.request_limit
                )
            }
        except ValueError as error:
            raise HTTPException(409, str(error))

    @app.post("/api/runs/{run_id}/evaluations")
    async def evaluations(run_id: str, request: SuiteCreate):
        try:
            return {
                "suite_id": await jobs.create_suite(
                    run_id, "evaluation", request.budget_usd, request.request_limit
                )
            }
        except ValueError as error:
            raise HTTPException(409, str(error))

    if (settings.frontend_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=settings.frontend_dir / "assets"), name="assets")

    @app.get("/{path:path}", response_model=None, include_in_schema=False)
    async def frontend(path: str):
        if path.startswith("api/"):
            raise HTTPException(404, "Unknown API endpoint")
        index = settings.frontend_dir / "index.html"
        if index.is_file():
            return FileResponse(index)
        return HTMLResponse(
            "<h1>Change My Mind backend is ready</h1><p>Build the frontend with <code>npm run build</code> in frontend/, or use the development server.</p><a href='/docs'>API documentation</a>"
        )

    return app
