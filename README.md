# Discord → TikTok video uploader

Mention the bot in Discord with **one MP4, MOV, or WebM attachment**. It downloads the file, sends it through TikTok's official Content Posting API, and tells you when it reaches your TikTok inbox. **Open the TikTok inbox notification to edit and publish it.** TikTok's `video.upload` API does not publish the post on its own.

Each Discord user connects their own TikTok account with `/link`. Tokens are encrypted in a local SQLite database; `/unlink` removes the connection.

## Set up

1. Create a Discord application and bot in the [Discord Developer Portal](https://discord.com/developers/applications). Invite it with the `bot` and `applications.commands` scopes, and give it View Channels, Read Message History, and Send Messages permissions. The bot handles messages that directly @mention it; it does not request Discord's Message Content intent.
2. Create a TikTok developer app, add **Login Kit** and **Content Posting API**, and enable/obtain the `video.upload` scope. Register an **HTTPS** Login Kit redirect URI ending in `/oauth/callback`, for example `https://bot.example.com/oauth/callback`. TikTok must be able to reach that URL. Put a reverse proxy or tunnel in front of the bot's local HTTP listener; forward `/oauth/callback` to `HTTP_HOST:HTTP_PORT`.
3. Create a Python 3.11+ environment and install dependencies:

   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env`. Fill in the Discord bot token, TikTok client key and secret, and the **exact** registered `TIKTOK_REDIRECT_URI`. Generate `TOKEN_ENCRYPTION_KEY` with:

   ```powershell
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Keep `.env`, the encryption key, and the SQLite database private and backed up together. Changing the key makes existing connections unreadable.
5. Start the bot:

   ```powershell
   python -m ugc_bot.bot
   ```

6. In Discord, run `/link`, authorize TikTok through the private link, then send a message that @mentions the bot and attaches a video. Use `/unlink` to disconnect. Set `DISCORD_GUILD_ID` during development for immediate command registration in one server; global slash commands can take time to appear.

`MAX_VIDEO_MB` defaults to 100 MB. The bot stores each upload temporarily and deletes it after transfer. Keep enough free disk space for two simultaneous videos.

## TikTok limits

TikTok's Upload API sends videos to the creator's inbox for completion in the TikTok app. TikTok limits pending shares (currently up to five in a 24-hour period) and video length (up to ten minutes for this API). The app requires a TikTok developer configuration and permission for `video.upload`; TikTok may reject a developer app or restrict access based on its product guidelines. Automatic **publishing** is a separate `video.publish` Direct Post integration with creator-facing consent and privacy controls, and typically requires TikTok review.

Official references: [Upload API](https://developers.tiktok.com/docs/en/content-posting-api-reference-upload-video), [media transfer rules](https://developers.tiktok.com/docs/en/content-posting-api-media-transfer-guide), [Login Kit](https://developers.tiktok.com/docs/en/login-kit-web), [token management](https://developers.tiktok.com/docs/en/oauth-user-access-token-management), [Discord mention access](https://support-dev.discord.com/hc/en-us/articles/6383579033751-Message-Content-Intent-Alternatives-Workarounds).
