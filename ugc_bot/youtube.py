from __future__ import annotations

import asyncio
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import aiohttp

SCOPE = "https://www.googleapis.com/auth/youtube.upload"
CHUNK_SIZE = 8 * 1024 * 1024


class YouTubeError(Exception):
    pass


def validate_metadata(title: str, description: str) -> None:
    if not title.strip() or len(title) > 100 or any(c in title + description for c in "<>"):
        raise YouTubeError("Use a title of 1–100 characters and no < or > in title or description.")
    if len(description.encode("utf-8")) > 5000:
        raise YouTubeError("Description must be at most 5000 UTF-8 bytes.")


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "state": state,
        "response_type": "code", "scope": SCOPE, "access_type": "offline",
        "prompt": "consent select_account",
    })


class YouTubeAPI:
    def __init__(self, session: aiohttp.ClientSession, client_id: str, client_secret: str, redirect_uri: str):
        self.session = session
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    async def _token_request(self, data: dict) -> dict:
        async with self.session.post("https://oauth2.googleapis.com/token", data={
            "client_id": self.client_id, "client_secret": self.client_secret, **data,
        }) as response:
            try:
                body = await response.json(content_type=None)
            except ValueError as exc:
                raise YouTubeError("YouTube authorization failed. Run /link-youtube again.") from exc
            if response.status != 200 or not isinstance(body, dict) or not body.get("access_token"):
                raise YouTubeError("YouTube authorization failed. Run /link-youtube again.")
        if SCOPE not in body.get("scope", SCOPE).split():
            raise YouTubeError("YouTube did not grant upload permission. Run /link-youtube again.")
        try:
            body["expires_at"] = int(time.time()) + int(body["expires_in"])
        except (KeyError, ValueError, TypeError) as exc:
            raise YouTubeError("YouTube returned an invalid authorization response. Run /link-youtube again.") from exc
        return body

    async def exchange_code(self, code: str) -> dict:
        tokens = await self._token_request({"code": code, "grant_type": "authorization_code",
                                            "redirect_uri": self.redirect_uri})
        if not tokens.get("refresh_token"):
            raise YouTubeError("YouTube did not grant offline access. Run /link-youtube again.")
        return tokens

    async def refresh(self, refresh_token: str) -> dict:
        tokens = await self._token_request({"refresh_token": refresh_token, "grant_type": "refresh_token"})
        tokens.setdefault("refresh_token", refresh_token)
        return tokens

    async def revoke(self, token: str) -> None:
        async with self.session.post("https://oauth2.googleapis.com/revoke", data={"token": token}) as response:
            if response.status != 200:
                raise YouTubeError("Google could not revoke access. Try /unlink-youtube again.")

    async def post_video(self, token: str, path: Path, mime_type: str, title: str,
                         description: str, privacy: str, made_for_kids: bool) -> str:
        validate_metadata(title, description)
        if privacy not in {"private", "unlisted", "public"}:
            raise YouTubeError("Choose private, unlisted or public visibility.")
        if not isinstance(made_for_kids, bool):
            raise YouTubeError("Choose whether this video is made for kids.")
        size = path.stat().st_size
        if size <= 0:
            raise YouTubeError("Video is empty.")
        async with self.session.post(
            "https://www.googleapis.com/upload/youtube/v3/videos",
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers={"Authorization": f"Bearer {token}", "X-Upload-Content-Type": mime_type,
                     "X-Upload-Content-Length": str(size)},
            json={"snippet": {"title": title.strip(), "description": description},
                  "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": made_for_kids}},
            allow_redirects=False,
        ) as response:
            if response.status not in (200, 201):
                raise YouTubeError(f"YouTube could not start the upload (HTTP {response.status}).")
            upload_url = response.headers.get("Location", "")
        try:
            parsed = urlsplit(upload_url)
            port = parsed.port
        except ValueError as exc:
            raise YouTubeError("YouTube returned a malformed upload URL.") from exc
        if (parsed.scheme != "https" or parsed.hostname != "www.googleapis.com"
                or port not in (None, 443) or parsed.username or parsed.password or parsed.fragment
                or parsed.path != "/upload/youtube/v3/videos"):
            raise YouTubeError("YouTube returned an unexpected upload URL.")
        with path.open("rb") as video:
            offset = 0
            while offset < size:
                length = min(CHUNK_SIZE, size - offset)
                chunk = await asyncio.to_thread(video.read, length)
                if len(chunk) != length:
                    raise YouTubeError("Video changed during upload.")
                end = offset + len(chunk)
                async with self.session.put(upload_url, data=chunk, allow_redirects=False, headers={
                    "Authorization": f"Bearer {token}", "Content-Type": mime_type,
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {offset}-{end - 1}/{size}",
                }) as response:
                    if end < size:
                        if response.status != 308 or response.headers.get("Range") != f"bytes=0-{end - 1}":
                            raise YouTubeError("YouTube did not acknowledge the video chunk. Check your channel before retrying.")
                    else:
                        if response.status not in (200, 201):
                            raise YouTubeError(f"YouTube upload failed (HTTP {response.status}). Check your channel before retrying.")
                        try:
                            body = await response.json(content_type=None)
                        except ValueError as exc:
                            raise YouTubeError("YouTube returned an invalid upload response. Check your channel before retrying.") from exc
                        if not isinstance(body, dict) or not isinstance(body.get("id"), str) or not body["id"]:
                            raise YouTubeError("YouTube returned no video ID. Check your channel before retrying.")
                        return body["id"]
                offset = end
        raise YouTubeError("Upload did not complete.")
