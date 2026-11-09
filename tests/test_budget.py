import asyncio

import pytest

from debate_bro.errors import BudgetExceeded, Cancelled
from debate_bro.schemas import RunCreate
from debate_bro.store import Store


async def test_concurrent_reservations_cannot_overspend(store):
    run = await store.create_run(RunCreate(question="Budget concurrency test", budget_usd=1))
    results = await asyncio.gather(
        store.reserve(run, "one", 0.7, final=True),
        store.reserve(run, "two", 0.7, final=True),
        return_exceptions=True,
    )
    assert sum(isinstance(r, BudgetExceeded) for r in results) == 1
    view = await store.get_run(run)
    assert view.reserved_usd == 0.7


async def test_finalization_reserve_and_equal_team_allowances(store, run_id):
    with pytest.raises(BudgetExceeded):
        await store.reserve(run_id, "too_much_research", 13.1)
    amount = (15 - 2) * 0.35
    await store.reserve(run_id, "a", amount, team="A")
    await store.reserve(run_id, "b", amount, team="B")
    for team in ["A", "B"]:
        with pytest.raises(BudgetExceeded):
            await store.reserve(run_id, f"over:{team}", 0.01, team=team)


async def test_aggregate_suite_budget_is_enforced(store, run_id):
    suite = await store.create_suite(run_id, "robustness", 1)
    children = [await store.create_run(RunCreate(question="Suite child"), suite_id=suite) for _ in range(2)]
    await store.reserve(children[0], "one", 0.7, final=True)
    with pytest.raises(BudgetExceeded, match="Aggregate"):
        await store.reserve(children[1], "two", 0.7, final=True)


async def test_restart_retains_unknown_charges_and_marks_interrupted(tmp_path):
    store = Store(tmp_path)
    await store.initialize()
    run = await store.create_run(RunCreate(question="A restart test"))
    await store.set_status(run, "running")
    await store.reserve(run, "pending", 0.4)
    await store.close()
    store = Store(tmp_path)
    await store.initialize()
    view = await store.get_run(run)
    assert view.status == "interrupted"
    assert view.cost_usd == 0.4 and view.reserved_usd == 0
    assert (await store.usage(run))[0]["state"] == "uncertain"
    await store.close()


async def test_cancel_blocks_new_calls(store, run_id):
    await store.set_status(run_id, "cancelled")
    with pytest.raises(Cancelled):
        await store.reserve(run_id, "after_cancel", 0.1)


async def test_job_cancellation_keeps_worker_available(store, run_id, tmp_path):
    from debate_bro.config import Settings
    from debate_bro.jobs import Jobs

    started = asyncio.Event()

    class SlowWorkflow:
        async def execute(self, id_):
            if id_ == run_id:
                started.set()
                await asyncio.Event().wait()
            else:
                await store.set_status(id_, "completed", stage="complete")

    jobs = Jobs(store, Settings(data_dir=tmp_path, OPENAI_API_KEY=None, _env_file=None))
    jobs.workflow = lambda run: SlowWorkflow()
    await jobs.start()
    try:
        await asyncio.wait_for(started.wait(), 3)
        await jobs.cancel(run_id)
        assert (await store.get_run(run_id)).status == "cancelled"
        second = await store.create_run(
            RunCreate(question="Another queued question", model_config={"provider": "demo"})
        )
        jobs.wake.set()
        for _ in range(100):
            if (await store.get_run(second)).status == "completed":
                break
            await asyncio.sleep(0.02)
        assert (await store.get_run(second)).status == "completed"
    finally:
        await jobs.stop()
