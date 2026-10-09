from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from pathlib import Path

import aiohttp
import discord
from aiohttp import web
from discord import app_commands

from .config import Config, load_config
from .storage import Storage
from .tiktok import MIME_TYPES, TikTokAPI, TikTokError, authorization_url


log = logging.getLogger(__name__)


class UploadBot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.message_content = False  # Discord exposes attachments on direct @mentions.
        super().__init__(intents=intents)
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.storage = Storage(config.database_path, config.encryption_key)
        self.http: aiohttp.ClientSession | None = None
        self.tiktok: TikTokAPI | None = None
        self.web_runner: web.AppRunner | None = None
        self.user_locks: dict[int, asyncio.Lock] = {}
        self.upload_slots = asyncio.Semaphore(2)
        self._register_commands()

    def _lock(self, user_id: int) -> asyncio.Lock:
        return self.user_locks.setdefault(user_id, asyncio.Lock())

    async def setup_hook(self) -> None:
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3600))
        self.tiktok = TikTokAPI(
            self.http,
            self.config.tiktok_client_key,
            self.config.tiktok_client_secret,
            self.config.tiktok_redirect_uri,
        )
        app = web.Application()
        app.router.add_get("/oauth/callback", self.oauth_callback)
        app.router.add_get("/healthz", lambda _: web.Response(text="ok"))
        self.web_runner = web.AppRunner(app)
        await self.web_runner.setup()
        site = web.TCPSite(self.web_runner, self.config.http_host, self.config.http_port)
        await site.start()

        if self.config.discord_guild_id:
            guild = discord.Object(id=self.config.discord_guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def close(self) -> None:
        if self.web_runner:
            await self.web_runner.cleanup()
        if self.http:
            await self.http.close()
        self.storage.close()
        await super().close()

    def _register_commands(self) -> None:
        @self.tree.command(name="link", description="Connect your TikTok account for video uploads")
        async def link(interaction: discord.Interaction) -> None:
            state = self.storage.new_state(interaction.user.id)
            url = authorization_url(self.config.tiktok_client_key, self.config.tiktok_redirect_uri, state)
            await interaction.response.send_message(
                "Connect your TikTok account here (link expires in 10 minutes):\n" + url,
                ephemeral=True,
            )

        @self.tree.command(name="unlink", description="Disconnect your TikTok account")
        async def unlink(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            async with self._lock(interaction.user.id):
                tokens = self.storage.get_tokens(interaction.user.id)
                if not tokens:
                    await interaction.followup.send("No TikTok account is connected.", ephemeral=True)
                    return
                try:
                    access_token = await self._access_token(interaction.user.id)
                    assert self.tiktok is not None
                    await self.tiktok.revoke(access_token)
                    result = "TikTok account disconnected and authorization revoked."
                except (TikTokError, aiohttp.ClientError, asyncio.TimeoutError):
                    result = (
                        "Account disconnected here. TikTok did not confirm revocation; "
                        "you can remove this app in TikTok's account permissions."
                    )
                self.storage.delete_tokens(interaction.user.id)
            await interaction.followup.send(result, ephemeral=True)

    async def oauth_callback(self, request: web.Request) -> web.Response:
        state = request.query.get("state", "")
        user_id = self.storage.consume_state(state) if state else None
        if user_id is None:
            return web.Response(status=400, text="Invalid or expired link. Return to Discord and run /link again.")
        if request.query.get("error"):
            return web.Response(status=400, text="TikTok authorization was declined. Return to Discord and run /link again.")
        code = request.query.get("code")
        if not code:
            return web.Response(status=400, text="TikTok did not provide an authorization code.")
        try:
            assert self.tiktok is not None
            tokens = await self.tiktok.exchange_code(code)
            async with self._lock(user_id):
                self.storage.save_tokens(user_id, tokens)
        except (TikTokError, aiohttp.ClientError, asyncio.TimeoutError):
            log.warning("TikTok OAuth exchange failed for Discord user %s", user_id)
            return web.Response(status=502, text="TikTok connection failed. Return to Discord and run /link again.")
        return web.Response(text="TikTok connected. Return to Discord and @mention the bot with one video file.")

    async def _access_token(self, user_id: int) -> str:
        tokens = self.storage.get_tokens(user_id)
        if not tokens:
            raise TikTokError("No TikTok account connected. Run /link first.")
        if int(tokens["expires_at"]) <= int(time.time()) + 60:
            assert self.tiktok is not None
            tokens = await self.tiktok.refresh(tokens["refresh_token"])
            self.storage.save_tokens(user_id, tokens)
        return tokens["access_token"]

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or self.user is None:
            return
        if message.guild is not None and self.user not in message.mentions:
            return
        if not message.attachments:
            await message.reply("You need to attach a video file for uploads to work.", mention_author=False)
            return
        if len(message.attachments) > 1:
            await message.reply("Attach exactly one MP4, MOV, or WebM video.", mention_author=False)
            return
        attachment = message.attachments[0]
        suffix = Path(attachment.filename).suffix.lower()
        mime_type = MIME_TYPES.get(suffix)
        if not mime_type:
            await message.reply("I can upload MP4, MOV, or WebM video files.", mention_author=False)
            return
        if attachment.size <= 0 or attachment.size > self.config.max_video_bytes:
            limit = self.config.max_video_bytes // 1_000_000
            await message.reply(f"Video must be between 1 byte and {limit} MB.", mention_author=False)
            return
        if not self.storage.get_tokens(message.author.id):
            await message.reply("Run `/link` to connect your TikTok account first.", mention_author=False)
            return

        async with self.upload_slots, self._lock(message.author.id):
            progress = await message.reply("Downloading and uploading your video to TikTok…", mention_author=False)
            try:
                token = await self._access_token(message.author.id)
                with tempfile.TemporaryDirectory(prefix="ugc-bot-") as directory:
                    path = Path(directory) / ("video" + suffix)
                    await self._download(attachment, path)
                    assert self.tiktok is not None
                    publish_id = await self.tiktok.upload_video(token, path, mime_type)
                await progress.edit(content="Video transferred. Waiting for TikTok to process it…")
                for _ in range(20):
                    await asyncio.sleep(3)
                    try:
                        status = await self.tiktok.status(token, publish_id)
                    except (TikTokError, aiohttp.ClientError, asyncio.TimeoutError):
                        log.warning("TikTok status check failed for Discord user %s", message.author.id)
                        await progress.edit(
                            content="Video transferred to TikTok, but I could not confirm processing. "
                            "Check your TikTok inbox shortly."
                        )
                        return
                    state = status.get("status")
                    if state == "SEND_TO_USER_INBOX":
                        await progress.edit(content="Uploaded! Open your TikTok inbox notification to edit and publish the video.")
                        return
                    if state == "PUBLISH_COMPLETE":
                        await progress.edit(content="Uploaded and published on TikTok.")
                        return
                    if state == "FAILED":
                        reason = status.get("fail_reason", "unknown")
                        raise TikTokError(f"TikTok could not process the video: {reason}")
                await progress.edit(
                    content="Video transferred to TikTok, but processing is still pending. Check your TikTok inbox shortly."
                )
            except TikTokError as exc:
                await progress.edit(content=str(exc))
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                log.exception("Video upload failed for Discord user %s", message.author.id)
                await progress.edit(content="Upload failed due to a network or file error. Please try again.")

    async def _download(self, attachment: discord.Attachment, path: Path) -> None:
        assert self.http is not None
        total = 0
        async with self.http.get(attachment.url) as response:
            response.raise_for_status()
            with path.open("wb") as file:
                async for chunk in response.content.iter_chunked(1024 * 1024):
                    total += len(chunk)
                    if total > self.config.max_video_bytes:
                        raise TikTokError("Video exceeded the configured size limit")
                    await asyncio.to_thread(file.write, chunk)
        if total != attachment.size:
            raise TikTokError("Discord attachment size changed during download; please retry")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = load_config()
    bot = UploadBot(config)
    bot.run(config.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
