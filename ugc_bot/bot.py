from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def load_time_zone(name: str) -> ZoneInfo:
    if not name or len(name) > 64:
        raise ValueError("Enter a valid IANA time zone, for example `America/Indianapolis`.")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"Unknown time zone `{name}`. Use an IANA name such as `America/Indianapolis`; "
            "install the bot's dependencies if that name should exist."
        ) from exc


def format_utc_offset(offset: timedelta) -> str:
    minutes = int(offset.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, remainder = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours:02d}:{remainder:02d}"


def time_zone_display(name: str) -> str:
    try:
        local_now = datetime.now(load_time_zone(name))
    except ValueError:
        return name
    return f"{name} (now {local_now:%H:%M}, {format_utc_offset(local_now.utcoffset())})"


def parse_schedule_time(value: str, now: int | None = None, time_zone: str | None = None) -> int:
    text = value.strip()
    if time_zone is None:
        try:
            when = datetime.strptime(text, "%Y-%m-%d %H:%M %z")
        except ValueError as exc:
            raise ValueError(
                "Include a UTC offset, for example `2026-10-12 14:30 -04:00`, "
                "or set your time zone with `/set-time-zone`."
            ) from exc
        scheduled_at = int(when.timestamp())
    else:
        zone = load_time_zone(time_zone)
        explicit_offset = None
        try:
            local = datetime.strptime(text, "%Y-%m-%d %H:%M")
        except ValueError:
            try:
                explicit_offset = datetime.strptime(text, "%Y-%m-%d %H:%M %z")
                local = explicit_offset.replace(tzinfo=None)
            except ValueError as exc:
                raise ValueError(
                    f"Enter local time in `{time_zone}` as `YYYY-MM-DD HH:MM`, "
                    "for example `2026-10-12 14:30`."
                ) from exc
        candidates: dict[int, timedelta] = {}
        for fold in (0, 1):
            candidate = local.replace(tzinfo=zone, fold=fold)
            try:
                timestamp = int(candidate.timestamp())
                if datetime.fromtimestamp(timestamp, zone).replace(tzinfo=None) == local:
                    candidates[timestamp] = candidate.utcoffset()
            except (OverflowError, OSError, ValueError):
                continue
        if not candidates:
            raise ValueError(f"That local time does not exist in `{time_zone}` because of a clock change.")
        if explicit_offset is not None:
            scheduled_at = int(explicit_offset.timestamp())
            if scheduled_at not in candidates:
                raise ValueError(f"That UTC offset does not match `{time_zone}` at the chosen time.")
        elif len(candidates) > 1:
            offsets = " or ".join(sorted(format_utc_offset(offset) for offset in candidates.values()))
            raise ValueError(
                f"That time occurs twice in `{time_zone}`. Add an offset ({offsets}) after the time."
            )
        else:
            scheduled_at = next(iter(candidates))
    current = int(time.time()) if now is None else now
    if scheduled_at < current + 60:
        raise ValueError("Choose a time at least one minute in the future.")
    if scheduled_at > current + int(timedelta(days=30).total_seconds()):
        raise ValueError("Choose a time within the next 30 days.")
    return scheduled_at


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


