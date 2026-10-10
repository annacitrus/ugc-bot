import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet

from ugc_bot.bot import CaptionModal, DirectPostView, UploadBot, is_direct_post_request, parse_schedule_time
from ugc_bot.config import Config, load_config
from ugc_bot.storage import Storage
from ugc_bot.tiktok import TikTokAPI, TikTokError, authorization_url, chunk_ranges


class FakeResponse:
    def __init__(self, status, body=None):
        self.status = status
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def json(self, **_):
        return self.body


class FakeSession:
    def __init__(self, upload_url="https://open-upload.tiktokapis.com/video/?upload_id=test&upload_token=test"):
        self.uploads = []
        self.upload_url = upload_url

    def post(self, url, **kwargs):
        self.init_url = url
        self.init_payload = kwargs["json"]
        return FakeResponse(
            200,
            {
                "error": {"code": "ok"},
                "data": {
                    "publish_id": "test-publish-id",
                    "upload_url": self.upload_url,
                },
            },
        )

    def put(self, url, **kwargs):
        self.uploads.append((url, kwargs))
        return FakeResponse(201)


async def run_inline(function, *args):
    return function(*args)


def run_fake_coroutine(coroutine):
    # Fake responses complete immediately, so this exercises async code without a network event loop.
    try:
        coroutine.send(None)
    except StopIteration as finished:
        return finished.value
    else:
        coroutine.close()
        raise AssertionError("Fake API unexpectedly waited for external I/O")


