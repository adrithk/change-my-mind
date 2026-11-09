import asyncio
import logging

from .chatgpt_auth import ChatGPTAuth
from .config import Settings
from .demo import DemoProvider, FixtureFetcher
from .errors import BudgetExceeded, Cancelled, NeedsReview, ProviderFailure
from .evidence import Evidence, HTTPSourceFetcher
from .providers import ChatGPTPlanProvider, Gateway, OpenAIProvider
from .schemas import RunCreate, SuiteComparison
from .store import Store, Suite
from .workflow import Workflow

logger = logging.getLogger(__name__)


class Jobs:
    def __init__(self, store: Store, settings: Settings, auth=None):
        self.store, self.settings = store, settings
        self.task = None
        self.wake = asyncio.Event()
        self.active_run_id = None
        self.active_task = None
        self.auth = auth or ChatGPTAuth(settings.data_dir)

    def workflow(self, run):
        demo = run.config["model_config"]["provider"] == "demo"
        kind = run.config["model_config"]["provider"]
        if demo:
            provider = DemoProvider()
        elif kind == "chatgpt":
            provider = ChatGPTPlanProvider(self.auth, run.config["chatgpt_profile_id"])
        elif self.settings.allow_paid_api:
            provider = OpenAIProvider(
                self.settings.openai_api_key.get_secret_value() if self.settings.openai_api_key else None
            )
        else:
            raise ProviderFailure("Separately billed API requests are disabled in this installation.")
        return Workflow(
            self.store,
            Gateway(self.store, provider, provider),
            Evidence(self.store, FixtureFetcher() if demo else HTTPSourceFetcher()),
        )

    async def start(self):
        await self.store.initialize()
        self.task = asyncio.create_task(self.work())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def cancel(self, run_id):
        await self.store.set_status(run_id, "cancelled")
        if self.active_run_id == run_id and self.active_task:
            self.active_task.cancel()
            try:
                await self.active_task
            except asyncio.CancelledError:
                pass

    async def work(self):
        while True:
            runs = await self.store.list_runs()
            queued = sorted([r for r in runs if r.status == "queued"], key=lambda r: r.created_at)
            if not queued:
                await self.finalize_suites()
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=1)
                except TimeoutError:
                    pass
                continue
            self.active_run_id = queued[0].id
            self.active_task = asyncio.create_task(self.execute(self.active_run_id))
            try:
                await self.active_task
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            finally:
                self.active_task, self.active_run_id = None, None
            await self.finalize_suites()

    async def execute(self, run_id):
        run = await self.store.get_run(run_id, records=False)
        engine = None
        try:
            engine = self.workflow(run)
            await self.store.set_status(run_id, "running")
            await engine.execute(run_id)
        except Cancelled:
            pass
        except BudgetExceeded as error:
            await self.store.set_status(run_id, "budget_exhausted", error=str(error))
        except NeedsReview as error:
            await self.store.set_status(run_id, "needs_review", error=str(error))
        except ProviderFailure as error:
            await self.store.set_status(
                run_id, "interrupted" if error.uncertain else "failed", error=str(error)
            )
        except asyncio.CancelledError:
            await self.store.mark_uncertain(run_id)
            if (await self.store.get_run(run_id, records=False)).status != "cancelled":
                await self.store.set_status(
                    run_id, "interrupted", error="Server stopped. Resume from the last durable stage."
                )
            raise
        except Exception as error:
            logger.exception("Workflow failed: %s", run_id)
            await self.store.set_status(run_id, "failed", error=f"{type(error).__name__}: {error}")
        finally:
            if hasattr(engine, "gateway"):
                await engine.gateway.aclose()

    async def create_suite(self, parent_id, kind, budget, request_limit=180):
        parent = await self.store.get_run(parent_id, records=False)
        if parent.status != "completed":
            raise ValueError("Complete the original assessment before starting a comparison suite")
        suite_id = await self.store.create_suite(parent_id, kind, budget, request_limit)
        variants = (
            [("full", "reverse"), ("full", "paraphrase"), ("full", "values")]
            if kind == "robustness"
            else [("single", "single"), ("synthesis", "synthesis"), ("reduced", "reduced")]
        )
        for mode, variant in variants:
            request = RunCreate(
                question=parent.question,
                model_config=parent.config["model_config"],
                budget_usd=parent.budget_usd,
                mode=mode,
                request_limit=parent.config.get("request_limit", 80),
            )
            await self.store.create_run(
                request,
                suite_id=suite_id,
                extra={
                    "variant": variant,
                    "parent_run_id": parent_id,
                    "reuse_parent": parent_id if variant == "values" else None,
                    "chatgpt_profile_id": parent.config.get("chatgpt_profile_id"),
                },
            )
        self.wake.set()
        return suite_id

    async def finalize_suites(self):
        from sqlalchemy import select

        from .metrics import metrics

        await self.store.refresh_suites()
        async with self.store.sessions() as session:
            pending = (
                await session.scalars(select(Suite).where(Suite.status.in_(["completed", "incomplete"])))
            ).all()
        for suite in pending:
            if "comparison" in suite.result:
                continue
            parent = await self.store.get_run(suite.parent_run_id)
            children = [await self.store.get_run(row["run_id"]) for row in suite.result.get("outcomes", [])]
            result = {
                **suite.result,
                "baseline": {"run_id": parent.id, "metrics": metrics(parent)},
                "variant_metrics": [{"run_id": child.id, "metrics": metrics(child)} for child in children],
            }
            if suite.status == "incomplete":
                result["comparison"] = {
                    "stability": "incomplete",
                    "explanation": "One or more variants did not complete; stability is not established.",
                }
            else:
                try:
                    context = {
                        "original_question": parent.question,
                        "kind": suite.kind,
                        "baseline": [r.data for r in parent.records if r.kind == "judgment"],
                        "variants": [
                            {
                                "variant": child.config.get("variant"),
                                "judgments": [r.data for r in child.records if r.kind == "judgment"],
                                "evidence_reused": bool(child.config.get("reuse_parent")),
                            }
                            for child in children
                        ],
                    }
                    engine = self.workflow(children[-1])
                    try:
                        comparison = await engine.invoke(
                            children[-1].id,
                            f"suite:{suite.id}",
                            "comparator",
                            context,
                            SuiteComparison,
                            final=True,
                        )
                    finally:
                        await engine.gateway.aclose()
                    result["comparison"] = comparison.model_dump()
                except Exception as error:
                    result["comparison"] = {
                        "stability": "incomplete",
                        "explanation": f"Comparison could not complete: {error}",
                    }
            async with self.store.lock, self.store.sessions.begin() as session:
                row = await session.get(Suite, suite.id)
                result["gross_cost_usd"] = sum(
                    [(await self.store.get_run(child.id, records=False)).cost_usd for child in children]
                )
                row.result = result
                if result["comparison"]["stability"] == "incomplete":
                    row.status = "incomplete"
