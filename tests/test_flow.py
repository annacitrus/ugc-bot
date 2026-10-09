import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from ugc_bot.storage import Storage
from ugc_bot.tiktok import TikTokAPI, authorization_url, chunk_ranges


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
    def __init__(self):
        self.uploads = []

    def post(self, url, **kwargs):
        self.init_payload = kwargs["json"]
        return FakeResponse(
            200,
            {
                "error": {"code": "ok"},
                "data": {
                    "publish_id": "test-publish-id",
                    "upload_url": "https://open-upload.tiktokapis.com/video/?upload_id=test&upload_token=test",
                },
            },
        )

    def put(self, url, **kwargs):
        self.uploads.append((url, kwargs))
        return FakeResponse(201)


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


class TikTokTests(unittest.IsolatedAsyncioTestCase):
    def test_chunk_boundaries(self):
        self.assertEqual(chunk_ranges(4_000_000), [(0, 4_000_000)])
        self.assertEqual(chunk_ranges(64_000_000), [(0, 64_000_000)])
        self.assertEqual(
            chunk_ranges(65_000_123),
            [(0, 10_000_000), (10_000_000, 10_000_000),
             (20_000_000, 10_000_000), (30_000_000, 10_000_000),
             (40_000_000, 10_000_000), (50_000_000, 15_000_123)],
        )

    async def test_inbox_upload_sends_exact_range(self):
        session = FakeSession()
        api = TikTokAPI(session, "key", "secret", "https://example.com/oauth/callback")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"test-video")
            publish_id = await api.upload_video("access-token", path, "video/mp4")
        self.assertEqual(publish_id, "test-publish-id")
        self.assertEqual(session.init_payload["source_info"]["video_size"], 10)
        self.assertEqual(session.uploads[0][1]["headers"]["Content-Range"], "bytes 0-9/10")
        self.assertEqual(session.uploads[0][1]["data"], b"test-video")

    def test_auth_url_uses_upload_scope_and_state(self):
        url = authorization_url("key", "https://example.com/oauth/callback", "random-state")
        self.assertIn("scope=video.upload", url)
        self.assertIn("state=random-state", url)
