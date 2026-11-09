import asyncio
import json
import time
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from test_providers import response_json

from change_my_mind.api import create_app
from change_my_mind.chatgpt_auth import ISSUER, PLAN_SCOPE, RESOURCE, TOKEN, ChatGPTAuth
from change_my_mind.config import Settings
from change_my_mind.errors import BudgetExceeded, ProviderFailure
from change_my_mind.providers import ChatGPTPlanProvider, Gateway
from change_my_mind.schemas import ConsistencyReview, ModelConfig, RunCreate


@pytest.fixture
def identity():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    public["kid"] = "test"

    def sign(nonce, **overrides):
        return jwt.encode(
            {
                "iss": ISSUER,
                "aud": "oaiapp_test",
                "sub": "plus-user",
                "exp": int(time.time()) + 3600,
                "nonce": nonce,
                "email": "plus@example.org",
                **overrides,
            },
            private,
            algorithm="RS256",
            headers={"kid": "test"},
        )

    return public, sign


def metadata_routes(identity):
    public, _ = identity
    respx.get(ISSUER + "/.well-known/openid-configuration").respond(
        200,
        json={
            "issuer": ISSUER,
            "jwks_uri": ISSUER + "/jwks",
            "revocation_endpoint": ISSUER + "/revoke",
        },
    )
    respx.get(ISSUER + "/jwks").respond(200, json={"keys": [public]})


async def login(auth, sign, *, scope=PLAN_SCOPE, **claims):
    start = await auth.begin("http://127.0.0.1:8000/auth/callback")
    query = parse_qs(urlsplit(start["authorization_url"]).query)
    tokens = {
        "access_token": "oauth-access",
        "refresh_token": "refresh-1",
        "token_type": "Bearer",
        "id_token": sign(query["nonce"][0], **claims),
        "expires_in": 3600,
        "scope": scope,
    }
    route = respx.post(TOKEN).respond(200, json=tokens)
    result = await auth.complete(
        {"state": query["state"][0], "code": "one-time-code", "client_id": "oaiapp_test"}
    )
    return result, query, route


@respx.mock
async def test_oauth_pkce_identity_credentials_and_reauthorization(tmp_path, identity):
    metadata_routes(identity)
    auth = ChatGPTAuth(tmp_path)
    result, query, route = await login(auth, identity[1])
    assert query["client_id"] == ["dynamic_agent_client"] and query["agent_name_hint"] == ["Change My Mind"]
    assert query["code_challenge_method"] == ["S256"]
    form = parse_qs(route.calls.last.request.content.decode())
    assert form["client_id"] == ["oaiapp_test"] and "client_secret" not in form
    assert form["resource"] == [RESOURCE] and "code_verifier" in form
    assert result["active"]["plan_enabled"]
    assert "oauth-access" not in json.dumps(result)
    assert auth.path.stat().st_mode & 0o777 == 0o600
    assert auth.directory.stat().st_mode & 0o777 == 0o700
    again = await auth.begin("http://127.0.0.1:9000/auth/callback", result["active"]["id"])
    returning = parse_qs(urlsplit(again["authorization_url"]).query)
    assert returning["client_id"] == ["oaiapp_test"] and "agent_name_hint" not in returning
    assert returning["ext_agent_host_id"] == query["ext_agent_host_id"]
    assert "id_token_hint" not in returning
    with pytest.raises(ProviderFailure, match="state"):
        await auth.complete({"state": query["state"][0], "code": "replay"})
    await auth.aclose()


@respx.mock
@pytest.mark.parametrize(
    "bad", [{"nonce": "wrong"}, {"aud": "other-client"}, {"iss": "https://attacker.example"}, {"exp": 1}]
)
async def test_invalid_id_tokens_never_enable_plan(tmp_path, identity, bad):
    metadata_routes(identity)
    auth = ChatGPTAuth(tmp_path)
    # A signed token still must match every protocol binding.
    original_sign = identity[1]

    def sign(nonce, **kwargs):
        return original_sign(bad.get("nonce", nonce), **{k: v for k, v in bad.items() if k != "nonce"})

    with pytest.raises(ProviderFailure, match="identity validation"):
        await login(auth, sign)
    assert auth.status()["active"] is None
    assert "oauth-access" not in auth.path.read_text()
    await auth.aclose()