class StorageTests(unittest.TestCase):
    def test_state_is_one_use_and_tokens_are_encrypted(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            database = Path(directory) / "bot.sqlite3"
            store = Storage(database, Fernet.generate_key())
            state = store.new_state(123)
            self.assertEqual(store.consume_state(state), 123)
            self.assertIsNone(store.consume_state(state))
            store.save_tokens(123, {"access_token": "secret-value"})
            self.assertEqual(store.get_tokens(123)["access_token"], "secret-value")
            raw = store.db.execute("SELECT encrypted_tokens FROM accounts").fetchone()[0]
            self.assertNotIn(b"secret-value", raw)
            store.delete_tokens(123)
            self.assertIsNone(store.get_tokens(123))
            store.close()

    def test_scheduled_post_claim_cancel_and_recovery(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = Storage(Path(directory) / "bot.sqlite3", Fernet.generate_key())
            store.add_scheduled_post("one", 123, "one.mp4", "one.mp4", "video/mp4", {}, 100)
            store.add_scheduled_post("two", 123, "two.mp4", "two.mp4", "video/mp4", {}, 200)
            self.assertEqual(store.active_scheduled_count(123), 2)
            self.assertEqual(store.cancel_scheduled_post(123, "two"), "two.mp4")
            self.assertEqual(store.cancel_scheduled_post(456, "one"), None)
            self.assertEqual(store.due_scheduled_posts(100)[0]["id"], "one")
            self.assertTrue(store.claim_scheduled_post("one"))
            self.assertFalse(store.claim_scheduled_post("one"))
            self.assertEqual(store.recover_interrupted_posts(), [("one", 123, "one.mp4")])
            self.assertEqual(store.list_scheduled_posts(123)[0]["state"], "uncertain")
            store.close()


class ConfigTests(unittest.TestCase):
    def test_railway_uses_public_listener_port_and_volume(self):
        variables = {
            "DISCORD_BOT_TOKEN": "test",
            "TIKTOK_CLIENT_KEY": "test",
            "TIKTOK_CLIENT_SECRET": "test",
            "TIKTOK_REDIRECT_URI": "https://bot.up.railway.app/oauth/callback",
            "TOKEN_ENCRYPTION_KEY": Fernet.generate_key().decode(),
            "RAILWAY_ENVIRONMENT_ID": "test-environment",
            "RAILWAY_VOLUME_MOUNT_PATH": "/data",
            "HTTP_HOST": "127.0.0.1",
            "PORT": "12345",
        }
        with patch.dict(os.environ, variables, clear=True), patch("ugc_bot.config.load_dotenv"):
            config = load_config()
        self.assertEqual(config.http_host, "0.0.0.0")
        self.assertEqual(config.http_port, 12345)
        self.assertEqual(config.database_path, Path("/data/bot.sqlite3"))


class BotStartupTests(unittest.TestCase):
    def test_discord_http_client_is_not_replaced(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = Config(
                discord_token="test",
                tiktok_client_key="test",
                tiktok_client_secret="test",
                tiktok_redirect_uri="https://bot.up.railway.app/oauth/callback",
                encryption_key=Fernet.generate_key(),
                database_path=Path(directory) / "bot.sqlite3",
                http_host="127.0.0.1",
                http_port=8080,
                discord_guild_id=None,
                max_video_bytes=100_000_000,
            )
            bot = UploadBot(config)
            try:
                self.assertIsNotNone(bot.http)
                self.assertTrue(hasattr(bot.http, "loop"))
                self.assertIsNone(bot.tiktok_session)
            finally:
                bot.storage.close()


class TikTokValueTests(unittest.TestCase):
    def test_schedule_time_requires_offset_and_future_window(self):
        now = int(datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc).timestamp())
        expected = int(datetime(2026, 10, 12, 18, 30, tzinfo=timezone.utc).timestamp())
        self.assertEqual(parse_schedule_time("2026-10-12 14:30 -04:00", now), expected)
        with self.assertRaisesRegex(ValueError, "UTC offset"):
            parse_schedule_time("2026-10-12 14:30", now)
        with self.assertRaisesRegex(ValueError, "future"):
            parse_schedule_time("2026-10-10 08:00 -04:00", now)

    def test_chunk_boundaries(self):
        self.assertEqual(chunk_ranges(4_000_000), [(0, 4_000_000)])
        self.assertEqual(chunk_ranges(64_000_000), [(0, 64_000_000)])
        self.assertEqual(
            chunk_ranges(65_000_123),
            [(0, 10_000_000), (10_000_000, 10_000_000),
             (20_000_000, 10_000_000), (30_000_000, 10_000_000),
             (40_000_000, 10_000_000), (50_000_000, 15_000_123)],
        )

    def test_auth_url_uses_upload_scope_and_state(self):
        url = authorization_url("key", "https://example.com/oauth/callback", "random-state")
        self.assertIn("scope=video.upload", url)
        self.assertIn("state=random-state", url)

    def test_direct_post_phrase_must_be_exact(self):
        self.assertTrue(is_direct_post_request("<@123> post this", 123))
        self.assertTrue(is_direct_post_request("post this <@!123>", 123))
        self.assertFalse(is_direct_post_request("<@123> please post this", 123))
        self.assertFalse(is_direct_post_request("<@123> Post this", 123))
        self.assertFalse(is_direct_post_request("<@123> post this later", 123))

    def test_direct_post_authorization_requests_both_scopes(self):
        url = authorization_url(
            "key", "https://example.com/oauth/callback", "random-state", ("video.upload", "video.publish")
        )
        self.assertIn("scope=video.upload%2Cvideo.publish", url)

    def test_direct_post_requires_privacy_selection(self):
        message = SimpleNamespace(author=SimpleNamespace(id=123))
        view = DirectPostView(None, message, {"privacy_level_options": ["SELF_ONLY"]})
        self.assertIsNone(view.privacy)
        self.assertFalse(view.submitted)
        self.assertEqual(view.commercial, "none")
        self.assertEqual(CaptionModal(view).caption.default, "")


class TikTokUploadTests(unittest.TestCase):
    def test_missing_upload_url_is_distinct_from_untrusted_url(self):
        session = FakeSession(upload_url=None)
        api = TikTokAPI(session, "key", "secret", "https://example.com/oauth/callback")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"test-video")
            with self.assertRaisesRegex(TikTokError, "did not return an upload URL"):
                run_fake_coroutine(api.post_video("access-token", path, "video/mp4", {"privacy_level": "SELF_ONLY"}))
        self.assertEqual(session.uploads, [])

    def test_tiktok_supplied_https_upload_host_is_used(self):
        upload_url = "https://upload.cdn.example.com/video/?upload_token=secret-token"
        session = FakeSession(upload_url=upload_url)
        api = TikTokAPI(session, "key", "secret", "https://example.com/oauth/callback")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"test-video")
            with patch("ugc_bot.tiktok.asyncio.to_thread", run_inline):
                run_fake_coroutine(api.post_video("access-token", path, "video/mp4", {"privacy_level": "SELF_ONLY"}))
        self.assertEqual(session.uploads[0][0], upload_url)

    def test_local_upload_url_reports_only_hostname(self):
        session = FakeSession(upload_url="https://127.0.0.1/video/?upload_token=secret-token")
        api = TikTokAPI(session, "key", "secret", "https://example.com/oauth/callback")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"test-video")
            with self.assertRaises(TikTokError) as raised:
                run_fake_coroutine(api.post_video("access-token", path, "video/mp4", {"privacy_level": "SELF_ONLY"}))
        self.assertIn("127.0.0.1", str(raised.exception))
        self.assertNotIn("secret-token", str(raised.exception))
        self.assertEqual(session.uploads, [])

    def test_inbox_upload_sends_exact_range(self):
        session = FakeSession()
        api = TikTokAPI(session, "key", "secret", "https://example.com/oauth/callback")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"test-video")
            with patch("ugc_bot.tiktok.asyncio.to_thread", run_inline):
                publish_id = run_fake_coroutine(api.upload_video("access-token", path, "video/mp4"))
        self.assertEqual(publish_id, "test-publish-id")
        self.assertEqual(session.init_payload["source_info"]["video_size"], 10)
        self.assertEqual(session.uploads[0][1]["headers"]["Content-Range"], "bytes 0-9/10")
        self.assertEqual(session.uploads[0][1]["data"], b"test-video")

    def test_direct_post_uses_publish_endpoint_and_metadata(self):
        session = FakeSession()
        api = TikTokAPI(session, "key", "secret", "https://example.com/oauth/callback")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"test-video")
            with patch("ugc_bot.tiktok.asyncio.to_thread", run_inline):
                publish_id = run_fake_coroutine(
                    api.post_video(
                        "access-token", path, "video/mp4", {"privacy_level": "SELF_ONLY", "title": "My video"}
                    )
                )
        self.assertEqual(publish_id, "test-publish-id")
        self.assertTrue(session.init_url.endswith("/v2/post/publish/video/init/"))
        self.assertEqual(session.init_payload["post_info"]["privacy_level"], "SELF_ONLY")
        self.assertEqual(session.uploads[0][1]["data"], b"test-video")


