import asyncio
import base64
import hashlib
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp
import pytest
from aiohttp import web
from blink_live_client.auth import REDIRECT, Auth
from blink_live_client.errors import AuthError, BlinkError
from blink_live_client.storage import load_json, save_json


async def test_hosted_pkce_and_rotating_tokens(server, monkeypatch, memory):
    requests = []

    async def handler(request):
        data = dict(await request.post())
        requests.append(data)
        assert data["client_id"] == "android"
        assert data["scope"] == "client"
        return web.json_response(
            {
                "access_token": f"access-{len(requests)}",
                "refresh_token": f"refresh-{len(requests)}",
                "expires_in": 3600,
            }
        )

    monkeypatch.setattr("blink_live_client.auth.OAUTH_ROOT", await server(handler))
    async with aiohttp.ClientSession() as session:
        auth = Auth(session, save=memory.save)
        query = parse_qs(urlsplit(auth.begin_login()).query)
        assert query["code_challenge_method"] == ["S256"]
        callback = REDIRECT + "?" + urlencode({"code": "test-code", "state": query["state"][0]})
        await auth.complete_login(callback)
        verifier = requests[0]["code_verifier"]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        assert challenge.rstrip(b"=").decode() == query["code_challenge"][0]
        assert memory.value["refresh_token"] == "refresh-1"
        tokens = await asyncio.gather(*[auth.token(rejected="access-1") for _ in range(20)])
        assert set(tokens) == {"access-2"}
        assert len(requests) == 2
        assert memory.value["refresh_token"] == "refresh-2"
        with pytest.raises(AuthError):
            await auth.complete_login(callback)


@pytest.mark.parametrize(
    "query",
    [
        "code=x&state=wrong",
        "code=x&state={state}&state={state}",
        "code=x&code=y&state={state}",
        "error=denied&state={state}",
        "code=&state={state}",
    ],
)
async def test_reject_callback_state_and_duplicate_fields(query):
    async with aiohttp.ClientSession() as session:
        auth = Auth(session)
        state = parse_qs(urlsplit(auth.begin_login()).query)["state"][0]
        with pytest.raises(AuthError):
            await auth.complete_login(REDIRECT + "?" + query.format(state=state))


async def test_expiry_and_callback_origin():
    async with aiohttp.ClientSession() as session:
        auth = Auth(session)
        auth.begin_login()
        auth._pending = (*auth._pending[:2], time.monotonic() - 1)
        with pytest.raises(AuthError, match="expired"):
            await auth.complete_login(REDIRECT + "?code=x&state=x")
        query = parse_qs(urlsplit(auth.begin_login()).query)
        with pytest.raises(AuthError):
            await auth.complete_login(
                "https://evil.example/signin/callback?code=x&state=" + query["state"][0],
            )


async def test_temporary_refresh_failure_preserves_refresh_token(server, monkeypatch):
    async def handler(request):
        return web.Response(status=503, text="SECRET must never be included in an error")

    monkeypatch.setattr("blink_live_client.auth.OAUTH_ROOT", await server(handler))
    async with aiohttp.ClientSession() as session:
        auth = Auth(session, {"refresh_token": "refresh"})
        with pytest.raises(BlinkError) as error:
            await auth.token()
        assert "SECRET" not in str(error.value)
        assert auth.state["refresh_token"] == "refresh"


async def test_invalid_grant_requires_reauth(server, monkeypatch):
    async def handler(request):
        return web.json_response({"error": "invalid_grant"}, status=400)

    monkeypatch.setattr("blink_live_client.auth.OAUTH_ROOT", await server(handler))
    async with aiohttp.ClientSession() as session:
        with pytest.raises(AuthError):
            await Auth(session, {"refresh_token": "bad"}).token()


async def test_atomic_owner_only_state(tmp_path):
    path = tmp_path / "credentials" / "auth.json"
    await save_json(path, {"access_token": "secret"})
    assert await load_json(path) == {"access_token": "secret"}
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert list(path.parent.iterdir()) == [path]
