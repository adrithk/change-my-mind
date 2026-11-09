"""Public-client OAuth for local personal projects. No API-key or Codex-token reuse.

Contract: https://developers.openai.com/siwc/token-sharing-open-source/sign-in
Credentials never enter application records, frontend storage, or reports.
"""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
import jwt

from .errors import ProviderFailure

ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN = ISSUER + "/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + PLAN_SCOPE
TERMINAL_REFRESH = {
    "invalid_grant",
    "invalid_refresh_token",
    "token_expired",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "refresh_token_reused",
}


class ChatGPTAuth:
    def __init__(self, data_dir: Path, *, client: httpx.AsyncClient | None = None):
        self.directory = data_dir / "chatgpt"
        self.path = self.directory / "credentials.json"
        self.http = client or httpx.AsyncClient(timeout=30, follow_redirects=False)
        self.lock = asyncio.Lock()
        self.pending: dict[str, dict] = {}
        self.discovery = None
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            os.chmod(self.path, 0o600)
        else:
            self.state = {"host_id": "urn:uuid:" + str(uuid.uuid4()), "profiles": {}, "active": None}

    def _save(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        fd, temporary = tempfile.mkstemp(dir=self.directory, prefix=".credentials-")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(self.state, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def status(self):
        profiles = []
        for identifier, profile in self.state["profiles"].items():
            profiles.append(
                {
                    "id": identifier,
                    "label": profile.get("email") or "Saved ChatGPT account",
                    "registration": profile["client_id"][-8:],
                    "connected": bool(profile.get("access_token")),
                    "plan_enabled": PLAN_SCOPE in profile.get("scopes", [])
                    and bool(profile.get("access_token")),
                }
            )
        active = next((p for p in profiles if p["id"] == self.state["active"]), None)
        return {"profiles": profiles, "active": active, "usage_url": "https://chatgpt.com/settings/usage"}

    def active_id(self):
        active = self.status()["active"]
        if not active or not active["plan_enabled"]:
            raise ProviderFailure("Continue with ChatGPT and enable plan usage before starting research.")
        return active["id"]

    async def begin(self, redirect_uri: str, profile_id: str | None = None):
        parsed = urlsplit(redirect_uri)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path != "/auth/callback":
            raise ProviderFailure("ChatGPT sign-in requires the 127.0.0.1 loopback callback.")
        async with self.lock:
            profile = self.state["profiles"].get(profile_id) if profile_id else None
            if profile_id and not profile:
                raise ProviderFailure("Unknown saved ChatGPT registration")
            state, nonce, verifier = (
                secrets.token_urlsafe(32),
                secrets.token_urlsafe(32),
                secrets.token_urlsafe(48),
            )
            self.pending = {
                key: value for key, value in self.pending.items() if value["expires"] > time.time()
            }
            self.pending[state] = {
                "nonce": nonce,
                "verifier": verifier,
                "redirect_uri": redirect_uri,
                "profile_id": profile_id,
                "expires": time.time() + 600,
            }
            self._save()
            params = {
                "client_id": profile["client_id"] if profile else "dynamic_agent_client",
                "ext_agent_host_id": self.state["host_id"],
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": SCOPES,
                "resource": RESOURCE,
                "state": state,
                "nonce": nonce,
                "code_challenge_method": "S256",
                "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("="),
            }
            if not profile:
                params["agent_name_hint"] = "Debate Bro"
            elif profile.get("email"):
                params["login_hint"] = profile["email"]
            # No ID token in a URL: use the account selector on reauthorization.
            if profile and PLAN_SCOPE not in profile.get("scopes", []):
                params["prompt"] = "consent"
            return {"authorization_url": AUTHORIZE + "?" + urlencode(params)}

    async def _metadata(self):
        if not self.discovery:
            response = await self.http.get(ISSUER + "/.well-known/openid-configuration")
            response.raise_for_status()
            metadata = response.json()
            if metadata.get("issuer") != ISSUER:
                raise ProviderFailure("Unexpected OpenAI identity issuer")
            for field in ("jwks_uri", "revocation_endpoint"):
                endpoint = urlsplit(metadata.get(field, ""))
                if endpoint.scheme != "https" or endpoint.hostname != "auth.openai.com":
                    raise ProviderFailure("Unexpected OpenAI identity endpoint")
            self.discovery = metadata
        return self.discovery

    async def _identity(self, token, client_id, nonce=None):
        metadata = await self._metadata()
        response = await self.http.get(metadata["jwks_uri"])
        response.raise_for_status()
        try:
            header = jwt.get_unverified_header(token)
            key = next(k for k in response.json()["keys"] if k.get("kid") == header.get("kid"))
            if header.get("alg") not in {"RS256", "ES256"}:
                raise ValueError("Unsupported identity signature")
            claims = jwt.decode(
                token,
                jwt.PyJWK.from_dict(key).key,
                algorithms=[header["alg"]],
                audience=client_id,
                issuer=ISSUER,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
            if nonce is not None and not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
                raise ValueError("Nonce mismatch")
            return claims
        except (jwt.PyJWTError, ValueError, KeyError, StopIteration) as error:
            raise ProviderFailure("ChatGPT identity validation failed; start a fresh sign-in.") from error

    @staticmethod
    def _token_data(tokens, previous=None):
        previous = previous or {}
        if not tokens.get("access_token") or tokens.get("token_type", "").lower() != "bearer":
            raise ProviderFailure("OpenAI did not return a usable bearer token")
        scopes = tokens.get("scope")
        return {
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token", previous.get("refresh_token")),
            "id_token": tokens.get("id_token", previous.get("id_token")),
            "scopes": scopes.split() if isinstance(scopes, str) else previous.get("scopes", []),
            "expires_at": time.time() + int(tokens.get("expires_in", 3600)),
        }

    async def complete(self, params: dict):
        # Consume once, even on cancellation/error; never exchange a replayed code.
        async with self.lock:
            attempt = self.pending.pop(params.get("state", ""), None)
            if not attempt or attempt["expires"] < time.time():
                raise ProviderFailure("Sign-in state expired or did not match. Start again.")
            if params.get("error"):
                raise ProviderFailure("ChatGPT sign-in was declined. No plan access was enabled.")
            previous = self.state["profiles"].get(attempt["profile_id"])
            client_id = previous["client_id"] if previous else params.get("client_id")
            if not client_id or client_id == "dynamic_agent_client" or not params.get("code"):
                raise ProviderFailure("OpenAI registration was incomplete; start sign-in again.")
            if previous and params.get("client_id", client_id) != client_id:
                raise ProviderFailure("Returned registration did not match the selected account.")
            profile_id = attempt["profile_id"] or "profile_" + uuid.uuid4().hex
            # Retain issued registration if code exchange fails; it must not be registered twice.
            if not previous:
                self.state["profiles"][profile_id] = {"client_id": client_id, "scopes": []}
                self._save()
            response = await self.http.post(
                TOKEN,
                data={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "code": params["code"],
                    "code_verifier": attempt["verifier"],
                    "redirect_uri": attempt["redirect_uri"],
                    "resource": RESOURCE,
                },
            )
            if response.is_error:
                raise ProviderFailure(
                    f"ChatGPT code exchange failed (HTTP {response.status_code}); sign in again using the saved registration."
                )
            tokens = response.json()
            identity = await self._identity(tokens.get("id_token", ""), client_id, attempt["nonce"])
            if previous and previous.get("subject") and identity["sub"] != previous["subject"]:
                raise ProviderFailure(
                    "Sign-in returned a different identity; the selected account was preserved."
                )
            self.state["profiles"][profile_id] = {
                "client_id": client_id,
                "issuer": identity["iss"],
                "subject": identity["sub"],
                "email": identity.get("email"),
                **self._token_data(tokens),
            }
            self.state["active"] = profile_id
            self._save()
            return self.status()

    async def select(self, profile_id):
        async with self.lock:
            if profile_id not in self.state["profiles"]:
                raise ProviderFailure("Unknown ChatGPT registration")
            self.state["active"] = profile_id
            self._save()
        return self.status()

    async def access_token(self, profile_id):
        async with self.lock:
            profile = self.state["profiles"].get(profile_id)
            if not profile or not profile.get("access_token") or PLAN_SCOPE not in profile.get("scopes", []):
                raise ProviderFailure(
                    "ChatGPT plan access is disabled; continue with the saved account again."
                )
            if profile["expires_at"] > time.time() + 60:
                return profile["access_token"]
            if not profile.get("refresh_token"):
                raise ProviderFailure("ChatGPT session expired; continue with the saved account again.")
            response = await self.http.post(
                TOKEN,
                data={
                    "grant_type": "refresh_token",
                    "client_id": profile["client_id"],
                    "refresh_token": profile["refresh_token"],
                    "resource": RESOURCE,
                },
            )
            if response.is_error:
                body = response.json()
                error = body.get("error", {})
                code = error.get("code") or error.get("type") if isinstance(error, dict) else error
                if code in TERMINAL_REFRESH:
                    self._clear(profile)
                    self._save()
                    raise ProviderFailure(
                        "ChatGPT session is no longer valid; sign in with the saved account again."
                    )
                raise ProviderFailure(
                    f"ChatGPT session refresh unavailable (HTTP {response.status_code}); try later."
                )
            tokens = response.json()
            if tokens.get("id_token"):
                identity = await self._identity(tokens["id_token"], profile["client_id"])
                if identity["sub"] != profile.get("subject"):
                    raise ProviderFailure("Refreshed ChatGPT identity did not match; credentials preserved.")
            replacement = self._token_data(tokens, profile)
            profile.update(replacement)
            self._save()
            if PLAN_SCOPE not in profile["scopes"]:
                raise ProviderFailure("ChatGPT plan usage permission is no longer enabled.")
            return profile["access_token"]

    async def models(self, profile_id=None):
        token = await self.access_token(profile_id or self.active_id())
        response = await self.http.get(RESOURCE + "/models", headers={"Authorization": "Bearer " + token})
        if response.is_error:
            raise ProviderFailure(
                f"ChatGPT model catalog unavailable (HTTP {response.status_code}). Check account eligibility and plan permissions."
            )
        models = response.json().get("models", [])
        return [
            {"slug": m["slug"], "display_name": m.get("display_name", m["slug"])}
            for m in models
            if m.get("visibility") == "list" and m.get("slug")
        ]

    @staticmethod
    def _clear(profile):
        for key in ("access_token", "refresh_token", "id_token", "expires_at"):
            profile.pop(key, None)

    async def disconnect(self, profile_id):
        async with self.lock:
            profile = self.state["profiles"].get(profile_id)
            if not profile:
                raise ProviderFailure("Unknown saved ChatGPT account")
            revoked = not bool(profile.get("refresh_token"))
            if not revoked:
                try:
                    metadata = await self._metadata()
                    for attempt in range(3):
                        response = await self.http.post(
                            metadata["revocation_endpoint"],
                            data={
                                "token": profile["refresh_token"],
                                "token_type_hint": "refresh_token",
                                "client_id": profile["client_id"],
                            },
                        )
                        if response.status_code == 200:
                            revoked = True
                            break
                        if response.status_code < 500:
                            break
                        await asyncio.sleep(2**attempt)
                except (httpx.HTTPError, ProviderFailure):
                    pass
            self._clear(profile)
            self._save()
            return {
                "revocation_confirmed": revoked,
                "message": "Signed out."
                if revoked
                else "Signed out locally. Remote revocation was not confirmed; disconnect this app in ChatGPT Settings.",
            }

    async def aclose(self):
        await self.http.aclose()
