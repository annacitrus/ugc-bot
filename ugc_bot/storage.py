from __future__ import annotations

import json
import secrets
import sqlite3
import time
from pathlib import Path

from cryptography.fernet import Fernet


class Storage:
    def __init__(self, path: Path, encryption_key: bytes):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS oauth_states (
                state TEXT PRIMARY KEY,
                discord_user_id TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS accounts (
                discord_user_id TEXT PRIMARY KEY,
                encrypted_tokens BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS youtube_accounts (
                discord_user_id TEXT PRIMARY KEY,
                encrypted_tokens BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS youtube_oauth_states (
                state TEXT PRIMARY KEY,
                discord_user_id TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_settings (
                discord_user_id TEXT PRIMARY KEY,
                time_zone TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS scheduled_posts (
                id TEXT PRIMARY KEY,
                discord_user_id TEXT NOT NULL,
                video_name TEXT NOT NULL,
                original_name TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                post_info TEXT NOT NULL,
                scheduled_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                publish_id TEXT,
                error TEXT
            );
            CREATE INDEX IF NOT EXISTS scheduled_posts_due
                ON scheduled_posts(state, scheduled_at);
            """
        )
        self.crypto = Fernet(encryption_key)

    def new_state(self, discord_user_id: int, *, youtube: bool = False) -> str:
        table = "youtube_oauth_states" if youtube else "oauth_states"
        state = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute(f"DELETE FROM {table} WHERE expires_at < ?", (int(time.time()),))
            self.db.execute(
                f"INSERT INTO {table} VALUES (?, ?, ?)",
                (state, str(discord_user_id), int(time.time()) + 600),
            )
        return state

    def consume_state(self, state: str, *, youtube: bool = False) -> int | None:
        table = "youtube_oauth_states" if youtube else "oauth_states"
        with self.db:
            row = self.db.execute(
                f"DELETE FROM {table} WHERE state = ? AND expires_at >= ? RETURNING discord_user_id",
                (state, int(time.time())),
            ).fetchone()
        return int(row[0]) if row else None

    def save_tokens(self, discord_user_id: int, tokens: dict, *, youtube: bool = False) -> None:
        table = "youtube_accounts" if youtube else "accounts"
        ciphertext = self.crypto.encrypt(json.dumps(tokens).encode("utf-8"))
        with self.db:
            self.db.execute(
                f"INSERT INTO {table} VALUES (?, ?) ON CONFLICT(discord_user_id) "
                "DO UPDATE SET encrypted_tokens = excluded.encrypted_tokens",
                (str(discord_user_id), ciphertext),
            )

    def get_tokens(self, discord_user_id: int, *, youtube: bool = False) -> dict | None:
        table = "youtube_accounts" if youtube else "accounts"
        row = self.db.execute(
            f"SELECT encrypted_tokens FROM {table} WHERE discord_user_id = ?",
            (str(discord_user_id),),
        ).fetchone()
        return json.loads(self.crypto.decrypt(row[0])) if row else None

    def delete_tokens(self, discord_user_id: int, *, youtube: bool = False) -> None:
        table = "youtube_accounts" if youtube else "accounts"
        with self.db:
            self.db.execute(f"DELETE FROM {table} WHERE discord_user_id = ?", (str(discord_user_id),))

    def set_time_zone(self, discord_user_id: int, time_zone: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO user_settings (discord_user_id, time_zone) VALUES (?, ?) "
                "ON CONFLICT(discord_user_id) DO UPDATE SET time_zone = excluded.time_zone",
                (str(discord_user_id), time_zone),
            )

    def get_time_zone(self, discord_user_id: int) -> str | None:
        row = self.db.execute(
            "SELECT time_zone FROM user_settings WHERE discord_user_id = ?",
            (str(discord_user_id),),
        ).fetchone()
        return row[0] if row else None

    def add_scheduled_post(
        self, post_id: str, discord_user_id: int, video_name: str,
        original_name: str, mime_type: str, post_info: dict, scheduled_at: int,
    ) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO scheduled_posts "
                "(id, discord_user_id, video_name, original_name, mime_type, post_info, scheduled_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (post_id, str(discord_user_id), video_name, original_name, mime_type,
                 json.dumps(post_info), scheduled_at, int(time.time())),
            )

    def list_scheduled_posts(self, discord_user_id: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, video_name, original_name, mime_type, post_info, scheduled_at, "
            "state, publish_id, error FROM scheduled_posts WHERE discord_user_id = ? "
            "ORDER BY CASE WHEN state = 'pending' THEN 0 ELSE 1 END, "
            "CASE WHEN state = 'pending' THEN scheduled_at END ASC, created_at DESC LIMIT 30",
            (str(discord_user_id),),
        ).fetchall()
        keys = ("id", "video_name", "original_name", "mime_type", "post_info", "scheduled_at",
                "state", "publish_id", "error")
        return [dict(zip(keys, row)) for row in rows]

    def active_scheduled_count(self, discord_user_id: int) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM scheduled_posts WHERE discord_user_id = ? AND state = 'pending'",
            (str(discord_user_id),),
        ).fetchone()[0]

    def cancel_scheduled_post(self, discord_user_id: int, post_id: str) -> str | None:
        with self.db:
            row = self.db.execute(
                "DELETE FROM scheduled_posts WHERE discord_user_id = ? AND id = ? AND state = 'pending' "
                "RETURNING video_name",
                (str(discord_user_id), post_id),
            ).fetchone()
        return row[0] if row else None

    def cancel_all_scheduled_posts(self, discord_user_id: int) -> list[str]:
        with self.db:
            rows = self.db.execute(
                "DELETE FROM scheduled_posts WHERE discord_user_id = ? AND state = 'pending' "
                "RETURNING video_name",
                (str(discord_user_id),),
            ).fetchall()
        return [row[0] for row in rows]

    def due_scheduled_posts(self, now: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, discord_user_id, video_name, mime_type, post_info, scheduled_at, "
            "state, publish_id FROM scheduled_posts "
            "WHERE (state = 'pending' AND scheduled_at <= ?) OR state = 'submitted' "
            "ORDER BY scheduled_at",
            (now,),
        ).fetchall()
        keys = ("id", "discord_user_id", "video_name", "mime_type", "post_info",
                "scheduled_at", "state", "publish_id")
        return [dict(zip(keys, row)) for row in rows]

    def claim_scheduled_post(self, post_id: str) -> bool:
        with self.db:
            result = self.db.execute(
                "UPDATE scheduled_posts SET state = 'posting' WHERE id = ? AND state = 'pending'",
                (post_id,),
            )
        return result.rowcount == 1

    def update_scheduled_post(self, post_id: str, state: str, *, publish_id: str | None = None,
                              error: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE scheduled_posts SET state = ?, publish_id = COALESCE(?, publish_id), error = ? "
                "WHERE id = ?",
                (state, publish_id, error, post_id),
            )

    def recover_interrupted_posts(self) -> list[tuple[str, int, str]]:
        with self.db:
            rows = self.db.execute(
                "UPDATE scheduled_posts SET state = 'uncertain', "
                "error = 'Bot stopped while sending this video; check TikTok before trying again.' "
                "WHERE state = 'posting' RETURNING id, discord_user_id, video_name"
            ).fetchall()
        return [(post_id, int(user_id), video_name) for post_id, user_id, video_name in rows]

    def close(self) -> None:
        self.db.close()
