import asyncio
import copy
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch
from urllib.parse import parse_qs, urlsplit

import discord
import aiohttp
from cryptography.fernet import Fernet

from ugc_bot.bot import DirectPostView, ScheduleModal, UploadBot, YouTubeDetailsModal, YouTubePostView
from ugc_bot.config import Config, load_config
from ugc_bot.storage import Storage
from ugc_bot.tiktok import TikTokError
from ugc_bot.youtube import (CHUNK_SIZE, SCOPE, YouTubeAPI, YouTubeError, authorization_url,
                             validate_metadata, validate_short, validate_short_metadata)


def run(coroutine):
    try:
        coroutine.send(None)
    except StopIteration as finished:
        return finished.value
    else:
        coroutine.close()
        raise AssertionError("Fake API unexpectedly waited for external I/O")


async def inline(function, *args):
    return function(*args)


class Response:
    def __init__(self, status=200, body=None, headers=None):
        self.status, self.body, self.headers = status, body, headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def json(self, **_):
        return copy.deepcopy(self.body)


class Session:
    def __init__(self, response=None, chunks=None):
        self.response = response or Response(headers={
            "Location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=test"})
        self.chunks = list(chunks or [Response(201, {"id": "video-123"})])
        self.posts, self.uploads = [], []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.response

    def put(self, url, **kwargs):
        self.uploads.append((url, kwargs))
        return self.chunks.pop(0)


def config(directory):
    return Config(
        discord_token="test", tiktok_client_key="test", tiktok_client_secret="test",
        tiktok_redirect_uri="https://bot.example.com/oauth/callback",
        encryption_key=Fernet.generate_key(), database_path=Path(directory) / "bot.sqlite3",
        http_host="127.0.0.1", http_port=8080, discord_guild_id=None, max_video_bytes=100_000_000,
        youtube_client_id="youtube-client", youtube_client_secret="youtube-secret",
        youtube_redirect_uri="https://bot.example.com/oauth/youtube/callback")


def message(guild=None, mentions=None):
    return SimpleNamespace(author=SimpleNamespace(id=123, bot=False), guild=guild,
                           mentions=mentions or [], reply=AsyncMock(),
                           attachments=[SimpleNamespace(filename="clip.mp4", size=10)])


def interaction(user_id=123):
    return SimpleNamespace(user=SimpleNamespace(id=user_id), message=SimpleNamespace(edit=AsyncMock()),
                           response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(),
                                                    edit_message=AsyncMock(), send_modal=AsyncMock()),
                           followup=SimpleNamespace(send=AsyncMock()))


