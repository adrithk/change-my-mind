import pytest

from debate_bro.demo import DemoProvider, FixtureFetcher
from debate_bro.errors import ProviderFailure
from debate_bro.evidence import Evidence
from debate_bro.providers import Gateway
from debate_bro.reports import html_report, report_data
from debate_bro.schemas import Submission
from debate_bro.workflow import Workflow


def workflow(store, provider=None):
    provider = provider or DemoProvider()
    return Workflow(store, Gateway(store, provider, provider), Evidence(store, FixtureFetcher()))


async def test_complete_assessment_checks_corrects_and_links_evidence(store, run_id):
    await workflow(store).execute(run_id)
    run = await store.get_run(run_id)
    assert run.status == "completed"
    assert run.cost_usd == 0
    claims = [r for r in run.records if r.kind == "claim"]
    failed = [r for r in claims if "per evening" in r.data["text"]]
    assert failed and all(r.superseded and r.data["status"] == "contradicted" for r in failed)
    roles = {r.data["role"] for r in await store.records(run_id, kinds=["invocation"])}
    assert {f"{team}_debater_{number}" for team in ["A", "B"] for number in [1, 2, 3]} <= roles
    assert {"checker", "judge", "analyst"} <= roles
    assert any(r.kind == "response" and r.data["disposition"] == "qualify" for r in run.records)
    judgment = next(r for r in run.records if r.kind == "judgment")
    assert not judgment.stale and judgment.data["winner"] == "hybrid"
    assert all(
        [(await store.get_record(run_id, id_)).kind == "finding" for id_ in judgment.data["finding_ids"]]
    )
    exported = await report_data(store, run_id)
    html = html_report(exported)
    assert "Synthetic demonstration" in html and 'href="#claim_' in html
    assert all(f'id="{r.id}"' in html for r in run.records if r.kind == "passage")


async def test_paired_failure_keeps_successful_draft_private_then_resumes(store, run_id):
    class Interrupted(DemoProvider):
        failed = False
        a_openings = 0

        async def generate(self, role, context, schema, config):
            if schema is Submission and context.get("stage") == "openings":
                if role == "A_debater_1":
                    self.a_openings += 1
                if role == "B_debater_1" and not self.failed:
                    self.failed = True
                    raise ProviderFailure("Simulated interrupted opponent")
            return await super().generate(role, context, schema, config)

    provider = Interrupted()
    engine = workflow(store, provider)
    with pytest.raises(ProviderFailure):
        await engine.execute(run_id)
    a_draft = await store.by_key(run_id, "submission", "openings:A")
    assert a_draft and a_draft.visibility == "private"
    assert a_draft.id not in {r.id for r in await store.records(run_id, audience="B")}
    before = len(await store.records(run_id, kinds=["source"]))
    await store.set_status(run_id, "interrupted")
    await engine.execute(run_id)
    assert (await store.get_run(run_id)).status == "completed"
    assert provider.a_openings == 1
    assert (await store.get_record(run_id, a_draft.id)).visibility == "public"
    assert len(await store.records(run_id, kinds=["source"])) >= before
    assert len([r for r in await store.records(run_id, kinds=["submission"]) if r.key == "openings:A"]) == 1


async def test_report_escapes_untrusted_content(store, run_id):
    await store.put(run_id, "limitation", "injection", {"text": "<script>alert('x')</script>"}, public=True)
    html = html_report(await report_data(store, run_id))
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


async def test_partial_check_batch_resumes_without_changing_targets(store, run_id, monkeypatch):
    original = store.apply_check
    calls = 0

    async def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ProviderFailure("Interrupted between checks")
        return await original(*args, **kwargs)

    monkeypatch.setattr(store, "apply_check", interrupted)
    engine = workflow(store)
    with pytest.raises(ProviderFailure):
        await engine.execute(run_id)
    plans = await store.records(run_id, kinds=["check_plan"])
    assert plans and len(plans[0].data["claim_ids"]) > 1
    await engine.execute(run_id)
    assert (await store.get_run(run_id)).status == "completed"
    initial_calls = [
        r for r in await store.records(run_id, kinds=["invocation"]) if r.key == "check:initial:0:read:0"
    ]
    assert len(initial_calls) == 1


async def test_clarification_preserves_original_question_and_revises_frame(store, run_id):
    from debate_bro.errors import NeedsReview
    from debate_bro.schemas import Frame

    class Ambiguous(DemoProvider):
        async def generate(self, role, context, schema, config):
            result = await super().generate(role, context, schema, config)
            if schema is Frame and not context.get("clarifications"):
                result.output.clarification_needed = "Which decision-maker should the assessment address?"
            return result

    engine = workflow(store, Ambiguous())
    original_question = (await store.get_run(run_id)).question
    with pytest.raises(NeedsReview):
        await engine.execute(run_id)
    await store.put(
        run_id, "clarification", "answer", {"answer": "The local library administrator."}, public=True
    )
    await engine.execute(run_id)
    result = await store.get_run(run_id)
    assert result.status == "completed" and result.question == original_question
    frames = [r for r in result.records if r.kind == "frame"]
    assert len(frames) >= 2 and frames[1].version == 2
    assert frames[0].data["clarification_needed"]
    assert not frames[-1].data["clarification_needed"]


async def test_submission_recovers_dependency_write_before_commit_marker(store, run_id, monkeypatch):
    engine = workflow(store)
    original_link = store.link
    failed = False

    async def interrupt_link(topic, upstream, downstream):
        nonlocal failed
        target = await store.get_record(topic, downstream)
        if target.kind == "submission" and not failed:
            failed = True
            raise ProviderFailure("Interrupted artifact assembly")
        await original_link(topic, upstream, downstream)

    monkeypatch.setattr(store, "link", interrupt_link)
    with pytest.raises(ProviderFailure):
        await engine.execute(run_id)
    assert not await store.is_committed(run_id, "submission", "research:A:background")
    await engine.execute(run_id)
    assert (await store.get_run(run_id)).status == "completed"
    assert await store.is_committed(run_id, "submission", "research:A:background")
