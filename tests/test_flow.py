import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet

from ugc_bot.bot import CaptionModal, DirectPostView, UploadBot, is_direct_post_request
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
