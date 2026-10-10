from __future__ import annotations

import asyncio
import ipaddress
import math
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import aiohttp


API = "https://open.tiktokapis.com"
CHUNK_SIZE = 10_000_000
MAX_SINGLE_CHUNK = 64_000_000
MIME_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}


class TikTokError(Exception):
    pass


def authorization_url(
    client_key: str, redirect_uri: str, state: str, scopes: tuple[str, ...] = ("video.upload",)
) -> str:
    return "https://www.tiktok.com/v2/auth/authorize/?" + urlencode(
        {
            "client_key": client_key,
            "scope": ",".join(scopes),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )


def chunk_ranges(size: int) -> list[tuple[int, int]]:
    if size <= 0:
        raise ValueError("Video is empty")
    if size <= MAX_SINGLE_CHUNK:
        return [(0, size)]
    count = math.floor(size / CHUNK_SIZE)
    if count > 1000:
        raise ValueError("Video requires too many chunks")
    return [
        (index * CHUNK_SIZE, CHUNK_SIZE if index < count - 1 else size - index * CHUNK_SIZE)
        for index in range(count)
    ]


class TikTokAPI:
    def __init__(self, session: aiohttp.ClientSession, client_key: str, client_secret: str, redirect_uri: str):
        self.session = session
        self.client_key = client_key
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    async def _token_request(self, data: dict[str, str]) -> dict:
        async with self.session.post(
            f"{API}/v2/oauth/token/",
            data={"client_key": self.client_key, "client_secret": self.client_secret, **data},
        ) as response:
            body = await response.json(content_type=None)
        if response.status != 200 or "error" in body:
            code = body.get("error", "token_request_failed")
            raise TikTokError(f"TikTok authorization failed: {code}")
        if "video.upload" not in {scope.strip() for scope in body.get("scope", "").split(",")}:
            raise TikTokError("TikTok did not grant video.upload permission")
        body["expires_at"] = int(time.time()) + int(body["expires_in"])
        return body

    async def exchange_code(self, code: str) -> dict:
        return await self._token_request(
            {"code": code, "grant_type": "authorization_code", "redirect_uri": self.redirect_uri}
        )

    async def refresh(self, refresh_token: str) -> dict:
        return await self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})

    async def revoke(self, access_token: str) -> None:
        async with self.session.post(
            f"{API}/v2/oauth/revoke/",
            data={"client_key": self.client_key, "client_secret": self.client_secret, "token": access_token},
        ) as response:
            if response.status != 200:
                raise TikTokError("TikTok could not revoke the authorization")

    async def _post(self, endpoint: str, access_token: str, payload: dict) -> dict:
        async with self.session.post(
            f"{API}{endpoint}",
            json=payload,
            headers={"Authorization": f"Bearer {access_token}"},
        ) as response:
            body = await response.json(content_type=None)
        error = body.get("error", {})
        if response.status != 200 or error.get("code") != "ok":
            code = error.get("code", "request_failed")
            if code == "scope_not_authorized":
                raise TikTokError("TikTok has not granted direct-post access. Enable video.publish and run /link-post.")
            if code == "unaudited_client_can_only_post_to_private_accounts":
                raise TikTokError("TikTok requires an audited app for public posts. Use a private account and Only me visibility until the app is audited.")
            raise TikTokError(f"TikTok API error: {code}")
        return body.get("data", {})

    async def creator_info(self, access_token: str) -> dict:
        return await self._post("/v2/post/publish/creator_info/query/", access_token, {})

    async def upload_video(self, access_token: str, path: Path, mime_type: str) -> str:
        return await self._send_video(
            "/v2/post/publish/inbox/video/init/", access_token, path, mime_type, None
        )

    async def post_video(self, access_token: str, path: Path, mime_type: str, post_info: dict) -> str:
        return await self._send_video(
            "/v2/post/publish/video/init/", access_token, path, mime_type, post_info
        )

    async def _send_video(
        self, endpoint: str, access_token: str, path: Path, mime_type: str, post_info: dict | None
    ) -> str:
        size = path.stat().st_size
        ranges = chunk_ranges(size)
        payload = {
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": ranges[0][1] if len(ranges) == 1 else CHUNK_SIZE,
                "total_chunk_count": len(ranges),
            }
        }
        if post_info is not None:
            payload["post_info"] = post_info
        init = await self._post(
            endpoint,
            access_token,
            payload,
        )
        upload_url = init.get("upload_url")
        if not isinstance(upload_url, str) or not upload_url:
            raise TikTokError("TikTok did not return an upload URL")
        try:
            parsed = urlsplit(upload_url)
            port = parsed.port
        except ValueError as exc:
            raise TikTokError("TikTok returned a malformed upload URL") from exc
        hostname = parsed.hostname
        try:
            ipaddress.ip_address(hostname or "")
        except ValueError:
            is_ip_address = False
        else:
            is_ip_address = True
        if (
            parsed.scheme != "https"
            or not hostname
            or "." not in hostname
            or hostname.endswith((".localhost", ".local", ".internal"))
            or is_ip_address
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
            or parsed.fragment
        ):
            raise TikTokError(f"TikTok returned an unexpected upload URL (host: {hostname or 'none'})")
        publish_id = init.get("publish_id")
        if not publish_id:
            raise TikTokError("TikTok did not return an upload ID")

        with path.open("rb") as file:
            for index, (offset, length) in enumerate(ranges):
                data = await asyncio.to_thread(file.read, length)
                if len(data) != length:
                    raise TikTokError("Video file changed during upload")
                headers = {
                    "Content-Type": mime_type,
                    "Content-Length": str(length),
                    "Content-Range": f"bytes {offset}-{offset + length - 1}/{size}",
                }
                async with self.session.put(upload_url, data=data, headers=headers, allow_redirects=False) as response:
                    expected = 201 if index == len(ranges) - 1 else 206
                    if response.status != expected:
                        raise TikTokError(f"TikTok rejected video chunk {index + 1} (HTTP {response.status})")
        return publish_id

    async def status(self, access_token: str, publish_id: str) -> dict:
        return await self._post(
            "/v2/post/publish/status/fetch/", access_token, {"publish_id": publish_id}
        )
