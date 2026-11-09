import httpx
import pytest
import respx

from change_my_mind.demo import DemoProvider
from change_my_mind.errors import ProviderFailure
from change_my_mind.providers import Gateway, ModelResult, OpenAIProvider
from change_my_mind.schemas import ConsistencyReview, ModelConfig, RunCreate, SearchResults


def response_json(output, *, search=False):
    content = [
        {
            "type": "message",
            "id": "msg_test",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": output.model_dump_json(), "annotations": []}],
        }
    ]
    if search:
        content.insert(
            0,
            {
                "type": "web_search_call",
                "id": "ws_test",
                "status": "completed",
                "action": {
                    "type": "search",
                    "queries": ["evidence"],
                    "sources": [{"type": "url", "url": "https://example.org/study"}],
                },
            },
        )
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": "gpt-6.1-sol",
        "output": content,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_tokens_details": {"cached_tokens": 50},
            "output_tokens_details": {"reasoning_tokens": 10},
        },
    }


@respx.mock
async def test_real_sdk_structured_output_and_search_provenance():
    provider = OpenAIProvider("unit-test-key")
    output = ConsistencyReview(consistent=True, issues=[], recommendation_consistent=True)
    route = respx.post("https://api.openai.com/v1/responses").mock(
        return_value=httpx.Response(200, json=response_json(output))
    )
    result = await provider.generate("consistency", {"question": "test"}, ConsistencyReview, ModelConfig())
    assert result.output.consistent and result.output_tokens == 20 and result.cached_tokens == 50
    import json

    body = json.loads(route.calls.last.request.content)
    assert body["model"] == "gpt-6.1-sol" and body["reasoning"]["effort"] == "medium"
    assert body["text"]["format"]["strict"]
    search = SearchResults(
        hits=[
            {"url": "https://example.org/study", "title": "Real returned URL"},
            {"url": "https://invented.example/fake", "title": "Fabrication"},
        ]
    )
    route.mock(return_value=httpx.Response(200, json=response_json(search, search=True)))
    result = await provider.discover("test original evidence", ModelConfig())
    assert [h.url for h in result.output.hits] == ["https://example.org/study"]
    assert result.searches == 1
    assert json.loads(route.calls.last.request.content)["max_tool_calls"] == 1
    await provider.client.close()


async def test_gateway_records_usage_and_reuses_completed_invocation(store):
    class Provider(DemoProvider):
        calls = 0

        async def generate(self, *args):
            self.calls += 1
            return ModelResult(
                ConsistencyReview(consistent=True, issues=[], recommendation_consistent=True),
                input_tokens=100,
                output_tokens=20,
                cached_tokens=50,
            )

    run = await store.create_run(RunCreate(question="Accounting test", model_config={"provider": "openai"}))
    provider = Provider()
    gateway = Gateway(store, provider, provider)
    gateway.token_count = lambda text: 100
    for _ in range(2):
        await gateway.invoke(run, "one", "consistency", {}, ConsistencyReview, final=True)
    assert provider.calls == 1
    assert len(await store.usage(run)) == 1
    assert (await store.get_run(run)).cost_usd == pytest.approx(0.000305)


async def test_uncertain_request_holds_reservation_and_does_not_retry(store):
    class Timeout(DemoProvider):
        calls = 0

        async def generate(self, *args):
            self.calls += 1
            raise ProviderFailure("Unknown remote outcome", uncertain=True)

    run = await store.create_run(
        RunCreate(question="Uncertain billing test", model_config={"provider": "openai"})
    )
    provider = Timeout()
    gateway = Gateway(store, provider, provider)
    gateway.token_count = lambda text: 100
    with pytest.raises(ProviderFailure):
        await gateway.invoke(run, "timeout", "checker", {}, ConsistencyReview)
    assert provider.calls == 1
    usage = (await store.usage(run))[0]
    assert usage["state"] == "uncertain" and usage["cost_usd"] == usage["reserved_usd"] > 0


async def test_empty_search_is_accounted_and_stops_before_research_claims(store):
    class EmptySearch(DemoProvider):
        async def discover(self, query, config):
            return ModelResult(SearchResults(hits=[]), input_tokens=100, output_tokens=20)

    run_id = await store.create_run(
        RunCreate(question="Empty search regression", model_config={"provider": "demo"})
    )
    provider = EmptySearch()
    gateway = Gateway(store, provider, provider)
    with pytest.raises(ProviderFailure, match="no verifiable source URLs"):
        await gateway.search(run_id, "lookup", "Original evidence")
    usage = await store.usage(run_id)
    assert len(usage) == 1 and usage[0]["state"] == "settled"
    assert usage[0]["details"]["input_tokens"] == 100
    assert "no verifiable" in usage[0]["details"]["error"]


async def test_successful_search_recovery_does_not_reuse_empty_legacy_result_or_repeat_calls(store):
    class Search(DemoProvider):
        calls = 0

        async def discover(self, query, config):
            self.calls += 1
            return ModelResult(SearchResults(hits=[{"url": "https://example.org/study", "title": "Source"}]))

    run_id = await store.create_run(
        RunCreate(question="Search cache recovery", model_config={"provider": "demo"})
    )
    await store.put(run_id, "invocation", "lookup", {"output": {"hits": []}})
    provider = Search()
    gateway = Gateway(store, provider, provider)
    for _ in range(2):
        result = await gateway.search(run_id, "lookup", "Primary sources")
        assert result.hits[0].url == "https://example.org/study"
    assert provider.calls == 1
    assert (await store.by_key(run_id, "invocation", "lookup")).data["output"]["hits"] == []


@respx.mock
async def test_malformed_model_output_is_an_uncertain_failure():
    provider = OpenAIProvider("unit-test-key")
    payload = response_json(ConsistencyReview(consistent=True, issues=[], recommendation_consistent=True))
    payload["output"][0]["content"][0]["text"] = "{}"
    respx.post("https://api.openai.com/v1/responses").mock(return_value=httpx.Response(200, json=payload))
    with pytest.raises(ProviderFailure) as error:
        await provider.generate("consistency", {}, ConsistencyReview, ModelConfig())
    assert error.value.uncertain
    await provider.client.close()