class AccountTests(unittest.TestCase):
    def test_existing_database_and_provider_states_remain_isolated(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "old.sqlite3"
            key = Fernet.generate_key()
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE accounts (discord_user_id TEXT PRIMARY KEY, encrypted_tokens BLOB NOT NULL)")
            db.execute("INSERT INTO accounts VALUES (?, ?)", ("123", Fernet(key).encrypt(b'{"access_token":"old"}')))
            db.commit()
            db.close()
            store = Storage(path, key)
            try:
                self.assertEqual(store.get_tokens(123), {"access_token": "old"})
                state = store.new_state(123, youtube=True)
                self.assertIsNone(store.consume_state(state))
                self.assertEqual(store.consume_state(state, youtube=True), 123)
                self.assertIsNone(store.consume_state(state, youtube=True))
                expired = store.new_state(456, youtube=True)
                store.db.execute("UPDATE youtube_oauth_states SET expires_at = 0 WHERE state = ?", (expired,))
                self.assertIsNone(store.consume_state(expired, youtube=True))
                store.save_tokens(123, {"refresh_token": "youtube-refresh"}, youtube=True)
                raw = store.db.execute("SELECT encrypted_tokens FROM youtube_accounts").fetchone()[0]
                self.assertNotIn(b"youtube-refresh", raw)
                store.delete_tokens(123, youtube=True)
                self.assertIsNone(store.get_tokens(123, youtube=True))
                self.assertEqual(store.get_tokens(123), {"access_token": "old"})
            finally:
                store.close()

    def test_youtube_config_is_optional_but_partial_or_wrong_callback_is_rejected(self):
        variables = {"DISCORD_BOT_TOKEN": "test", "TIKTOK_CLIENT_KEY": "test", "TIKTOK_CLIENT_SECRET": "test",
                     "TIKTOK_REDIRECT_URI": "https://bot.example.com/oauth/callback",
                     "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode()}
        with patch.dict(os.environ, variables, clear=True), patch("ugc_bot.config.load_dotenv"):
            self.assertEqual(load_config().youtube_client_id, "")
            os.environ["YOUTUBE_CLIENT_ID"] = "client"
            with self.assertRaisesRegex(ValueError, "all three"):
                load_config()
            os.environ["YOUTUBE_CLIENT_SECRET"] = "secret"
            os.environ["YOUTUBE_REDIRECT_URI"] = "https://bot.example.com/oauth/callback"
            with self.assertRaisesRegex(ValueError, "oauth/youtube/callback"):
                load_config()
            os.environ["YOUTUBE_REDIRECT_URI"] = "https://bot.example.com/oauth/youtube/callback"
            self.assertEqual(load_config().youtube_client_id, "client")

    def test_authorization_requests_upload_scope_offline_access_and_state(self):
        params = parse_qs(urlsplit(authorization_url("client", "https://host/oauth/youtube/callback", "state")).query)
        self.assertEqual(params["scope"], [SCOPE])
        self.assertEqual(params["access_type"], ["offline"])
        self.assertEqual(params["state"], ["state"])
        self.assertIn("consent", params["prompt"][0])

    def test_legacy_schedules_migrate_and_platform_cancellation_is_isolated(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "old.sqlite3"
            key = Fernet.generate_key()
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE scheduled_posts (id TEXT PRIMARY KEY, discord_user_id TEXT NOT NULL, "
                           "video_name TEXT NOT NULL, original_name TEXT NOT NULL, mime_type TEXT NOT NULL, "
                           "post_info TEXT NOT NULL, scheduled_at INTEGER NOT NULL, created_at INTEGER NOT NULL, "
                           "state TEXT NOT NULL DEFAULT 'pending', publish_id TEXT, error TEXT)")
                db.execute("INSERT INTO scheduled_posts VALUES "
                           "('old', '123', 'old.mp4', 'old.mp4', 'video/mp4', '{}', 100, 1, 'pending', NULL, NULL)")
            db.close()
            store = Storage(path, key)
            try:
                self.assertEqual(store.list_scheduled_posts(123)[0]["platform"], "tiktok")
                store.add_scheduled_post("short", 123, "short.mp4", "short.mp4", "video/mp4", {}, 100, platform="youtube")
                store.add_scheduled_post("foreign", 456, "foreign.mp4", "foreign.mp4", "video/mp4", {}, 100, platform="youtube")
                self.assertEqual(store.active_scheduled_count(123), 2)
                self.assertIsNone(store.cancel_scheduled_post(456, "short"))
                self.assertEqual(store.cancel_all_scheduled_posts(123), ["old.mp4"])
                self.assertEqual(store.list_scheduled_posts(123)[0]["platform"], "youtube")
                self.assertEqual(store.cancel_all_scheduled_posts(123, platform="youtube"), ["short.mp4"])
                self.assertEqual(store.active_scheduled_count(456), 1)
                self.assertTrue(store.claim_scheduled_post("foreign"))
                store.recover_interrupted_posts()
                self.assertEqual(store.list_scheduled_posts(456)[0]["state"], "uncertain")
                self.assertIn("YouTube", store.list_scheduled_posts(456)[0]["error"])
            finally:
                store.close()

    def test_refresh_preserves_refresh_token_and_rejects_wrong_scope(self):
        session = Session(Response(body={"access_token": "new", "expires_in": 3600}))
        api = YouTubeAPI(session, "client", "secret", "https://host/oauth/youtube/callback")
        tokens = run(api.refresh("saved-refresh"))
        self.assertEqual(tokens["refresh_token"], "saved-refresh")
        self.assertGreater(tokens["expires_at"], time.time())
        self.assertEqual(session.posts[0][1]["data"]["grant_type"], "refresh_token")
        with self.assertRaisesRegex(YouTubeError, "offline access"):
            run(api.exchange_code("code"))
        session.response.body["scope"] = "other-scope"
        with self.assertRaisesRegex(YouTubeError, "upload permission"):
            run(api.exchange_code("code"))
        session.response = Response(400, {"error": "invalid_grant"})
        with self.assertRaisesRegex(YouTubeError, "link-youtube"):
            run(api.refresh("expired"))


class ShortsValidationTests(unittest.TestCase):
    def metadata(self, width=1080, height=1920, duration="180", **video_fields):
        return {"streams": [{"codec_type": "video", "width": width, "height": height, **video_fields}],
                "format": {"duration": duration}}

    def test_square_vertical_rotated_and_anamorphic_display_sizes(self):
        for metadata in (self.metadata(), self.metadata(1080, 1080),
                         self.metadata(1920, 1080, side_data_list=[{"rotation": -90}]),
                         self.metadata(1920, 1080, tags={"rotate": "90"}),
                         self.metadata(1080, 1920, sample_aspect_ratio="1:1")):
            validate_short_metadata(metadata)
        for metadata in (self.metadata(1920, 1080),
                         self.metadata(1080, 1920, side_data_list=[{"rotation": 90}]),
                         self.metadata(1080, 1080, sample_aspect_ratio="2:1")):
            with self.assertRaisesRegex(YouTubeError, "square or vertical"):
                validate_short_metadata(metadata)

    def test_duration_limit_and_unreadable_metadata(self):
        with self.assertRaisesRegex(YouTubeError, "3 minutes"):
            validate_short_metadata(self.metadata(duration="180.001"))
        for metadata in ({}, {"streams": []}, self.metadata(duration="N/A"),
                         self.metadata(duration="nan"), self.metadata(duration="0"),
                         self.metadata(0, 1920), self.metadata(sample_aspect_ratio="unknown"),
                         self.metadata(tags={"rotate": "45"})):
            with self.subTest(metadata=metadata), self.assertRaisesRegex(YouTubeError, "verify Shorts"):
                validate_short_metadata(metadata)
        metadata = self.metadata(duration="N/A")
        metadata["streams"][0]["duration"] = "59.5"
        validate_short_metadata(metadata)
        metadata["streams"].append({"codec_type": "audio", "duration": "181"})
        with self.assertRaisesRegex(YouTubeError, "3 minutes"):
            validate_short_metadata(metadata)

    def test_probe_invocation_uses_local_file_and_rejects_corrupt_input(self):
        async def wait_inline(coro, timeout):
            return await coro

        process = SimpleNamespace(returncode=0, communicate=AsyncMock(
            return_value=(json.dumps(self.metadata()).encode(), b"")))
        with patch("ugc_bot.youtube.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)) as spawn:
            with patch("ugc_bot.youtube.asyncio.wait_for", wait_inline):
                run(validate_short(Path("clip.mp4"), "custom-ffprobe"))
                self.assertEqual(spawn.await_args.args[0], "custom-ffprobe")
                self.assertIn("-protocol_whitelist", spawn.await_args.args)
                self.assertEqual(spawn.await_args.args[-1], str(Path("clip.mp4").resolve()))
                process.returncode = 1
                with self.assertRaisesRegex(YouTubeError, "Could not read"):
                    run(validate_short(Path("clip.mp4")))

    def test_missing_probe_and_timeout_fail_closed(self):
        with patch("ugc_bot.youtube.asyncio.create_subprocess_exec", new=AsyncMock(side_effect=FileNotFoundError)):
            with self.assertRaisesRegex(YouTubeError, "ffprobe"):
                run(validate_short(Path("clip.mp4")))

        async def timed_out(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError

        process = SimpleNamespace(returncode=None, communicate=AsyncMock(return_value=(b"", b"")), kill=Mock())
        with patch("ugc_bot.youtube.asyncio.create_subprocess_exec", new=AsyncMock(return_value=process)):
            with patch("ugc_bot.youtube.asyncio.wait_for", timed_out):
                with self.assertRaisesRegex(YouTubeError, "timed out"):
                    run(validate_short(Path("clip.mp4")))
        process.kill.assert_called_once()

    def test_metadata_errors_identify_the_missing_or_invalid_field(self):
        for title, description, error in (("", "", "Enter a YouTube title"), ("x" * 101, "", "100 characters"),
                                          ("<title>", "", "title cannot"), ("title", "<description>", "description cannot")):
            with self.assertRaisesRegex(YouTubeError, error):
                validate_metadata(title, description)
        validate_metadata(" Valid title ", "")


class UploadTests(unittest.TestCase):
    def upload(self, session, data=b"test-video", **metadata):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(data)
            api = YouTubeAPI(session, "client", "secret", "https://host/oauth/youtube/callback")
            with patch("ugc_bot.youtube.asyncio.to_thread", inline):
                return run(api.post_video("access", path, "video/mp4", metadata.get("title", "Title"),
                                          metadata.get("description", "Description"),
                                          metadata.get("privacy", "private"), metadata.get("made_for_kids", False)))

    def test_upload_sends_metadata_and_exact_bytes(self):
        session = Session()
        self.assertEqual(self.upload(session), "video-123")
        payload = session.posts[0][1]
        self.assertEqual(payload["params"], {"uploadType": "resumable", "part": "snippet,status"})
        self.assertEqual(payload["json"]["status"], {"privacyStatus": "private", "selfDeclaredMadeForKids": False})
        self.assertEqual(payload["json"]["snippet"], {"title": "Title", "description": "Description"})
        self.assertEqual(session.uploads[0][1]["data"], b"test-video")
        self.assertEqual(session.uploads[0][1]["headers"]["Content-Range"], "bytes 0-9/10")
        self.assertFalse(session.uploads[0][1]["allow_redirects"])

    def test_chunked_upload_and_bad_acknowledgment(self):
        data = b"x" * CHUNK_SIZE + b"tail"
        session = Session(chunks=[Response(308, headers={"Range": f"bytes=0-{CHUNK_SIZE - 1}"}),
                                  Response(201, {"id": "video-123"})])
        self.assertEqual(self.upload(session, data), "video-123")
        self.assertEqual(session.uploads[1][1]["data"], b"tail")
        self.assertEqual(session.uploads[1][1]["headers"]["Content-Range"],
                         f"bytes {CHUNK_SIZE}-{CHUNK_SIZE + 3}/{CHUNK_SIZE + 4}")
        session = Session(chunks=[Response(308, headers={"Range": "bytes=0-3"})])
        with self.assertRaisesRegex(YouTubeError, "acknowledge"):
            self.upload(session, data)
        self.assertEqual(len(session.uploads), 1)

    def test_untrusted_upload_locations_never_receive_tokens_or_bytes(self):
        for location in ("", "http://www.googleapis.com/upload/youtube/v3/videos",
                         "https://evil.example/video?secret=test", "https://127.0.0.1/video",
                         "https://www.googleapis.com:invalid/upload/youtube/v3/videos",
                         "https://user@www.googleapis.com/upload/youtube/v3/videos"):
            with self.subTest(location=location):
                session = Session(Response(headers={"Location": location}))
                with self.assertRaises(YouTubeError):
                    self.upload(session)
                self.assertEqual(session.uploads, [])

    def test_bad_metadata_prevents_any_network_request(self):
        for metadata in ({"title": " "}, {"title": "x" * 101}, {"description": "<invalid>"},
                         {"description": "😀" * 1251}, {"privacy": "other"}, {"made_for_kids": None}):
            with self.subTest(metadata=metadata):
                session = Session()
                with self.assertRaises(YouTubeError):
                    self.upload(session, **metadata)
                self.assertEqual(session.posts, [])

    def test_transfer_failure_is_not_retried_and_missing_id_is_uncertain(self):
        for response in (Response(503), Response(201, {})):
            session = Session(chunks=[response])
            with self.assertRaisesRegex(YouTubeError, "Check your channel"):
                self.upload(session)
            self.assertEqual(len(session.uploads), 1)


class MentionFlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.bot = UploadBot(config(self.directory.name))
        self.bot.youtube = SimpleNamespace(post_video=AsyncMock(return_value="video-123"),
                                           refresh=AsyncMock(return_value={"access_token": "new",
                                                                         "refresh_token": "refresh",
                                                                         "expires_at": int(time.time()) + 3600}),
                                           exchange_code=AsyncMock(return_value={"access_token": "youtube"}),
                                           revoke=AsyncMock())
        self.bot.tiktok = SimpleNamespace(creator_info=AsyncMock(return_value={"privacy_level_options": ["SELF_ONLY"]}),
                                          post_video=AsyncMock())
        self.bot._access_token = AsyncMock(return_value="tiktok")
        probe = patch("ugc_bot.bot.validate_short", new=AsyncMock())
        self.short_probe = probe.start()
        self.addCleanup(probe.stop)

    def tearDown(self):
        self.bot.storage.close()
        self.directory.cleanup()

    def link_youtube(self, expired=False):
        self.bot.storage.save_tokens(123, {"access_token": "youtube", "refresh_token": "refresh",
                                          "expires_at": 0 if expired else int(time.time()) + 3600}, youtube=True)

    def mention(self, msg):
        with patch.object(UploadBot, "user", new_callable=PropertyMock, return_value=SimpleNamespace(id=999)):
            run(self.bot.on_message(msg))

    def test_link_command_exists_and_upload_is_a_mention(self):
        commands = {command.name for command in self.bot.tree.get_commands()}
        self.assertIn("link-youtube", commands)
        self.assertIn("unlink-youtube", commands)
        self.assertNotIn("upload-youtube", commands)

    def test_both_accounts_open_both_question_panels_without_posting(self):
        self.link_youtube()
        self.bot.storage.save_tokens(123, {"scope": "video.publish"})
        msg = message()
        self.mention(msg)
        panels = [call.kwargs["view"] for call in msg.reply.await_args_list]
        self.assertEqual([type(view) for view in panels], [DirectPostView, YouTubePostView])
        self.bot.youtube.post_video.assert_not_awaited()
        self.bot.tiktok.post_video.assert_not_awaited()

    def test_youtube_only_and_tiktok_preparation_failure_still_open_youtube(self):
        self.link_youtube()
        msg = message()
        self.mention(msg)
        self.assertIsInstance(msg.reply.await_args.kwargs["view"], YouTubePostView)
        self.bot.tiktok.creator_info.assert_not_awaited()
        self.bot.storage.save_tokens(123, {"scope": "video.publish"})
        self.bot.tiktok.creator_info.side_effect = TikTokError("TikTok unavailable")
        msg = message()
        self.mention(msg)
        self.assertEqual(msg.reply.await_count, 2)
        self.assertIsInstance(msg.reply.await_args.kwargs["view"], YouTubePostView)

    def test_no_accounts_and_unmentioned_guild_messages(self):
        msg = message()
        self.mention(msg)
        self.assertIn("/link-youtube", msg.reply.await_args.args[0])
        msg = message(guild=object())
        self.mention(msg)
        msg.reply.assert_not_awaited()

    def test_settings_require_title_privacy_and_explicit_audience_and_owner(self):
        view = YouTubePostView(self.bot, message())
        self.assertIsNotNone(view.selection_error())
        view.title = "My Short"
        self.assertIn("visibility", view.selection_error())
        view.privacy = "private"
        self.assertIn("made for kids", view.selection_error())
        view.made_for_kids = False
        self.assertIsNone(view.selection_error())
        stranger = interaction(456)
        self.assertFalse(run(view.interaction_check(stranger)))
        stranger.response.send_message.assert_awaited_once()
        self.assertTrue(run(view.interaction_check(interaction())))

    def test_publish_requires_answers_and_prevents_double_submission(self):
        view = YouTubePostView(self.bot, message())
        self.bot.post_youtube_video = AsyncMock()
        current = interaction()
        run(view.publish.callback(current))
        self.bot.post_youtube_video.assert_not_awaited()
        self.assertIsInstance(current.response.send_modal.await_args.args[0], YouTubeDetailsModal)
        view.title, view.privacy, view.made_for_kids = "Title", "public", False
        run(view.publish.callback(current))
        run(view.publish.callback(current))
        self.bot.post_youtube_video.assert_awaited_once()
        self.assertTrue(view.submitted)
        self.assertTrue(all(item.disabled for item in view.children))

    def test_schedule_prompts_for_title_then_uses_the_saved_time_zone(self):
        view = YouTubePostView(self.bot, message())
        current = interaction()
        run(view.schedule.callback(current))
        self.assertIsInstance(current.response.send_modal.await_args.args[0], YouTubeDetailsModal)
        self.bot.storage.set_time_zone(123, "America/Indianapolis")
        view.title, view.privacy, view.made_for_kids = "Short title", "public", False
        run(view.schedule.callback(current))
        modal = current.response.send_modal.await_args.args[0]
        self.assertIsInstance(modal, ScheduleModal)
        self.assertIn("YouTube Short", modal.title)
        self.assertEqual(modal.time_zone, "America/Indianapolis")

    def test_schedule_modal_saves_once_at_the_expected_utc_time(self):
        self.link_youtube()
        self.bot.storage.set_time_zone(123, "America/Indianapolis")
        view = YouTubePostView(self.bot, message())
        view.title, view.privacy, view.made_for_kids = "Short title", "public", False
        self.bot._download = AsyncMock(side_effect=lambda _, path: path.write_bytes(b"test-video"))
        current = interaction()
        modal = ScheduleModal(view, current.message, "America/Indianapolis")
        modal.when._value = "2026-10-12 14:30"
        now = int(datetime(2026, 10, 10, 12, tzinfo=timezone.utc).timestamp())
        with patch("ugc_bot.bot.time.time", return_value=now):
            run(modal.on_submit(current))
            run(modal.on_submit(current))
        posts = self.bot.storage.list_scheduled_posts(123)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["scheduled_at"], int(datetime(2026, 10, 12, 18, 30, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(posts[0]["platform"], "youtube")
        self.assertTrue(view.submitted)
        self.assertTrue(all(item.disabled for item in view.children))
        self.bot.youtube.post_video.assert_not_awaited()
        self.assertIn("YouTube Short", current.message.edit.await_args.kwargs["content"])

    def test_schedule_modal_rejects_changed_zone_and_foreign_sender(self):
        view = YouTubePostView(self.bot, message())
        view.title, view.privacy, view.made_for_kids = "Short title", "public", False
        self.bot.storage.set_time_zone(123, "America/Indianapolis")
        modal = ScheduleModal(view, interaction().message, "America/Indianapolis")
        run(modal.on_submit(interaction(456)))
        self.assertEqual(self.bot.storage.active_scheduled_count(123), 0)
        self.bot.storage.set_time_zone(123, "UTC")
        current = interaction()
        run(modal.on_submit(current))
        self.assertIn("time zone changed", current.response.send_message.await_args.args[0])

    def save_short(self):
        self.link_youtube()
        view = YouTubePostView(self.bot, message())
        view.title, view.description, view.privacy, view.made_for_kids = "Saved title", "Saved description", "unlisted", False
        self.bot._download = AsyncMock(side_effect=lambda _, path: path.write_bytes(b"test-video"))
        when = int(time.time()) + 120
        post_id = run(self.bot.schedule_direct_video(view.message, view, when))
        view.title = "Changed after scheduling"
        return post_id, when

    def test_scheduled_short_survives_restart_and_uploads_once_with_saved_metadata(self):
        post_id, when = self.save_short()
        self.bot.youtube.post_video.assert_not_awaited()
        self.assertEqual(self.bot.storage.due_scheduled_posts(when - 1), [])
        previous_config, youtube = self.bot.config, self.bot.youtube
        self.bot.storage.close()
        self.bot = UploadBot(previous_config)
        self.bot.youtube = youtube
        self.bot._notify_scheduled = AsyncMock()
        self.link_youtube(expired=True)
        post = self.bot.storage.due_scheduled_posts(when)[0]
        run(self.bot._process_scheduled_post(post))
        run(self.bot._process_scheduled_post(post))
        youtube.post_video.assert_awaited_once()
        self.assertEqual(youtube.post_video.await_args.args[3:], ("Saved title", "Saved description", "unlisted", False))
        youtube.refresh.assert_awaited_once_with("refresh")
        stored = self.bot.storage.list_scheduled_posts(123)[0]
        self.assertEqual(stored["state"], "uploaded")
        self.assertEqual(stored["publish_id"], "video-123")
        self.assertEqual(self.bot.storage.due_scheduled_posts(when), [])
        self.assertFalse((self.bot.scheduled_dir / f"{post_id}.mp4").exists())
        self.assertIn("shorts/video-123", self.bot._notify_scheduled.await_args.args[1])

    def test_short_rejected_before_queuing_cleans_partial_video(self):
        self.link_youtube()
        self.short_probe.side_effect = YouTubeError("YouTube Shorts must be square or vertical.")
        with self.assertRaisesRegex(YouTubeError, "square or vertical"):
            self.save_short()
        self.assertEqual(self.bot.storage.list_scheduled_posts(123), [])
        self.assertEqual(list(self.bot.scheduled_dir.iterdir()), [])

    def test_due_short_is_rechecked_and_preflight_failure_is_not_uploaded(self):
        post_id, when = self.save_short()
        self.short_probe.side_effect = YouTubeError("Saved video is landscape")
        self.bot._notify_scheduled = AsyncMock()
        run(self.bot._process_scheduled_post(self.bot.storage.due_scheduled_posts(when)[0]))
        self.assertEqual(self.bot.storage.list_scheduled_posts(123)[0]["state"], "failed")
        self.assertFalse((self.bot.scheduled_dir / f"{post_id}.mp4").exists())
        self.bot.youtube.post_video.assert_not_awaited()

    def test_refresh_network_failure_retries_but_transfer_failure_is_uncertain(self):
        post_id, when = self.save_short()
        self.link_youtube(expired=True)
        self.bot._notify_scheduled = AsyncMock()
        self.bot.youtube.refresh.side_effect = aiohttp.ClientError("offline")
        run(self.bot._process_scheduled_post(self.bot.storage.due_scheduled_posts(when)[0]))
        self.assertEqual(self.bot.storage.list_scheduled_posts(123)[0]["state"], "pending")
        self.assertTrue((self.bot.scheduled_dir / f"{post_id}.mp4").exists())
        self.bot.youtube.post_video.assert_not_awaited()
        self.bot.youtube.refresh.side_effect = None
        self.bot.youtube.post_video.side_effect = aiohttp.ClientError("transfer interrupted")
        run(self.bot._process_scheduled_post(self.bot.storage.due_scheduled_posts(when)[0]))
        self.assertEqual(self.bot.storage.list_scheduled_posts(123)[0]["state"], "uncertain")
        self.assertFalse((self.bot.scheduled_dir / f"{post_id}.mp4").exists())
        self.assertEqual(self.bot.storage.due_scheduled_posts(when), [])

    def test_cancel_scheduled_short_and_unlink_preserve_other_platform(self):
        post_id, when = self.save_short()
        stranger = interaction(456)
        run(self.bot.tree.get_command("cancel-post").callback(stranger, post_id))
        self.assertEqual(self.bot.storage.active_scheduled_count(123), 1)
        run(self.bot.tree.get_command("cancel-post").callback(interaction(), post_id))
        self.assertEqual(self.bot.storage.active_scheduled_count(123), 0)
        self.assertFalse((self.bot.scheduled_dir / f"{post_id}.mp4").exists())
        self.save_short()
        tiktok_path = self.bot.scheduled_dir / "tiktok.mp4"
        tiktok_path.write_bytes(b"video")
        self.bot.storage.add_scheduled_post("tiktok", 123, "tiktok.mp4", "tiktok.mp4", "video/mp4", {}, when)
        run(self.bot.tree.get_command("unlink-youtube").callback(interaction()))
        self.assertEqual(self.bot.storage.list_scheduled_posts(123)[0]["platform"], "tiktok")
        self.assertTrue(tiktok_path.exists())

    def test_unlink_tiktok_preserves_scheduled_youtube_short(self):
        post_id, _ = self.save_short()
        self.bot.storage.save_tokens(123, {"access_token": "tiktok"})
        self.bot.tiktok.revoke = AsyncMock()
        run(self.bot.tree.get_command("unlink").callback(interaction()))
        self.assertEqual(self.bot.storage.active_scheduled_count(123), 1)
        self.assertTrue((self.bot.scheduled_dir / f"{post_id}.mp4").exists())
        self.assertIsNotNone(self.bot.storage.get_tokens(123, youtube=True))

    def test_details_modal_saves_answers_and_rejects_stale_or_foreign_submissions(self):
        view = YouTubePostView(self.bot, message())
        modal = YouTubeDetailsModal(view)
        self.assertLessEqual(modal.description.max_length, 4000)
        modal.video_title._value = "My Short"
        modal.description._value = "My description"
        current = interaction()
        run(modal.on_submit(current))
        self.assertEqual((view.title, view.description), ("My Short", "My description"))
        stranger = interaction(456)
        modal.video_title._value = "Changed"
        run(modal.on_submit(stranger))
        self.assertEqual(view.title, "My Short")
        with patch.object(view, "is_finished", return_value=True):
            run(modal.on_submit(current))
        self.assertEqual(view.title, "My Short")
        self.assertIn("closed", current.response.send_message.await_args.args[0])

    def test_post_refreshes_only_youtube_and_removes_temporary_video(self):
        self.link_youtube(expired=True)
        self.bot.storage.save_tokens(123, {"access_token": "tiktok"})
        paths = []

        async def download(_, path):
            paths.append(path)
            path.write_bytes(b"test-video")

        self.bot._download = download
        view = YouTubePostView(self.bot, message())
        view.title, view.privacy, view.made_for_kids = "Title", "unlisted", False
        progress = SimpleNamespace(edit=AsyncMock())
        run(self.bot.post_youtube_video(progress, view.message, view))
        self.bot.youtube.refresh.assert_awaited_once_with("refresh")
        self.assertEqual(self.bot.storage.get_tokens(123)["access_token"], "tiktok")
        self.assertEqual(self.bot.storage.get_tokens(123, youtube=True)["access_token"], "new")
        self.assertIn("shorts/video-123", progress.edit.await_args.kwargs["content"])
        self.assertFalse(paths[0].exists())
        self.bot.tiktok.post_video.assert_not_awaited()

    def test_unlinking_after_panel_prevents_post(self):
        view = YouTubePostView(self.bot, message())
        view.title, view.privacy, view.made_for_kids = "Title", "private", False
        progress = SimpleNamespace(edit=AsyncMock())
        run(self.bot.post_youtube_video(progress, view.message, view))
        self.assertIn("link-youtube", progress.edit.await_args.kwargs["content"])
        self.bot.youtube.post_video.assert_not_awaited()

    def test_oauth_callback_rejects_tiktok_state_and_replay(self):
        tiktok_state = self.bot.storage.new_state(123)
        response = run(self.bot.youtube_oauth_callback(SimpleNamespace(query={"state": tiktok_state, "code": "code"})))
        self.assertEqual(response.status, 400)
        self.bot.youtube.exchange_code.assert_not_awaited()
        youtube_state = self.bot.storage.new_state(123, youtube=True)
        request = SimpleNamespace(query={"state": youtube_state, "code": "code"})
        self.assertEqual(run(self.bot.youtube_oauth_callback(request)).status, 200)
        self.assertEqual(run(self.bot.youtube_oauth_callback(request)).status, 400)
        self.bot.youtube.exchange_code.assert_awaited_once_with("code")
        self.assertEqual(self.bot.storage.get_tokens(123, youtube=True), {"access_token": "youtube"})
        self.assertEqual(self.bot.storage.consume_state(tiktok_state), 123)

    def test_unlink_revokes_youtube_without_removing_tiktok(self):
        self.link_youtube()
        self.bot.storage.save_tokens(123, {"access_token": "tiktok"})
        run(self.bot.tree.get_command("unlink-youtube").callback(interaction()))
        self.bot.youtube.revoke.assert_awaited_once_with("refresh")
        self.assertIsNone(self.bot.storage.get_tokens(123, youtube=True))
        self.assertEqual(self.bot.storage.get_tokens(123)["access_token"], "tiktok")


if __name__ == "__main__":
    unittest.main()
