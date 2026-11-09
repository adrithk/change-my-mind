import httpx
import pytest
import respx

from debate_bro.demo import DemoProvider
from debate_bro.errors import ProviderFailure
from debate_bro.providers import Gateway, ModelResult, OpenAIProvider
from debate_bro.schemas import ConsistencyReview, ModelConfig, RunCreate, SearchResults


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