@respx.mock
async def test_scope_catalog_refresh_rotation_and_signout(tmp_path, identity):
    metadata_routes(identity)
    auth = ChatGPTAuth(tmp_path)
    result, _, _ = await login(auth, identity[1], scope="openid email")
    assert not result["active"]["plan_enabled"]
    with pytest.raises(ProviderFailure):
        auth.active_id()
    profile = auth.state["profiles"][result["active"]["id"]]
    profile["scopes"] = [PLAN_SCOPE]
    catalog = respx.get(RESOURCE + "/models").respond(
        200,
        json={
            "models": [
                {"slug": "account-model", "display_name": "Account model", "visibility": "list"},
                {"slug": "hidden", "visibility": "hide"},
            ]
        },
    )
    assert await auth.models() == [{"slug": "account-model", "display_name": "Account model"}]
    assert catalog.calls.last.request.headers["authorization"] == "Bearer oauth-access"
    profile["expires_at"] = 1
    refresh = respx.post(TOKEN).respond(
        200,
        json={
            "access_token": "access-2",
            "refresh_token": "refresh-2",
            "token_type": "Bearer",
            "expires_in": 3600,
        },
    )
    before_refresh = refresh.call_count
    tokens = await asyncio.gather(
        auth.access_token(result["active"]["id"]), auth.access_token(result["active"]["id"])
    )
    assert tokens == ["access-2", "access-2"] and refresh.call_count == before_refresh + 1
    assert "scope" not in parse_qs(refresh.calls.last.request.content.decode())
    assert profile["refresh_token"] == "refresh-2" and PLAN_SCOPE in profile["scopes"]
    revoke = respx.post(ISSUER + "/revoke").respond(200)
    assert (await auth.disconnect(result["active"]["id"]))["revocation_confirmed"]
    assert b"refresh-2" in revoke.calls.last.request.content
    assert profile["client_id"] == "oaiapp_test" and "access_token" not in profile
    await auth.aclose()


@respx.mock
async def test_streaming_plan_contract_and_terminal_quota_no_fallback(store):
    class Auth:
        async def access_token(self, profile):
            assert profile == "frozen-profile"
            return "plan-token"

    provider = ChatGPTPlanProvider(Auth(), "frozen-profile")
    output = ConsistencyReview(consistent=True, issues=[], recommendation_consistent=True)
    event = {"type": "response.completed", "sequence_number": 1, "response": response_json(output)}
    created = {
        "type": "response.created",
        "sequence_number": 0,
        "response": {**response_json(output), "status": "in_progress", "output": []},
    }
    stream = "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in [created, event])
    route = respx.post(RESOURCE + "/responses").respond(
        200, content=stream, headers={"Content-Type": "text/event-stream"}
    )
    result = await provider.generate("consistency", {}, ConsistencyReview, ModelConfig())
    assert result.output.consistent and result.input_tokens == 100
    body = json.loads(route.calls.last.request.content)
    assert body["store"] is False and body["stream"] is True and body["instructions"]
    assert "max_output_tokens" not in body and "max_tool_calls" not in body
    assert all(m["role"] != "system" for m in body["input"])
    assert route.calls.last.request.headers["authorization"] == "Bearer plan-token"
    route.respond(
        429, json={"error": {"code": "subscription_sharing_usage_limit_exceeded", "message": "App limit"}}
    )
    run = await store.create_run(RunCreate(question="Plan quota test"))
    gateway = Gateway(store, provider, provider)
    gateway.token_count = lambda _: 1
    before = route.call_count
    with pytest.raises(ProviderFailure, match="No API fallback") as error:
        await gateway.invoke(run, "quota", "consistency", {}, ConsistencyReview, final=True)
    assert error.value.uncertain and not error.value.retryable
    assert route.call_count == before + 1
    usage = await store.usage(run)
    assert len(usage) == 1 and usage[0]["cost_usd"] == 0
    assert usage[0]["details"]["billing"] == "chatgpt_plan"


async def test_plan_concurrent_request_reservation_and_final_reserve(store):
    run = await store.create_run(RunCreate(question="Plan concurrency test", request_limit=20))
    for i in range(7):
        await store.reserve(run, str(i), 0)
    results = await asyncio.gather(
        store.reserve(run, "a", 0), store.reserve(run, "b", 0), return_exceptions=True
    )
    assert sum(isinstance(r, BudgetExceeded) for r in results) == 1
    for i in range(12):
        await store.reserve(run, f"final-{i}", 0, final=True)
    with pytest.raises(BudgetExceeded):
        await store.reserve(run, "over", 0, final=True)
    assert (await store.get_run(run)).requests_used == 20


def test_api_key_cannot_enable_paid_calls_or_leak_credentials(tmp_path):
    application = create_app(Settings(data_dir=tmp_path, OPENAI_API_KEY="test-never-use", _env_file=None))
    with TestClient(application) as client:
        assert not client.get("/api/settings").json()["openai_configured"]
        assert (
            client.post(
                "/api/runs", json={"question": "Must not charge API", "model_config": {"provider": "openai"}}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/auth/chatgpt/start", json={}, headers={"Origin": "http://127.0.0.1:9999"}
            ).status_code
            == 403
        )
        result = client.post("/api/auth/chatgpt/start", json={})
        assert result.status_code == 200
        assert "code_verifier" not in result.text and "test-never-use" not in result.text
        assert client.get("/auth/callback?state=bad&code=do-not-echo").status_code == 400
