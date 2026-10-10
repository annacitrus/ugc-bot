# Discord → TikTok and YouTube video uploader

Mention **@ghostpost** in Discord or DM it with **one MP4, MOV, or WebM attachment**. The bot opens a posting panel for each linked platform. Answer the TikTok and/or YouTube questions, then confirm posting in the corresponding panel. You can post the same file to either platform or both. No video is uploaded until you press a post button (or schedule a TikTok post).

Each Discord user connects their own TikTok account with `/link-tiktok` and YouTube channel with `/link-youtube`. Tokens are encrypted in a local SQLite database; `/unlink` removes TikTok and `/unlink-youtube` removes YouTube.

## Set up

### YouTube Shorts direct uploads

Enable **YouTube Data API v3** in a Google Cloud project, configure the OAuth consent screen (add your Google account as a test user while testing), and create an OAuth client of type **Web application**. Register `https://YOUR-DOMAIN/oauth/youtube/callback` as an authorized redirect URI. Use the same public host as the bot's existing callback server.

Set `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, and `YOUTUBE_REDIRECT_URI` in `.env` or Railway variables and restart the bot. These settings are optional; existing TikTok configuration is still required. YouTube connections and one-time OAuth states are stored separately from TikTok, with tokens encrypted using the existing encryption key.

Run `/link-youtube`, select the intended Google account/channel, and grant upload permission. Then **@mention ghostpost with a video file**, or DM it the video. In the YouTube panel, use **Edit YouTube details** to enter a title and optional description. Choose visibility (`private`, `unlisted`, or `public`) and explicitly answer whether the video is **made for kids**. Press **Post to YouTube** to upload directly. The bot returns a video link after transfer; YouTube processing continues afterward. If TikTok is also linked, its separate panel offers the existing caption, privacy, interaction, commercial disclosure, posting and scheduling choices. Each panel confirms its own platform, so posting to YouTube does not submit or disable TikTok and vice versa. Panels expire after ten minutes and only the video sender can use them. `/unlink-youtube` revokes Google authorization and removes the saved YouTube connection; `/unlink` continues to affect TikTok only. YouTube scheduling is not included.

YouTube uses its standard [video upload API](https://developers.google.com/youtube/v3/docs/videos/insert) for Shorts. [Square or vertical videos up to three minutes](https://support.google.com/youtube/answer/15424877) are classified as Shorts by YouTube; this bot does not crop or shorten videos or guarantee Shorts classification. [Testing-mode refresh tokens expire after seven days](https://developers.google.com/identity/protocols/oauth2#expiration), requiring `/link-youtube` again.

Uploads use Google's [resumable transfer protocol](https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol), share the configured `MAX_VIDEO_MB` limit, and remove temporary videos after transfer. Interrupted transfers are not retried automatically; check your channel before resubmitting to avoid duplicates.

### Discord and TikTok setup

1. Create a Discord application and bot in the [Discord Developer Portal](https://discord.com/developers/applications). Invite it with the `bot` and `applications.commands` scopes, and give it View Channels, Read Message History, and Send Messages permissions. The bot handles messages that directly @mention it; it does not request Discord's Message Content intent.
2. Create a TikTok developer app, add **Login Kit** and **Content Posting API**, and enable/obtain the `video.upload` scope. For direct posts, also enable **Direct Post** and obtain approval for `video.publish`. Register an **HTTPS** Login Kit redirect URI ending in `/oauth/callback`, for example `https://bot.example.com/oauth/callback`. Set up the Cloudflare Tunnel below so TikTok can reach it.
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

6. In Discord, run `/link-tiktok` and authorize both `video.upload` and `video.publish` through the private link. For scheduling, run `/set-time-zone` with your IANA time zone, for example `America/Indianapolis`. Then @mention the bot with one video attachment, or DM the video to it. The bot opens a panel for privacy, caption, interaction and commercial disclosure choices. Press **Post now** to submit immediately, or **Schedule post** to choose a future time. Use `/unlink` to disconnect. Set `DISCORD_GUILD_ID` during development for immediate command registration in one server; global slash commands can take time to appear.

### Scheduled posts

Run `/set-time-zone` with an IANA name such as `America/Indianapolis` to save your scheduling time zone; omit the argument to view the current setting. The direct-post panel displays the selected zone and its current local time and UTC offset. The **Schedule post** dialog shows the zone beside the time field. Enter a local date and time such as `2026-10-12 14:30`; the bot applies the zone's daylight saving rules. If a time occurs twice when clocks fall back, add the intended offset, for example `2026-11-01 01:30 -04:00`. A skipped spring-forward time is rejected. If you have not set a time zone, you can still schedule by entering an explicit UTC offset such as `2026-10-12 14:30 -04:00`.

