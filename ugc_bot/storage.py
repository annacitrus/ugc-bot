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
            """
        )
        self.crypto = Fernet(encryption_key)

    def new_state(self, discord_user_id: int) -> str:
        state = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute("DELETE FROM oauth_states WHERE expires_at < ?", (int(time.time()),))
            self.db.execute(
                "INSERT INTO oauth_states VALUES (?, ?, ?)",
                (state, str(discord_user_id), int(time.time()) + 600),
            )
        return state

    def consume_state(self, state: str) -> int | None:
        with self.db:
            row = self.db.execute(
                "DELETE FROM oauth_states WHERE state = ? AND expires_at >= ? RETURNING discord_user_id",
                (state, int(time.time())),
            ).fetchone()
        return int(row[0]) if row else None

    def save_tokens(self, discord_user_id: int, tokens: dict) -> None:
        ciphertext = self.crypto.encrypt(json.dumps(tokens).encode("utf-8"))
        with self.db:
            self.db.execute(
                "INSERT INTO accounts VALUES (?, ?) ON CONFLICT(discord_user_id) "
                "DO UPDATE SET encrypted_tokens = excluded.encrypted_tokens",
                (str(discord_user_id), ciphertext),
            )

    def get_tokens(self, discord_user_id: int) -> dict | None:
        row = self.db.execute(
            "SELECT encrypted_tokens FROM accounts WHERE discord_user_id = ?",
            (str(discord_user_id),),
        ).fetchone()
        return json.loads(self.crypto.decrypt(row[0])) if row else None

    def delete_tokens(self, discord_user_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM accounts WHERE discord_user_id = ?", (str(discord_user_id),))

    def close(self) -> None:
        self.db.close()
