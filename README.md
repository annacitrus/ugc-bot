# Discord → TikTok video uploader

Mention the bot in Discord or DM it with **one MP4, MOV, or WebM attachment**. It downloads the file, sends it through TikTok's official Content Posting API, and tells you when it reaches your TikTok inbox. Without an attachment, it replies with an upload instruction. **Open the TikTok inbox notification to edit and publish it.** TikTok's `video.upload` API does not publish the post on its own.

Each Discord user connects their own TikTok account with `/link`. Tokens are encrypted in a local SQLite database; `/unlink` removes the connection.

## Set up

1. Create a Discord application and bot in the [Discord Developer Portal](https://discord.com/developers/applications). Invite it with the `bot` and `applications.commands` scopes, and give it View Channels, Read Message History, and Send Messages permissions. The bot handles messages that directly @mention it; it does not request Discord's Message Content intent.
2. Create a TikTok developer app, add **Login Kit** and **Content Posting API**, and enable/obtain the `video.upload` scope. Register an **HTTPS** Login Kit redirect URI ending in `/oauth/callback`, for example `https://bot.example.com/oauth/callback`. Set up the Cloudflare Tunnel below so TikTok can reach it.
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

## Cloudflare Tunnel on Windows

You need a Cloudflare account with a domain added to it. Pick a dedicated hostname such as `bot.example.com`. A named tunnel keeps this hostname stable for TikTok's registered redirect URI; a temporary Quick Tunnel URL changes and is unsuitable here. The tunnel exposes only the bot's local HTTP callback server. Discord messages and video transfers do not pass through Cloudflare. **The Python Discord bot still runs on your PC or another always-on server; Cloudflare Tunnel is not a bot host.** Cloudflare Workers would require a separate HTTP interactions implementation and a slash command instead of passive `@mention` detection.

1. Install Cloudflare's `cloudflared` executable into this project. The script downloads the official Windows 64-bit release and verifies its published SHA-256 checksum:

   ```powershell
   .\scripts\install-cloudflared.ps1
   ```

2. In the [Cloudflare dashboard](https://dash.cloudflare.com/), open **Networking → Tunnels**, create a remotely managed tunnel (for example, `ugc-bot`), and add a **Published application** route. Set its public hostname to your chosen `bot.example.com` and its service URL to `http://127.0.0.1:8080` (or the matching `HTTP_HOST:HTTP_PORT` from `.env`). Cloudflare creates the DNS route for you. Do not put a Cloudflare Access login policy on this callback hostname.
3. Copy the tunnel token from the dashboard's connector command into a file named `cloudflare-tunnel.token` in this project root. Put **only the token** in the file, without `cloudflared`, `--token`, quotes, or a command prefix. The file is ignored by Git. Anyone holding this token can run a connector for your tunnel, so keep it private.
4. Set `TIKTOK_REDIRECT_URI=https://bot.example.com/oauth/callback` in `.env`, replacing the hostname with yours. Register that **exact same URI** in TikTok Login Kit. Keep `HTTP_HOST=127.0.0.1` and `HTTP_PORT=8080` unless you intentionally change the local listener and Cloudflare service URL together.
5. Keep two PowerShell windows open while using the bot:

   ```powershell
   # Window 1: from the project root
   .\.venv\Scripts\python.exe -m ugc_bot.bot
   ```

   ```powershell
   # Window 2: from the project root
   .\scripts\start-cloudflare.ps1
   ```

6. Verify both local and public routes:

   ```powershell
   .\scripts\check-cloudflare.ps1
   ```

Both checks should print `OK`. The callback path itself returns an error without a valid one-time OAuth state; that is expected. Run `/link` in Discord for the real flow.

Cloudflare's [tunnel setup](https://developers.cloudflare.com/tunnel/get-started/) and [token-file option](https://developers.cloudflare.com/tunnel/reference/run-parameters/) describe the dashboard and connector steps. TikTok requires the [registered HTTPS redirect URI](https://developers.tiktok.com/docs/en/login-kit-web) to match the OAuth request.

## TikTok limits

TikTok's Upload API sends videos to the creator's inbox for completion in the TikTok app. TikTok limits pending shares (currently up to five in a 24-hour period) and video length (up to ten minutes for this API). The app requires a TikTok developer configuration and permission for `video.upload`; TikTok may reject a developer app or restrict access based on its product guidelines. Automatic **publishing** is a separate `video.publish` Direct Post integration with creator-facing consent and privacy controls, and typically requires TikTok review.

Official references: [Upload API](https://developers.tiktok.com/docs/en/content-posting-api-reference-upload-video), [media transfer rules](https://developers.tiktok.com/docs/en/content-posting-api-media-transfer-guide), [Login Kit](https://developers.tiktok.com/docs/en/login-kit-web), [token management](https://developers.tiktok.com/docs/en/oauth-user-access-token-management), [Discord mention access](https://support-dev.discord.com/hc/en-us/articles/6383579033751-Message-Content-Intent-Alternatives-Workarounds).
