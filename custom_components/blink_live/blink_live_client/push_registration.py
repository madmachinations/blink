"""Android Firebase installation and FCM registration using async HTTP only."""

import asyncio
import base64
import json
import secrets
import time
from urllib.parse import parse_qs

import aiohttp

from .errors import ApiError, BlinkError
from .push_proto import MAX_FRAME, checkin_request, fields, integer

# Public client identifiers from Blink Android 60.0, not account credentials.
FIREBASE_API_KEY = "AIzaSyBcgwrpglzCZJPUNNeQQnpBbJAFlas_XAI"
FIREBASE_APP_ID = "1:351117235468:android:14fb3ef63df2a7c2"
FIREBASE_PROJECT = "blink-6c1ea"
FIREBASE_SENDER = "351117235468"
ANDROID_PACKAGE = "com.immediasemi.android.blink"
ANDROID_CERT = "65902c52c99a37502bdd4f342db2180c4c73b578"
FIS_ROOT = f"https://firebaseinstallations.googleapis.com/v1/projects/{FIREBASE_PROJECT}"
CHECKIN_URL = "https://android.clients.google.com/checkin"
REGISTER_URL = "https://android.clients.google.com/c2dm/register3"


class PushRegistration:
    def __init__(self, session, state, save):
        self.session = session
        self.state = state
        self.save = save

    async def _post(self, url, **kwargs) -> bytes:
        try:
            async with self.session.post(
                url,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=30),
                **kwargs,
            ) as response:
                if response.status >= 300:
                    raise ApiError(response.status)
                result = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    result.extend(chunk)
                    if len(result) > MAX_FRAME:
                        raise BlinkError("Oversized push registration response")
                return bytes(result)
        except (aiohttp.ClientError, TimeoutError):
            raise BlinkError("Push registration service unavailable") from None

    def _fis_headers(self):
        return {
            "x-goog-api-key": FIREBASE_API_KEY,
            "X-Android-Package": ANDROID_PACKAGE,
            "X-Android-Cert": ANDROID_CERT,
            "x-firebase-client": (
                "android-min-sdk/23 fire-core/20.0.0 fire-installations/17.0.0 fire-fcm/22.0.0"
            ),
            "x-firebase-client-log-type": "3",
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 14)",
        }

    async def _gcm_post(self, **kwargs):
        try:
            return await self._post(REGISTER_URL, **kwargs)
        except ApiError as error:
            if error.status in (401, 403):
                # Retry with refreshed installation auth and a fresh check-in,
                # rather than retrying the same rejected credentials forever.
                self.state.pop("fis_auth_token", None)
                self.state.pop("security_token", None)
                self.state.pop("fcm_token", None)
                await self.save(dict(self.state))
            raise

    async def _installation(self):
        state = self.state
        if state.get("fis_auth_token") and state.get("fis_expires_at", 0) > time.time() + 60:
            return
        data = None
        if state.get("fid") and state.get("fis_refresh_token"):
            try:
                raw = await self._post(
                    f"{FIS_ROOT}/installations/{state['fid']}/authTokens:generate",
                    headers={
                        **self._fis_headers(),
                        "Authorization": f"FIS_v2 {state['fis_refresh_token']}",
                    },
                    json={"installation": {"appId": FIREBASE_APP_ID, "sdkVersion": "a:17.0.0"}},
                )
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise BlinkError("Invalid Firebase authorization response")
            except ApiError as error:
                if error.status not in (401, 404):
                    raise
        if data is None:
            raw_fid = bytearray(secrets.token_bytes(17))
            raw_fid[0] = 0x70 | (raw_fid[0] & 15)
            fid = base64.urlsafe_b64encode(raw_fid).decode().rstrip("=")[:22]
            result = json.loads(
                await self._post(
                    f"{FIS_ROOT}/installations",
                    headers=self._fis_headers(),
                    json={
                        "fid": fid,
                        "appId": FIREBASE_APP_ID,
                        "authVersion": "FIS_v2",
                        "sdkVersion": "a:17.0.0",
                    },
                )
            )
            if not isinstance(result, dict):
                raise BlinkError("Invalid Firebase installation response")
            refresh = result.get("refreshToken")
            if not isinstance(refresh, str) or not refresh:
                raise BlinkError("Incomplete Firebase installation")
            state.update(fid=fid, fis_refresh_token=refresh)
            data = result.get("authToken")
            state.pop("fcm_token", None)
        if not isinstance(data, dict):
            raise BlinkError("Incomplete Firebase authorization")
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise BlinkError("Incomplete Firebase authorization")
        expiry = int(str(data.get("expiresIn", "3600s")).removesuffix("s"))
        if not 60 < expiry <= 604800:
            raise BlinkError("Invalid Firebase token lifetime")
        state.update(fis_auth_token=token, fis_expires_at=time.time() + expiry)
        await self.save(dict(state))

    async def ensure(self, *, reset_android: bool = False):
        """Resume partial registration, renew FIS auth and periodically refresh FCM."""
        state = self.state
        if reset_android:
            for key in ("android_id", "security_token", "fcm_token"):
                state.pop(key, None)
        await self._installation()
        if (
            not state.get("android_id")
            or not state.get("security_token")
            or state.get("checkin_at", 0) < time.time() - 7 * 86400
        ):
            response = fields(
                await self._post(
                    CHECKIN_URL,
                    data=checkin_request(state),
                    headers={"Content-Type": "application/x-protobuf"},
                )
            )
            android_id, security = integer(response, 7), integer(response, 8)
            if not android_id or not security:
                raise BlinkError("Incomplete Android check-in")
            if (android_id, security) != (state.get("android_id"), state.get("security_token")):
                state.pop("fcm_token", None)
            state.update(android_id=android_id, security_token=security, checkin_at=time.time())
            await self.save(dict(state))
        if state.get("fcm_token") and state.get("registered_at", 0) > time.time() - 86400:
            # A previous attempt may have acquired the token but failed its
            # final disk write. Never open the transport until this succeeds.
            await self.save(dict(state))
            return
        for attempt in range(10):
            result = parse_qs(
                (
                    await self._gcm_post(
                        headers={
                            "Authorization": (
                                f"AidLogin {state['android_id']}:{state['security_token']}"
                            )
                        },
                        data={
                            "X-subtype": FIREBASE_SENDER,
                            "device": str(state["android_id"]),
                            "app": ANDROID_PACKAGE,
                            "cert": ANDROID_CERT,
                            "app_ver": "1",
                            "X-app_ver": "1",
                            "X-osv": "29",
                            "X-cliv": "fiid-21.1.1",
                            "X-gmsv": "220217001",
                            "X-scope": "*",
                            "X-Goog-Firebase-Installations-Auth": state["fis_auth_token"],
                            "X-gms_app_id": FIREBASE_APP_ID,
                            "X-Firebase-Client": (
                                "android-min-sdk/23 fire-core/20.0.0 "
                                "fire-installations/17.0.0 fire-fcm/22.0.0"
                            ),
                            "X-Firebase-Client-Log-Type": "1",
                            "X-app_ver_name": "1",
                            "target_ver": "31",
                            "sender": FIREBASE_SENDER,
                        },
                    )
                ).decode()
            )
            token = result.get("token", [""])[0]
            if token:
                state.update(fcm_token=token, registered_at=time.time())
                await self.save(dict(state))
                return
            reason = result.get("Error", [""])[0]
            if reason == "PHONE_REGISTRATION_ERROR" and attempt < 9:
                await asyncio.sleep(1)
                continue
            if reason in ("AUTHENTICATION_FAILED", "INVALID_SENDER"):
                state.pop("fis_auth_token", None)
                state.pop("security_token", None)
                await self.save(dict(state))
            # Never include provider responses, which can contain credentials.
            raise BlinkError("FCM registration rejected")
