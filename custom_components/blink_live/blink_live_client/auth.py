"""Blink hosted Android OAuth profile with PKCE and one-shot callback validation."""

import asyncio
import base64
import hashlib
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp

from .errors import ApiError, AuthError, BlinkError

OAUTH_ROOT = "https://api.oauth.blink.com"
REDIRECT = "https://applinks.blink.com/signin/callback"
USER_AGENT = "Blink/57.1 (samsung SM-G998B; Android 14)"
SaveTokens = Callable[[dict], Awaitable[None]]


class Auth:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        state: dict | None = None,
        save: SaveTokens | None = None,
    ) -> None:
        self.session = session
        self.state = dict(state or {})
        self.state.setdefault("hardware_id", str(uuid.uuid4()))
        self.save = save
        self._lock = asyncio.Lock()
        self._pending: tuple[str, str, float] | None = None

    def begin_login(self) -> str:
        verifier = secrets.token_urlsafe(48)
        state = secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        self._pending = (state, verifier, time.monotonic() + 900)
        query = {
            "client_id": "android",
            "redirect_uri": REDIRECT,
            "response_type": "code",
            "scope": "client",
            "state": state,
            "code_challenge": challenge.rstrip(b"=").decode(),
            "code_challenge_method": "S256",
            "prompt": "login",
            "hardware_id": self.state["hardware_id"],
            "app_brand": "blink",
            "app_version": "Version 57.1",
            "device_brand": "Python",
            "device_model": "Blink Live",
            "device_os_version": "Android 14",
            "dark_mode": "false",
        }
        return f"{OAUTH_ROOT}/oauth/v2/authorize?{urlencode(query)}"

    async def complete_login(self, callback: str) -> None:
        pending, self._pending = self._pending, None
        if pending is None or pending[2] <= time.monotonic():
            raise AuthError("Sign-in expired; start again")
        if len(callback) > 4096 or any(ord(c) <= 32 for c in callback):
            raise AuthError("Invalid callback URL; start sign-in again")
        try:
            parsed = urlsplit(callback)
            query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
            valid = (
                f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == REDIRECT
                and not parsed.fragment
                and len(query.get("state", [])) == 1
                and secrets.compare_digest(query["state"][0], pending[0])
                and len(query.get("code", [])) == 1
                and bool(query["code"][0])
                and "error" not in query
            )
        except ValueError:
            valid = False
        if not valid:
            raise AuthError("Invalid callback URL or OAuth state; start sign-in again")
        async with self._lock:
            await self._exchange(
                {
                    "grant_type": "authorization_code",
                    "code": query["code"][0],
                    "code_verifier": pending[1],
                    "redirect_uri": REDIRECT,
                }
            )

    async def token(self, rejected: str | None = None) -> str:
        async with self._lock:
            token = self.state.get("access_token")
            if token and self.state.get("expires_at", 0) > time.time() + 60:
                if rejected is None or rejected != token:
                    return token
            if not self.state.get("refresh_token"):
                raise AuthError("Sign in to Blink Live")
            await self._exchange(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": self.state["refresh_token"],
                }
            )
            return self.state["access_token"]

    async def persist(self) -> None:
        if self.save:
            await self.save(dict(self.state))

    async def _exchange(self, fields: dict) -> None:
        form = {
            "client_id": "android",
            "scope": "client",
            "app_brand": "blink",
            "hardware_id": self.state["hardware_id"],
            **fields,
        }
        try:
            async with self.session.post(
                f"{OAUTH_ROOT}/oauth/token",
                data=form,
                headers={"User-Agent": USER_AGENT},
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status in (400, 401, 403):
                    raise AuthError("Blink rejected sign-in; authenticate again")
                if response.status != 200:
                    raise ApiError(response.status)
                if len(body := await response.content.read(1024 * 1024 + 1)) > 1024 * 1024:
                    raise BlinkError("Oversized OAuth response")
                import json

                data = json.loads(body)
        except (aiohttp.ClientError, TimeoutError):
            raise BlinkError("Blink authentication service unavailable") from None
        except (ValueError, TypeError):
            raise BlinkError("Invalid OAuth response") from None
        if not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
            raise BlinkError("Invalid OAuth token response")
        refresh = data.get("refresh_token") or self.state.get("refresh_token")
        if not refresh:
            raise BlinkError("Missing OAuth refresh token")
        try:
            expiry = float(data.get("expires_in", 3600))
            if not 0 < expiry <= 31_536_000:
                raise ValueError
        except (ValueError, TypeError):
            raise BlinkError("Invalid OAuth token lifetime") from None
        self.state.update(
            access_token=data["access_token"],
            refresh_token=refresh,
            expires_at=time.time() + expiry,
        )
        await self.persist()