The time must be at least one minute and no more than 30 days ahead. Changing `/set-time-zone` does not move posts already scheduled. Discord displays the saved time in each viewer's local timezone. Run `/scheduled` to see your recent posts and their status; use `/cancel-post` with the displayed ID before a post starts. The bot sends a DM when TikTok confirms publication or reports a failure. If a transfer is interrupted, the post is marked **uncertain** and is not retried automatically, to avoid duplicates.

Scheduling is handled by the bot: it saves the video alongside `bot.sqlite3` in a `scheduled_videos` directory, then calls TikTok's Direct Post API when the time arrives. Keep the bot running and the database and video directory on persistent storage. If the bot is offline at the chosen time, it submits pending posts after restarting. Up to 20 posts can be pending per Discord user. `/unlink` cancels pending posts and deletes their saved videos.

`MAX_VIDEO_MB` defaults to 100 MB. The bot stores each upload temporarily and deletes it after transfer. Keep enough free disk space for two simultaneous videos.

## Deploy on Railway

Railpack reads [`railpack.json`](railpack.json) and starts the bot with `python -m ugc_bot.bot`. Railway supplies `PORT`; the bot binds to `0.0.0.0` on that port when running there. The `/healthz` endpoint is available for a Railway deployment health check.

1. Connect this repository to a Railway service. In **Settings → Networking**, generate a public Railway domain. In **Settings → Deploy**, set the health check path to `/healthz` if you want Railway to verify startup.
2. In the service's **Variables**, set `DISCORD_BOT_TOKEN`, `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, and `TOKEN_ENCRYPTION_KEY`. Set `TIKTOK_REDIRECT_URI` to `https://YOUR-RAILWAY-DOMAIN/oauth/callback`, using your actual generated domain. Register that exact URI in TikTok Login Kit. Add optional `DISCORD_GUILD_ID` and `MAX_VIDEO_MB` as needed. Railway does not use your local `.env` file.
3. Attach a persistent volume to the service, for example at `/data`. The bot automatically puts `bot.sqlite3` and scheduled videos there when Railway supplies `RAILWAY_VOLUME_MOUNT_PATH`. Keep the encryption key stable across deploys so stored TikTok connections remain readable. Make sure the volume has space for every pending video.
4. Redeploy from the updated repository. Once live, `https://YOUR-RAILWAY-DOMAIN/healthz` should return `ok`. Then run `/link-tiktok` in Discord.

Railway hosts both the Discord process and its HTTPS callback in this setup. You do not need the Cloudflare Tunnel below if you use the Railway domain. Keep the service to one replica while it uses SQLite and a single Discord bot connection.

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

Both checks should print `OK`. The callback path itself returns an error without a valid one-time OAuth state; that is expected. Run `/link-tiktok` in Discord for the real flow.

Cloudflare's [tunnel setup](https://developers.cloudflare.com/tunnel/get-started/) and [token-file option](https://developers.cloudflare.com/tunnel/reference/run-parameters/) describe the dashboard and connector steps. TikTok requires the [registered HTTPS redirect URI](https://developers.tiktok.com/docs/en/login-kit-web) to match the OAuth request.

## TikTok limits

The app requests `video.upload` and `video.publish` during `/link-tiktok` authorization. Posting through the bot uses TikTok's Direct Post API and follows TikTok's per-post privacy and consent requirements. Existing upload-only connections must reauthorize with `/link-tiktok` to post. **Unaudited apps can only post privately (`SELF_ONLY`) and only to private creator accounts**; public posting requires TikTok audit approval. The Direct Post API also has daily posting caps.

Official references: [Upload API](https://developers.tiktok.com/docs/en/content-posting-api-reference-upload-video), [Direct Post API](https://developers.tiktok.com/docs/en/content-posting-api-reference-direct-post), [content sharing guidelines](https://developers.tiktok.com/docs/en/content-sharing-guidelines), [media transfer rules](https://developers.tiktok.com/docs/en/content-posting-api-media-transfer-guide), [Login Kit](https://developers.tiktok.com/docs/en/login-kit-web), [token management](https://developers.tiktok.com/docs/en/oauth-user-access-token-management), [Discord mention access](https://support-dev.discord.com/hc/en-us/articles/6383579033751-Message-Content-Intent-Alternatives-Workarounds).
