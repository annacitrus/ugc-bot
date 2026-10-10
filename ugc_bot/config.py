from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    discord_token: str
    tiktok_client_key: str
    tiktok_client_secret: str
    tiktok_redirect_uri: str
    encryption_key: bytes
    database_path: Path
    http_host: str
    http_port: int
    discord_guild_id: int | None
    max_video_bytes: int
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_redirect_uri: str = ""


def load_config() -> Config:
    load_dotenv()
    required = (
        "DISCORD_BOT_TOKEN",
        "TIKTOK_CLIENT_KEY",
        "TIKTOK_CLIENT_SECRET",
        "TIKTOK_REDIRECT_URI",
        "TOKEN_ENCRYPTION_KEY",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise ValueError(f"Missing environment variables: {', '.join(missing)}")

    redirect_uri = os.environ["TIKTOK_REDIRECT_URI"]
    parsed = urlsplit(redirect_uri)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path != "/oauth/callback" or parsed.query or parsed.fragment:
        raise ValueError("TIKTOK_REDIRECT_URI must be a public HTTPS URL ending in /oauth/callback")
    key = os.environ["TOKEN_ENCRYPTION_KEY"].encode("ascii")
    Fernet(key)  # Fail at startup if the key is malformed.
    max_mb = int(os.getenv("MAX_VIDEO_MB", "100"))
    if not 1 <= max_mb <= 4096:
        raise ValueError("MAX_VIDEO_MB must be between 1 and 4096")

    guild_id = os.getenv("DISCORD_GUILD_ID")
    youtube = [os.getenv(name, "") for name in (
        "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REDIRECT_URI")]
    if any(youtube):
        if not all(youtube):
            raise ValueError("Set all three YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET and YOUTUBE_REDIRECT_URI variables")
        parsed_youtube = urlsplit(youtube[2])
        if (parsed_youtube.scheme != "https" or not parsed_youtube.netloc
                or parsed_youtube.path != "/oauth/youtube/callback"
                or parsed_youtube.query or parsed_youtube.fragment):
            raise ValueError("YOUTUBE_REDIRECT_URI must be HTTPS and end in /oauth/youtube/callback")
    railway = bool(os.getenv("RAILWAY_ENVIRONMENT_ID"))
    default_database = (
        str(Path(os.environ["RAILWAY_VOLUME_MOUNT_PATH"]) / "bot.sqlite3")
        if os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
        else "bot.sqlite3"
    )
    return Config(
        discord_token=os.environ["DISCORD_BOT_TOKEN"],
        tiktok_client_key=os.environ["TIKTOK_CLIENT_KEY"],
        tiktok_client_secret=os.environ["TIKTOK_CLIENT_SECRET"],
        tiktok_redirect_uri=redirect_uri,
        encryption_key=key,
        database_path=Path(os.getenv("DATABASE_PATH", default_database)),
        http_host="0.0.0.0" if railway else os.getenv("HTTP_HOST", "127.0.0.1"),
        http_port=int(os.getenv("PORT") or os.getenv("HTTP_PORT", "8080")),
        discord_guild_id=int(guild_id) if guild_id else None,
        max_video_bytes=max_mb * 1_000_000,
        youtube_client_id=youtube[0],
        youtube_client_secret=youtube[1],
        youtube_redirect_uri=youtube[2],
    )