class ScheduleModal(discord.ui.Modal, title="Schedule TikTok post"):
    def __init__(self, post_view: DirectPostView, panel_message: discord.Message, time_zone: str | None):
        super().__init__()
        self.post_view = post_view
        self.panel_message = panel_message
        self.time_zone = time_zone
        if time_zone:
            label = f"Time in {time_zone}"
            label = label if len(label) <= 45 else "Local date and time (zone below)"
            placeholder = f"{time_zone}: YYYY-MM-DD HH:MM"
        else:
            label = "Date and time with UTC offset"
            placeholder = "YYYY-MM-DD HH:MM -04:00"
        self.when = discord.ui.TextInput(label=label, placeholder=placeholder, max_length=80)
        self.add_item(self.when)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.post_view.bot.storage.get_time_zone(self.post_view.message.author.id) != self.time_zone:
            await interaction.response.send_message(
                "Your time zone changed. Open Schedule post again to review the time in the new zone.", ephemeral=True
            )
            return
        try:
            scheduled_at = parse_schedule_time(str(self.when.value), time_zone=self.time_zone)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if self.post_view.submitted:
            await interaction.response.send_message("This post is already being sent or scheduled.", ephemeral=True)
            return
        error = self.post_view.selection_error()
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        self.post_view.submitted = True
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            post_id = await self.post_view.bot.schedule_direct_video(self.post_view.message, self.post_view, scheduled_at)
        except (TikTokError, aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            self.post_view.submitted = False
            await interaction.followup.send(f"Could not schedule this video: {exc}", ephemeral=True)
            return
        self.post_view.stop()
        for item in self.post_view.children:
            item.disabled = True
        await self.panel_message.edit(
            content=f"Scheduled **{self.post_view.message.attachments[0].filename}** for <t:{scheduled_at}:F> "
                    f"(ID `{post_id}`; entered in {self.time_zone or 'the supplied UTC offset'}). "
                    "Use `/scheduled` to review or `/cancel-post` to cancel.",
            view=self.post_view,
        )
        await interaction.followup.send(f"Scheduled for <t:{scheduled_at}:F>.", ephemeral=True)


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
            await interaction.response.send_message("Only the person who attached the video can post or schedule it.", ephemeral=True)
            return False
        return True

    def selection_error(self) -> str | None:
        if self.privacy is None:
            return "Choose a privacy setting before posting."
        if self.commercial in {"branded", "both"} and self.privacy not in {
            "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS"
        }:
            return "Branded content needs Everyone or Friends visibility. Choose a different audience."
        return None

    def post_info(self, creator: dict) -> dict:
        return {
            "privacy_level": self.privacy,
            "title": self.caption,
            "disable_comment": "comment" not in self.interactions or creator.get("comment_disabled", False),
            "disable_duet": "duet" not in self.interactions or creator.get("duet_disabled", False),
            "disable_stitch": "stitch" not in self.interactions or creator.get("stitch_disabled", False),
            "brand_organic_toggle": self.commercial in {"own", "both"},
            "brand_content_toggle": self.commercial in {"branded", "both"},
        }

    @discord.ui.button(label="Edit caption", style=discord.ButtonStyle.secondary, row=3)
    async def edit_caption(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(CaptionModal(self))

    @discord.ui.button(label="Post now", style=discord.ButtonStyle.primary, row=3)
    async def publish(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.submitted:
            await interaction.response.send_message("This post is already being sent.", ephemeral=True)
            return
        error = self.selection_error()
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        self.submitted = True
        self.stop()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Sending your video to TikTok…", view=self)
        await self.bot.post_direct_video(interaction.message, self.message, self)

    @discord.ui.button(label="Schedule post", style=discord.ButtonStyle.secondary, row=3)
    async def schedule(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.submitted:
            await interaction.response.send_message("This post is already being sent or scheduled.", ephemeral=True)
            return
        error = self.selection_error()
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        time_zone = self.bot.storage.get_time_zone(self.message.author.id)
        await interaction.response.send_modal(ScheduleModal(self, interaction.message, time_zone))


class UploadBot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.message_content = False  # Discord exposes attachments on direct @mentions.
        super().__init__(intents=intents)
        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.storage = Storage(config.database_path, config.encryption_key)
        self.scheduled_dir = config.database_path.parent / "scheduled_videos"
        self.scheduled_task: asyncio.Task | None = None
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
        self.scheduled_dir.mkdir(parents=True, exist_ok=True)
        self.scheduled_task = asyncio.create_task(self._scheduled_loop())

    async def close(self) -> None:
        if self.scheduled_task:
            self.scheduled_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.scheduled_task
        if self.web_runner:
            await self.web_runner.cleanup()
        if self.tiktok_session:
            await self.tiktok_session.close()
        self.storage.close()
        await super().close()

    def _register_commands(self) -> None:
        @self.tree.command(name="link-tiktok", description="Connect TikTok for inbox uploads and direct posts")
        async def link_tiktok(interaction: discord.Interaction) -> None:
            state = self.storage.new_state(interaction.user.id)
            url = authorization_url(self.config.tiktok_client_key, self.config.tiktok_redirect_uri, state)
            await interaction.response.send_message(
                "Connect your TikTok account for uploads and direct posts "
                "(link expires in 10 minutes):\n" + url
                + "\nTo schedule in your local time, set it with `/set-time-zone` "
                  "(for example `America/Indianapolis`).",
                ephemeral=True,
            )

        @self.tree.command(name="set-time-zone", description="Set or view your time zone for scheduled posts")
        @app_commands.describe(time_zone="IANA name, for example America/Indianapolis; omit to view your setting")
        async def set_time_zone(interaction: discord.Interaction, time_zone: str | None = None) -> None:
            if time_zone is None:
                saved = self.storage.get_time_zone(interaction.user.id)
                result = (
                    f"Your scheduling time zone is `{time_zone_display(saved)}`."
                    if saved else "No scheduling time zone set. Use `/set-time-zone` with an IANA name "
                            "such as `America/Indianapolis`."
                )
            else:
                name = time_zone.strip()
                try:
                    load_time_zone(name)
                except ValueError as exc:
                    await interaction.response.send_message(str(exc), ephemeral=True)
                    return
                self.storage.set_time_zone(interaction.user.id, name)
                result = (
                    f"Scheduling time zone set to `{time_zone_display(name)}`. "
                    "Existing scheduled posts keep their original publish times."
                )
            await interaction.response.send_message(result, ephemeral=True)

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
                cancelled = self.storage.cancel_all_scheduled_posts(interaction.user.id)
                for video_name in cancelled:
                    (self.scheduled_dir / video_name).unlink(missing_ok=True)
                if cancelled:
                    result += f" Cancelled {len(cancelled)} pending scheduled post(s)."
            await interaction.followup.send(result, ephemeral=True)

        @self.tree.command(name="scheduled", description="See your recent scheduled TikTok posts")
        async def scheduled(interaction: discord.Interaction) -> None:
            posts = self.storage.list_scheduled_posts(interaction.user.id)
            if not posts:
                await interaction.response.send_message("You have no scheduled posts.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            lines = [
                f"`{post['id']}` · <t:{post['scheduled_at']}:F> · {post['state']} · "
                f"{discord.utils.escape_markdown(discord.utils.escape_mentions(post['original_name']))[:50]}"
                + (f" · {discord.utils.escape_mentions(post['error'])[:80]}" if post["error"] else "")
                for post in posts
            ]
            for index in range(0, len(lines), 10):
                await interaction.followup.send(
                    ("Your scheduled posts:\n" if index == 0 else "More scheduled posts:\n")
                    + "\n".join(lines[index:index + 10])
                    + ("\nUse `/cancel-post` for a pending post." if index + 10 >= len(lines) else ""),
                    ephemeral=True,
                )

        @self.tree.command(name="cancel-post", description="Cancel a pending scheduled TikTok post")
        @app_commands.describe(post_id="ID shown by /scheduled")
        async def cancel_post(interaction: discord.Interaction, post_id: str) -> None:
            async with self._lock(interaction.user.id):
                video_name = self.storage.cancel_scheduled_post(interaction.user.id, post_id.strip())
                if video_name:
                    (self.scheduled_dir / video_name).unlink(missing_ok=True)
            await interaction.response.send_message(
                "Scheduled post cancelled." if video_name else "No pending post with that ID was found.",
                ephemeral=True,
            )

    async def oauth_callback(self, request: web.Request) -> web.Response:
        state = request.query.get("state", "")
        user_id = self.storage.consume_state(state) if state else None
        if user_id is None:
            return web.Response(status=400, text="Invalid or expired link. Return to Discord and run /link-tiktok again.")
        if request.query.get("error"):
            return web.Response(status=400, text="TikTok authorization was declined. Return to Discord and run /link-tiktok again.")
        code = request.query.get("code")
        if not code:
            return web.Response(status=400, text="TikTok did not provide an authorization code.")
        try:
            assert self.tiktok is not None
            tokens = await self.tiktok.exchange_code(code)
            async with self._lock(user_id):
                self.storage.save_tokens(user_id, tokens)
        except TikTokError as exc:
            log.warning("TikTok OAuth exchange failed for Discord user %s: %s", user_id, exc)
            return web.Response(status=400, text=f"{exc}. Return to Discord and run /link-tiktok again.")
        except (aiohttp.ClientError, asyncio.TimeoutError):
            log.warning("TikTok OAuth exchange failed for Discord user %s", user_id)
            return web.Response(status=502, text="TikTok connection failed. Return to Discord and run /link-tiktok again.")
        return web.Response(text="TikTok connected. Return to Discord and @mention the bot with one video file.")

    async def _access_token(self, user_id: int) -> str:
        tokens = self.storage.get_tokens(user_id)
        if not tokens:
            raise TikTokError("No TikTok account connected. Run /link-tiktok first.")
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
            await message.reply("Run `/link-tiktok` to connect your TikTok account first.", mention_author=False)
            return

        if is_direct_post_request(message.content, self.user.id):
            tokens = self.storage.get_tokens(message.author.id)
            if "video.publish" not in {scope.strip() for scope in tokens.get("scope", "").split(",")}:
                await message.reply(
                    "Direct posting needs TikTok's `video.publish` permission. Run `/link-tiktok` again.",
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
            saved_zone = self.storage.get_time_zone(message.author.id)
            schedule_hint = (
                f"Scheduling time zone: `{time_zone_display(saved_zone)}`. "
                "Use `/set-time-zone` if it is wrong."
                if saved_zone else
                "Scheduling time zone: not set. Enter a UTC offset or use `/set-time-zone` first."
            )
            await message.reply(
                f"Post **{attachment.filename}** to **{account}**. Choose privacy, review the options, "
                "then choose **Post now** or **Schedule post**. By posting, you agree to TikTok's "
                "[Music Usage Confirmation](https://www.tiktok.com/legal/page/global/music-usage-confirmation/en). "
                "For branded content, you also agree to TikTok's "
                "[Branded Content Policy](https://www.tiktok.com/legal/page/global/bc-policy/en). "
                "Unaudited apps can only post privately to private TikTok accounts. "
                "The video may take a few minutes to appear on your profile.\n" + schedule_hint,
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
                post_info = settings.post_info(creator)
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

    async def schedule_direct_video(
        self, message: discord.Message, settings: DirectPostView, scheduled_at: int
    ) -> str:
        attachment = message.attachments[0]
        suffix = Path(attachment.filename).suffix.lower()
        post_id = secrets.token_hex(6)
        video_name = post_id + suffix
        path = self.scheduled_dir / video_name
        partial_path = self.scheduled_dir / (video_name + ".part")
        async with self.upload_slots, self._lock(message.author.id):
            tokens = self.storage.get_tokens(message.author.id)
            if not tokens or "video.publish" not in {
                scope.strip() for scope in tokens.get("scope", "").split(",")
            }:
                raise TikTokError("Direct posting is not connected. Run /link-tiktok again.")
            if self.storage.active_scheduled_count(message.author.id) >= 20:
                raise TikTokError("You already have 20 pending scheduled posts. Cancel one before adding another.")
            self.scheduled_dir.mkdir(parents=True, exist_ok=True)
            try:
                await self._download(attachment, partial_path)
                partial_path.replace(path)
                self.storage.add_scheduled_post(
                    post_id, message.author.id, video_name, attachment.filename,
                    MIME_TYPES[suffix], settings.post_info(settings.creator), scheduled_at,
                )
            except BaseException:
                partial_path.unlink(missing_ok=True)
                path.unlink(missing_ok=True)
                raise
        return post_id

    async def _notify_scheduled(self, user_id: int, message: str) -> None:
        try:
            user = self.get_user(user_id) or await self.fetch_user(user_id)
            await user.send(message, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("Could not notify Discord user %s about a scheduled post", user_id)

    async def _check_scheduled_post(self, post: dict, token: str | None = None) -> None:
        user_id = int(post["discord_user_id"])
        try:
            if token is None:
                token = await self._access_token(user_id)
            assert self.tiktok is not None
            status = await self.tiktok.status(token, post["publish_id"])
        except (TikTokError, aiohttp.ClientError, asyncio.TimeoutError):
            log.warning("Could not check scheduled TikTok post %s", post["id"])
            return
        state = status.get("status")
        if state == "PUBLISH_COMPLETE":
            self.storage.update_scheduled_post(post["id"], "posted")
            await self._notify_scheduled(user_id, f"Scheduled TikTok post `{post['id']}` was published.")
        elif state == "FAILED":
            reason = str(status.get("fail_reason", "unknown"))[:200]
            self.storage.update_scheduled_post(post["id"], "failed", error=reason)
            await self._notify_scheduled(user_id, f"Scheduled TikTok post `{post['id']}` failed: {reason}")

    async def _process_scheduled_post(self, post: dict) -> None:
        user_id = int(post["discord_user_id"])
        async with self._lock(user_id):
            if post["state"] == "submitted":
                await self._check_scheduled_post(post)
                return
            if not self.storage.claim_scheduled_post(post["id"]):
                return
            path = self.scheduled_dir / post["video_name"]
            try:
                if not path.is_file():
                    raise TikTokError("Saved video is missing from the bot's storage.")
                tokens = self.storage.get_tokens(user_id)
                if not tokens or "video.publish" not in {
                    scope.strip() for scope in tokens.get("scope", "").split(",")
                }:
                    raise TikTokError("TikTok direct-post permission is no longer connected.")
                token = await self._access_token(user_id)
                assert self.tiktok is not None
                creator = await self.tiktok.creator_info(token)
                post_info = json.loads(post["post_info"])
                if post_info["privacy_level"] not in creator.get("privacy_level_options", []):
                    raise TikTokError("The selected TikTok privacy setting is no longer available.")
                for name in ("comment", "duet", "stitch"):
                    if creator.get(f"{name}_disabled", False):
                        post_info[f"disable_{name}"] = True
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                self.storage.update_scheduled_post(post["id"], "pending", error="Temporary network error; will retry.")
                log.warning("Scheduled TikTok post %s will retry after network error: %s", post["id"], exc)
                return
            except (TikTokError, OSError, ValueError, KeyError) as exc:
                reason = str(exc)[:200]
                self.storage.update_scheduled_post(post["id"], "failed", error=reason)
                path.unlink(missing_ok=True)
                await self._notify_scheduled(user_id, f"Scheduled TikTok post `{post['id']}` failed: {reason}")
                return
            try:
                async with self.upload_slots:
                    publish_id = await self.tiktok.post_video(token, path, post["mime_type"], post_info)
            except (TikTokError, aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                self.storage.update_scheduled_post(
                    post["id"], "uncertain",
                    error="Could not confirm whether TikTok received this post; check your profile before retrying.",
                )
                path.unlink(missing_ok=True)
                log.warning("Scheduled TikTok post %s could not be confirmed: %s", post["id"], exc)
                await self._notify_scheduled(
                    user_id, f"Scheduled TikTok post `{post['id']}` may have been sent. "
                             "Check your profile before trying again; the bot will not retry it automatically."
                )
                return
            self.storage.update_scheduled_post(post["id"], "submitted", publish_id=publish_id)
            path.unlink(missing_ok=True)
            post["publish_id"] = publish_id
            await self._check_scheduled_post(post, token)

    async def _scheduled_loop(self) -> None:
        await self.wait_until_ready()
        interrupted = self.storage.recover_interrupted_posts()
        for post_id, user_id, video_name in interrupted:
            (self.scheduled_dir / video_name).unlink(missing_ok=True)
            await self._notify_scheduled(
                user_id, f"Scheduled TikTok post `{post_id}` was interrupted. Check your profile before trying again."
            )
        while True:
            try:
                posts = self.storage.due_scheduled_posts(int(time.time()))
                results = await asyncio.gather(
                    *(self._process_scheduled_post(post) for post in posts), return_exceptions=True
                )
                for result in results:
                    if isinstance(result, Exception):
                        log.error("Scheduled post worker error: %r", result)
            except Exception:
                log.exception("Scheduled post worker failed")
            await asyncio.sleep(30)

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