class ScheduledPostingTests(unittest.TestCase):
    def test_saved_video_posts_once_and_is_removed(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = Config(
                discord_token="test", tiktok_client_key="test", tiktok_client_secret="test",
                tiktok_redirect_uri="https://bot.example.com/oauth/callback",
                encryption_key=Fernet.generate_key(), database_path=Path(directory) / "bot.sqlite3",
                http_host="127.0.0.1", http_port=8080, discord_guild_id=None,
                max_video_bytes=100_000_000,
            )
            bot = UploadBot(config)
            bot.scheduled_dir.mkdir()
            bot.storage.save_tokens(123, {
                "access_token": "token", "expires_at": int(time.time()) + 3600,
                "scope": "video.upload,video.publish",
            })
            calls = []
            notices = []

            class FakeTikTok:
                async def creator_info(self, _):
                    return {"privacy_level_options": ["SELF_ONLY"]}

                async def post_video(self, _, path, mime_type, post_info):
                    calls.append((path.read_bytes(), mime_type, post_info))
                    return "publish-123"

                async def status(self, _, publish_id):
                    self_publish_id = publish_id
                    return {"status": "PUBLISH_COMPLETE"} if self_publish_id == "publish-123" else {}

            async def fake_download(_, path):
                path.write_bytes(b"video-data")

            async def fake_notify(user_id, message):
                notices.append((user_id, message))

            bot.tiktok = FakeTikTok()
            bot._download = fake_download
            bot._notify_scheduled = fake_notify
            message = SimpleNamespace(
                author=SimpleNamespace(id=123),
                attachments=[SimpleNamespace(filename="video.mp4")],
            )
            settings = SimpleNamespace(
                creator={"privacy_level_options": ["SELF_ONLY"]},
                post_info=lambda _: {"privacy_level": "SELF_ONLY", "title": "Scheduled"},
            )
            scheduled_at = int(time.time()) + 120
            try:
                post_id = run_fake_coroutine(bot.schedule_direct_video(message, settings, scheduled_at))
                self.assertTrue((bot.scheduled_dir / f"{post_id}.mp4").exists())
                post = bot.storage.due_scheduled_posts(scheduled_at)[0]
                run_fake_coroutine(bot._process_scheduled_post(post))
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][0], b"video-data")
                self.assertEqual(bot.storage.list_scheduled_posts(123)[0]["state"], "posted")
                self.assertFalse((bot.scheduled_dir / f"{post_id}.mp4").exists())
                self.assertEqual(len(notices), 1)
                run_fake_coroutine(bot._process_scheduled_post(post))
                self.assertEqual(len(calls), 1)

                class UncertainTikTok(FakeTikTok):
                    async def post_video(self, *_):
                        raise TikTokError("transfer interrupted")

                bot.tiktok = UncertainTikTok()
                uncertain_id = run_fake_coroutine(bot.schedule_direct_video(message, settings, scheduled_at))
                uncertain_post = bot.storage.due_scheduled_posts(scheduled_at)[0]
                run_fake_coroutine(bot._process_scheduled_post(uncertain_post))
                uncertain_row = next(
                    row for row in bot.storage.list_scheduled_posts(123) if row["id"] == uncertain_id
                )
                self.assertEqual(uncertain_row["state"], "uncertain")
                self.assertFalse((bot.scheduled_dir / f"{uncertain_id}.mp4").exists())
                self.assertEqual(bot.storage.due_scheduled_posts(scheduled_at), [])
            finally:
                bot.storage.close()
