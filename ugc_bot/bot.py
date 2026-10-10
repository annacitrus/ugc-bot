from __future__ import annotations

import asyncio
import logging
import re
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


def is_direct_post_request(content: str, bot_id: int) -> bool:
    return re.sub(rf"<@!?{bot_id}>", "", content).strip() == "post this"


class CaptionModal(discord.ui.Modal, title="TikTok caption"):
    caption = discord.ui.TextInput(
        label="Caption (optional)", style=discord.TextStyle.paragraph, required=False, max_length=2000
    )

    def __init__(self, post_view: DirectPostView):
        super().__init__()
        self.post_view = post_view
        self.caption.default = post_view.caption

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.post_view.caption = str(self.caption.value)
        await interaction.response.send_message("Caption saved for this post.", ephemeral=True)


class DirectPostView(discord.ui.View):
    def __init__(self, bot: UploadBot, message: discord.Message, creator: dict):
        super().__init__(timeout=600)
        self.bot = bot
        self.message = message
        self.creator = creator
        self.privacy: str | None = None
        self.caption = ""
        self.interactions: set[str] = set()
        self.commercial = "none"
        self.submitted = False

        privacy_names = {
            "PUBLIC_TO_EVERYONE": "Everyone",
            "MUTUAL_FOLLOW_FRIENDS": "Friends",
            "FOLLOWER_OF_CREATOR": "Followers",
            "SELF_ONLY": "Only me",
        }
        options = [
            discord.SelectOption(label=privacy_names.get(value, value), value=value)
            for value in creator.get("privacy_level_options", [])
            if value in privacy_names
        ]
        if not options:
            raise TikTokError("TikTok did not provide a privacy option for direct posting.")
        privacy_select = discord.ui.Select(placeholder="Choose who can see this post", options=options, row=0)

        async def set_privacy(interaction: discord.Interaction) -> None:
            self.privacy = privacy_select.values[0]
            await interaction.response.defer()

        privacy_select.callback = set_privacy
        self.add_item(privacy_select)

        allowed = [
            (name, label)
            for name, label in (("comment", "Allow comments"), ("duet", "Allow duets"), ("stitch", "Allow stitches"))
            if not creator.get(f"{name}_disabled", False)
        ]
        if allowed:
            interaction_select = discord.ui.Select(
                placeholder="Optional: allow interactions (none by default)",
                options=[discord.SelectOption(label=label, value=name) for name, label in allowed],
                min_values=0,
                max_values=len(allowed),
                row=1,
            )

            async def set_interactions(interaction: discord.Interaction) -> None:
                self.interactions = set(interaction_select.values)
                await interaction.response.defer()

            interaction_select.callback = set_interactions
            self.add_item(interaction_select)

        commercial_select = discord.ui.Select(
            placeholder="Commercial content (off by default)",
            options=[
                discord.SelectOption(label="Not promotional", value="none"),
                discord.SelectOption(label="My brand", value="own"),
                discord.SelectOption(label="Branded content / paid partnership", value="branded"),
                discord.SelectOption(label="My brand and branded content", value="both"),
            ],
            row=2,
        )

        async def set_commercial(interaction: discord.Interaction) -> None:
            self.commercial = commercial_select.values[0]
            await interaction.response.defer()

        commercial_select.callback = set_commercial
        self.add_item(commercial_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.message.author.id:
            await interaction.response.send_message("Only the person who attached the video can publish it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Edit caption", style=discord.ButtonStyle.secondary, row=3)
    async def edit_caption(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(CaptionModal(self))

    @discord.ui.button(label="Post to TikTok", style=discord.ButtonStyle.primary, row=3)
    async def publish(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.submitted:
            await interaction.response.send_message("This post is already being sent.", ephemeral=True)
            return
        if self.privacy is None:
            await interaction.response.send_message("Choose a privacy setting before posting.", ephemeral=True)
            return
        if self.commercial in {"branded", "both"} and self.privacy not in {
            "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS"
        }:
            await interaction.response.send_message(
                "Branded content needs Everyone or Friends visibility. Choose a different audience.", ephemeral=True
            )
            return
        self.submitted = True
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Sending your video to TikTok…", view=self)
        await self.bot.post_direct_video(interaction.message, self.message, self)


class UploadBot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.message_content = False  # Discord exposes attachments on direct @mentions.
        super().__init__(intents=intents)
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.storage = Storage(config.database_path, config.encryption_key)
        self.tiktok_session: aiohttp.ClientSession | None = None
        self.tiktok: TikTokAPI | None = None
        self.web_runner: web.AppRunner | None = None
        self.user_locks: dict[int, asyncio.Lock] = {}
        self.upload_slots = asyncio.Semaphore(2)
        self._register_commands()

    def _lock(self, user_id: int) -> asyncio.Lock:
        return self.user_locks.setdefault(user_id, asyncio.Lock())

    async def setup_hook(self) -> None:
        self.tiktok_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3600))
        self.tiktok = TikTokAPI(
            self.tiktok_session,
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
        if self.tiktok_session:
            await self.tiktok_session.close()
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

        @self.tree.command(name="link-post", description="Allow direct posts to your TikTok account")
        async def link_post(interaction: discord.Interaction) -> None:
            state = self.storage.new_state(interaction.user.id)
            url = authorization_url(
                self.config.tiktok_client_key,
                self.config.tiktok_redirect_uri,
                state,
                ("video.upload", "video.publish"),
            )
            await interaction.response.send_message(
                "Authorize TikTok direct posting here (link expires in 10 minutes):\n" + url,
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

        if is_direct_post_request(message.content, self.user.id):
            tokens = self.storage.get_tokens(message.author.id)
            if "video.publish" not in {scope.strip() for scope in tokens.get("scope", "").split(",")}:
                await message.reply(
                    "Direct posting needs TikTok's `video.publish` permission. Run `/link-post` first.",
                    mention_author=False,
                )
                return
            try:
                token = await self._access_token(message.author.id)
                assert self.tiktok is not None
                creator = await self.tiktok.creator_info(token)
                view = DirectPostView(self, message, creator)
            except (TikTokError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
                await message.reply(f"Could not prepare a direct post: {exc}", mention_author=False)
                return
            account = creator.get("creator_nickname") or creator.get("creator_username") or "your TikTok account"
            await message.reply(
                f"Post **{attachment.filename}** to **{account}**. Choose privacy, review the options, "
                "then press **Post to TikTok**. By posting, you agree to TikTok's "
                "[Music Usage Confirmation](https://www.tiktok.com/legal/page/global/music-usage-confirmation/en). "
                "For branded content, you also agree to TikTok's "
                "[Branded Content Policy](https://www.tiktok.com/legal/page/global/bc-policy/en). "
                "Unaudited apps can only post privately to private TikTok accounts. "
                "The video may take a few minutes to appear on your profile.",
                mention_author=False,
                view=view,
            )
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

    async def post_direct_video(
        self, progress: discord.Message, message: discord.Message, settings: DirectPostView
    ) -> None:
        attachment = message.attachments[0]
        suffix = Path(attachment.filename).suffix.lower()
        mime_type = MIME_TYPES[suffix]
        async with self.upload_slots, self._lock(message.author.id):
            try:
                token = await self._access_token(message.author.id)
                assert self.tiktok is not None
                creator = await self.tiktok.creator_info(token)
                if settings.privacy not in creator.get("privacy_level_options", []):
                    raise TikTokError("Your TikTok privacy options changed. Send the video again to choose a new one.")
                if settings.commercial in {"branded", "both"} and settings.privacy not in {
                    "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS"
                }:
                    raise TikTokError("Branded content needs Everyone or Friends visibility.")
                post_info = {
                    "privacy_level": settings.privacy,
                    "title": settings.caption,
                    "disable_comment": "comment" not in settings.interactions or creator.get("comment_disabled", False),
                    "disable_duet": "duet" not in settings.interactions or creator.get("duet_disabled", False),
                    "disable_stitch": "stitch" not in settings.interactions or creator.get("stitch_disabled", False),
                    "brand_organic_toggle": settings.commercial in {"own", "both"},
                    "brand_content_toggle": settings.commercial in {"branded", "both"},
                }
                with tempfile.TemporaryDirectory(prefix="ugc-bot-") as directory:
                    path = Path(directory) / ("video" + suffix)
                    await self._download(attachment, path)
                    publish_id = await self.tiktok.post_video(token, path, mime_type, post_info)
                await progress.edit(content="Video transferred. Waiting for TikTok to publish it…")
                for _ in range(20):
                    await asyncio.sleep(3)
                    try:
                        status = await self.tiktok.status(token, publish_id)
                    except (TikTokError, aiohttp.ClientError, asyncio.TimeoutError):
                        log.warning("TikTok direct-post status check failed for Discord user %s", message.author.id)
                        await progress.edit(
                            content="Video transferred, but I could not confirm publication. Check your TikTok profile."
                        )
                        return
                    state = status.get("status")
                    if state == "PUBLISH_COMPLETE":
                        await progress.edit(content="Posted on TikTok! It may take a few minutes to appear on your profile.")
                        return
                    if state == "FAILED":
                        raise TikTokError(
                            f"TikTok could not publish the video: {status.get('fail_reason', 'unknown')}"
                        )
                await progress.edit(content="TikTok is still processing the post. Check your profile shortly.")
            except TikTokError as exc:
                await progress.edit(content=str(exc))
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                log.exception("TikTok direct post failed for Discord user %s", message.author.id)
                await progress.edit(content="Direct post failed due to a network or file error. Please try again.")

    async def _download(self, attachment: discord.Attachment, path: Path) -> None:
        assert self.tiktok_session is not None
        total = 0
        async with self.tiktok_session.get(attachment.url) as response:
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
