import copy
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch
from urllib.parse import parse_qs, urlsplit

import discord
from cryptography.fernet import Fernet

from ugc_bot.bot import DirectPostView, UploadBot, YouTubeDetailsModal, YouTubePostView
from ugc_bot.config import Config, load_config
from ugc_bot.storage import Storage
from ugc_bot.tiktok import TikTokError
from ugc_bot.youtube import CHUNK_SIZE, SCOPE, YouTubeAPI, YouTubeError, authorization_url


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
        view.title, view.privacy, view.made_for_kids = "Title", "public", False
        run(view.publish.callback(current))
        run(view.publish.callback(current))
        self.bot.post_youtube_video.assert_awaited_once()
        self.assertTrue(view.submitted)
        self.assertTrue(all(item.disabled for item in view.children))

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
        self.assertIn("watch?v=video-123", progress.edit.await_args.kwargs["content"])
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
