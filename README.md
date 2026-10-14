# Universal Video Downloader Bot

Telegram bot that downloads public videos (via `yt-dlp`) and sends them back in chat. Runs as a persistent Railway worker. No web server required.

## Features

- Send a public video URL → get the video back in Telegram
- Commands: `/start` `/help` `/about` `/cancel` `/status`
- Automatic MP4-preferring format selection within the size limit
- FFmpeg merging of separate audio/video streams
- Per-user limit (one active download), configurable global concurrency cap
- Per-download temp directories + cleanup on success, failure, and cancel
- Size pre-check (when metadata provides it) + strict post-download enforcement
- Friendly error messages, no stack traces to users, token never logged

Supports publicly accessible videos from sites supported by `yt-dlp`, which includes many video-sharing and supported adult platforms. Support depends on the actual URL and extractor — not every URL will work. No DRM / paywall / login bypass.

## Project files

| File | Purpose |
|---|---|
| `bot.py` | Bot source |
| `requirements.txt` | `python-telegram-bot`, `yt-dlp` |
| `Dockerfile` | Python 3.12 + FFmpeg image, runs `python bot.py` |
| `.env.example` | Example env vars (no secrets) |
| `.gitignore` | Ignores `.env`, caches, media |

## Configuration (environment variables)

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOT_TOKEN` | yes | — | Telegram bot token from [@BotFather](https://t.me/BotFather) |
| `MAX_FILE_MB` | no | `700` | Max video size accepted (1–2000). Changing it does **not** override Telegram Bot API upload limits (default Bot API ≈ 50 MB for bots; larger files need a self-hosted Bot API server via `TELEGRAM_API_URL`). |
| `DOWNLOAD_DIR` | no | `/tmp/video_downloads` | Base temp directory |
| `MAX_CONCURRENT_DOWNLOADS` | no | `3` | Global concurrent download cap |
| `REQUEST_TIMEOUT` | no | `120` | Network/extractor timeout (seconds). Uploads always allow at least 600 s. |
| `TELEGRAM_API_URL` | no | — | Base URL of a self-hosted/local Bot API server, e.g. `http://localhost:8081`. Required for uploads above ~50 MB. |
| `LOG_LEVEL` | no | `INFO` | Log verbosity: `DEBUG` / `INFO` / `WARNING` / `ERROR`. |

Copy-paste block for Railway → Service → Variables (only `BOT_TOKEN` is required):

```env
BOT_TOKEN=
MAX_FILE_MB=700
DOWNLOAD_DIR=/tmp/video_downloads
MAX_CONCURRENT_DOWNLOADS=3
REQUEST_TIMEOUT=120
TELEGRAM_API_URL=
LOG_LEVEL=INFO
```

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# FFmpeg must be installed: sudo apt install ffmpeg  (or brew install ffmpeg)
export BOT_TOKEN="your-token-here"
export MAX_FILE_MB=700
python bot.py
```

Check syntax quickly:

```bash
python -m py_compile bot.py
```

## Deploy on Railway (beginner steps)

1. **Create the bot token:** open Telegram → [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
2. **Push this project to GitHub:** create a repo, upload all files (`bot.py`, `requirements.txt`, `Dockerfile`, `README.md`, `.env.example`, `.gitignore`). Do **not** commit real tokens.
3. **Create a Railway project:** [railway.app](https://railway.app) → New Project → Deploy from GitHub repo → select your repo.
4. **Set variables:** open the service → **Variables** tab → add:
   - `BOT_TOKEN` = your token
   - `MAX_FILE_MB` = `700` (optional)
   - `DOWNLOAD_DIR` = `/tmp/video_downloads` (optional)
   - `TELEGRAM_API_URL` = your local Bot API base URL (optional, required for uploads above ~50 MB)
5. **Confirm the builder:** Railway auto-detects the `Dockerfile`. No start-command override needed — the image runs `python bot.py`.
6. **Deploy & check logs:** Deploy → **Logs** should show `Starting bot | max_file=...`. Send `/start` to your bot in Telegram.
7. **No domains/ports needed.** This is a polling worker; do not add a web server or health-check endpoint.

## Usage

1. Send `/start` to the bot.
2. Paste a public video URL.
3. Watch progress: checking → extracting → downloading → uploading → completed.
4. Use `/cancel` to stop, `/status` to inspect your state.

## Error messages

| Situation | Bot reply |
|---|---|
| Invalid URL | "Please send a valid public video URL." |
| Unsupported site | "This website is not currently supported." |
| Private/login/geo/removed | "This video is unavailable or requires access that this bot cannot provide." |
| Too large | "The video exceeds the configured file-size limit …" |
| Download fail | "Unable to download this video. Please check the URL and try again." |
| Upload fail | "The download completed, but Telegram could not upload the file." |

## Notes / limits

- Telegram Bot API bots can typically upload ~50 MB via the default cloud API (up to ~2 GB only with a self-hosted Bot API server + `TELEGRAM_API_URL`). `MAX_FILE_MB=700` therefore needs a local Bot API server for the >50 MB uploads to succeed; otherwise Telegram will reject them even though the bot downloads them.
- Railway disks are ephemeral and small-service RAM is limited; 700 MB downloads raise disk/timeout pressure. Keep `MAX_CONCURRENT_DOWNLOADS` low (default `3`) and expect slower uploads for very large files.
- yt-dlp extractor breakage happens when sites change; updating `yt-dlp` (redeploy) usually fixes it.
- Never send cookies, credentials, or `.env` contents to the bot or logs.
