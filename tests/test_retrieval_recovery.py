import pytest

from change_my_mind.demo import DemoProvider, FixtureFetcher
from change_my_mind.errors import EvidenceError, NeedsReview
from change_my_mind.evidence import Evidence, Page
from change_my_mind.providers import Gateway, ModelResult
from change_my_mind.schemas import CheckBatch, EvidenceRequest, SearchHit, SearchResults, Submission
from change_my_mind.workflow import Workflow


def draft(source_id):
    return Submission(
        evidence_requests=[EvidenceRequest(source_id=source_id, query="Inspect the full methods")],
        synthesis="The evidence remains incomplete; no factual conclusion is established.",
        claims=[],
        arguments=[],
        objections=[],
        responses=[],
        strongest_opposing_points=[],
        gaps=[],
        next_queries=["Find an accessible primary report on another host"],
        stop_reason="continue",
    )


class SourceSeeking(DemoProvider):
    def __init__(self, source_id):
        self.source_id, self.calls = source_id, 0

    async def generate(self, role, context, schema, config):
        self.calls += 1
        if schema is CheckBatch:
            return ModelResult(
                CheckBatch(checks=[], evidence_requests=draft(self.source_id).evidence_requests)
            )
        return ModelResult(draft(self.source_id))


async def test_blocked_cached_research_recovers_without_another_model_call(store, run_id):
    source = await store.put(
        run_id,
        "source",
        "blocked",
        {"access_status": "unavailable", "limitation": "403 Forbidden"},
        team="A",
        stage="research",
    )
    provider = SourceSeeking(source.id)
    engine = Workflow(store, Gateway(store, provider, provider), Evidence(store, FixtureFetcher()))
    # Reproduce an interrupted old run with an already-paid-for structured response.
    await engine.gateway.invoke(run_id, "research:read:0", "A_debater_1", {}, Submission, team="A")
    await store.set_status(run_id, "needs_review", stage="research")
    context = await engine.context(run_id, team="A")
    assert context["evidence_omitted"] == 0
    output = await engine.invoke(run_id, "research", "A_debater_1", context, Submission, team="A")
    assert provider.calls == 1
    assert not output.evidence_requests and output.next_queries
    assert any(source.id in gap and "unavailable" in gap for gap in output.gaps)
    invocation = await store.by_key(run_id, "invocation", "research:read:0")
    assert invocation.data["output"]["evidence_requests"]  # Original audit output survives.
    assert not await store.records(run_id, audience="B")


async def test_stored_passages_are_supplied_once_then_repeated_requests_become_gaps(store, run_id):
    evidence = Evidence(store, FixtureFetcher())
    source = await evidence.retrieve(run_id, "https://fixtures.changemymind.test/pilot", team="A")
    provider = SourceSeeking(source.id)
    engine = Workflow(store, Gateway(store, provider, provider), evidence)
    output = await engine.invoke(run_id, "research", "A_debater_1", {}, Submission, team="A")
    assert provider.calls == 2  # One useful source read, no third call with duplicate text.
    assert not output.evidence_requests and output.gaps


@pytest.mark.parametrize("private", [False, True])
async def test_unknown_or_opponent_source_requests_cannot_be_downgraded_to_gaps(store, run_id, private):
    source_id = "source_invented"
    if private:
        source_id = (
            await store.put(
                run_id,
                "source",
                "opponent",
                {"access_status": "unavailable"},
                team="B",
            )
        ).id
    provider = SourceSeeking(source_id)
    engine = Workflow(store, Gateway(store, provider, provider), Evidence(store, FixtureFetcher()))
    with pytest.raises(EvidenceError):
        await engine.invoke(run_id, "research", "A_debater_1", {}, Submission, team="A")


async def test_final_checks_still_stop_on_unavailable_requested_evidence(store, run_id):
    source = await store.put(
        run_id,
        "source",
        "blocked",
        {"access_status": "unavailable"},
        public=True,
    )
    provider = SourceSeeking(source.id)
    engine = Workflow(store, Gateway(store, provider, provider), Evidence(store, FixtureFetcher()))
    with pytest.raises(NeedsReview):
        await engine.invoke(run_id, "check", "checker", {}, CheckBatch)
    assert provider.calls == 1


async def test_javascript_shell_cannot_become_retrieved_evidence(store, run_id):
    class ShellFetcher:
        async def fetch(self, url):
            text = "You need to enable JavaScript to run this app."
            return Page(url, "Blocked app", text.encode(), [("article", text)])

    source = await Evidence(store, ShellFetcher()).retrieve(run_id, "https://example.org/report")
    assert source.data["access_status"] == "unavailable"
    assert "JavaScript" in source.data["limitation"]
    assert not await store.records(run_id, kinds=["passage"])


async def test_blocked_first_results_use_remaining_hits_without_another_search(store, run_id):
    class Provider(DemoProvider):
        searches = 0

        async def discover(self, query, config):
            self.searches += 1
            return ModelResult(
                SearchResults(
                    hits=[
                        SearchHit(url=url, title="Fixture", description="Offline test")
                        for url in [
                            "https://blocked.test/1",
                            "https://blocked.test/2",
                            "https://blocked.test/3",
                            "https://fixtures.changemymind.test/pilot",
                            "https://fixtures.changemymind.test/finance",
                        ]
                    ]
                )
            )

    class Fetcher(FixtureFetcher):
        async def fetch(self, url):
            if "blocked.test" in url:
                raise EvidenceError("403 Forbidden")
            return await super().fetch(url)

    provider = Provider()
    engine = Workflow(store, Gateway(store, provider, provider), Evidence(store, Fetcher()))
    await engine.framing(run_id)
    await engine.research_task(run_id, "A", "background", "research")
    sources = await store.records(run_id, audience="A", kinds=["source"])
    assert provider.searches == 1
    assert sum(source.data["access_status"] == "retrieved" for source in sources) == 2
    assert await store.is_committed(run_id, "submission", "research:A:background")
