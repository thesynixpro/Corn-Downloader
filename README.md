# Corn Downloader

Telegram bot that downloads publicly accessible videos — platform pages via
`yt-dlp`, direct `.m3u8` HLS playlists via a native HLS engine, direct media
links via streaming HTTP — and sends the result back to the chat. Runs as a
persistent polling worker (no web server needed).

> **Scope:** only publicly accessible media, or media the operator is
> authorized to fetch. The bot never bypasses logins, paywalls, CAPTCHAs, DRM,
> geo-blocks or other access controls. If authentication is required, it says
> so and stops.

## Features

- Send a video URL → get a video info card → pick a quality → receive the video back in Telegram.
- Premium chat UI: branded welcome screen, inline-keyboard menus, info cards,
  live progress bars with speed/ETA, completion screens, and friendly error screens.
- Commands: `/start`, `/help`, `/about`, `/settings`, `/profile`,
  `/download <url>`, `/cancel`, `/status`.
- Three download engines with automatic fallback:
  - **Native HLS engine** for `.m3u8` URLs: master/media playlist detection,
    variant selection, separate audio renditions, AES-128 support via FFmpeg,
    byte-range segments, bounded per-segment retries and concurrency.
  - **Direct HTTP streaming** for `.mp4` / `.webm` / `.mkv` / `.mov` links.
  - **yt-dlp** for platform pages and as universal fallback.
- Best-quality format selection inside `MAX_FILE_MB` and `MAX_VIDEO_HEIGHT`,
  MP4-preferred, merged/remuxed with FFmpeg.
- Progress updates: percent, block progress bar, size, speed, ETA, segment counts.
- Quality selection offers only genuinely available options (parsed variants or
  known-size combined formats); explicit choices are re-validated at download time.
- Per-user settings (default quality, upload style, progress verbosity) and
  accurate per-user statistics, persisted in SQLite.
- Per-user single job + global download queue with queue position reporting.
- Temp dirs cleaned on success, failure, and cancel; stale dirs removed at boot.
- >50 MB delivery: local Bot API server, MTProto direct upload (up to ~2 GB),
  or automatic splitting into parts.
- Security: SSRF guard (private/loopback/link-local/metadata ranges, DNS
  resolution, host allow-list), per-redirect validation, timeouts, size and
  disk budgets, sanitized filenames, token redaction in logs.
- Unsupported URLs, expired playlists, auth-walled pages, DRM streams and
  network failures map to clear user-facing messages.

## Project structure

```
.
├── bot.py                      # entry point (polling, graceful shutdown)
├── app/
│   ├── config.py               # environment settings, limits, derived caps
│   ├── logging_setup.py        # logging + secret redaction
│   ├── formatting.py           # human-readable sizes/durations/progress
│   ├── progress.py             # progress state + rate meter
│   ├── ui.py                   # premium message templates (stdlib-only, tested)
│   ├── tgui.py                 # Card → python-telegram-bot markup bridge
│   ├── callbacks.py            # compact safe callback-data codec (≤64 bytes)
│   ├── selections.py           # quality selections + height-cap logic
│   ├── store.py                # SQLite user preferences + statistics
│   ├── urlguard.py             # URL validation + SSRF protection
│   ├── platforms.py            # host → platform detection, host normalization
│   ├── hls.py                  # pure m3u8 parser + segment downloader
│   ├── hls_download.py         # HLS engine (segments, AES-128 via FFmpeg)
│   ├── format_select.py        # pure yt-dlp format selection + quality options
│   ├── downloader.py           # engine router + yt-dlp + direct HTTP
│   ├── media.py                # ffprobe validation, remux/mux, splitting
│   ├── net.py                  # httpx client with per-hop SSRF validation
│   ├── uploader.py             # Bot API / MTProto / split delivery
│   ├── jobs.py                 # job manager + download pipeline
│   └── handlers.py             # commands/messages/callbacks + wiring
├── tests/                      # unit tests (stdlib unittest, pytest-compatible)
├── requirements.txt
├── requirements-dev.txt
├── .env.example
└── Dockerfile
```

## Requirements

- Python **3.11+** (3.12 recommended)
- **FFmpeg** (and ffprobe) on `PATH`
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Installation

### 1. Get the code and install dependencies

```bash
git clone <your-repo-url> universal-video-downloader-bot
cd universal-video-downloader-bot

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Install FFmpeg

| Platform | Command |
|---|---|
| Debian / Ubuntu | `sudo apt update && sudo apt install -y ffmpeg` |
| Fedora / RHEL | `sudo dnf install -y ffmpeg` |
| Arch | `sudo pacman -S ffmpeg` |
| macOS | `brew install ffmpeg` |
| Windows | `winget install Gyan.FFmpeg` (or scoop/choco), reopen the terminal |
| Docker | already included in the provided `Dockerfile` |

Verify:

```bash
ffmpeg -version | head -1
ffprobe -version | head -1
```

### 3. Configure

```bash
cp .env.example .env
# edit .env and set at minimum BOT_TOKEN
```

`python-dotenv` loads `.env` automatically. In production, environment
variables take the same names and **override** nothing from `.env` if already
set in the environment.

### 4. Run

```bash
python bot.py
```

Open Telegram, message the bot, send `/start`, then paste a video URL.

## Configuration reference

| Variable | Required | Default | Description |
|---|---|---|---|
| `BOT_TOKEN` | yes | — | Bot token from @BotFather |
| `MAX_FILE_MB` | no | `700` | Max accepted size (1–2000). Cloud Bot API still caps bot uploads at ~50 MB unless `TELEGRAM_API_URL` or MTProto is configured |
| `DOWNLOAD_DIR` | no | `/tmp/video_downloads` | Temp download root |
| `MAX_CONCURRENT_DOWNLOADS` | no | `3` | Global simultaneous downloads (queue beyond this) |
| `REQUEST_TIMEOUT` | no | `120` | Per-request/read timeout (s); uploads always get ≥600 s |
| `CONNECT_TIMEOUT` | no | `30` | TCP connect timeout (s) |
| `DOWNLOAD_TIMEOUT` | no | `3600` | Overall per-download watchdog (s), `0` disables |
| `TELEGRAM_API_URL` | no | — | Self-hosted Bot API base URL, e.g. `http://localhost:8081` |
| `SPLIT_PART_MB` | no | `45` | Part size for split delivery (20–49) |
| `SPLIT_MAX_PARTS` | no | `15` | Max parts for split delivery (2–25) |
| `API_ID` / `API_HASH` | no | — | From [my.telegram.org](https://my.telegram.org); enables MTProto direct upload (up to ~2 GB, one video) |
| `MAX_VIDEO_HEIGHT` | no | `2160` | Height cap for selection (`0` = no cap) |
| `ALLOWED_HOSTS` | no | empty | Comma-separated host allow-list (matches subdomains). Empty = any public host |
| `MAX_TEMP_DISK_MB` | no | `5000` | Temp-storage budget; jobs refuse to start if it would be exceeded |
| `HLS_SEGMENT_CONCURRENCY` | no | `4` | Parallel HLS segment downloads (1–16) |
| `HLS_REFERER` | no | empty | Optional Referer header for direct HLS/media links |
| `HLS_USER_AGENT` | no | Chrome UA | User-Agent for the native engines |
| `DATA_DIR` | no | `<DOWNLOAD_DIR>/bot_data` | Directory for the SQLite prefs/stats database |
| `SUPPORT_CHAT_ID` | no | empty | Enables Feedback buttons; user feedback is forwarded here |
| `LOG_LEVEL` | no | `INFO` | DEBUG / INFO / WARNING / ERROR / CRITICAL |

### Large files (>50 MB)

Telegram's **cloud** Bot API accepts roughly 50 MB per bot upload. Options:

1. **MTProto** (`API_ID` + `API_HASH`): the bot logs in as itself and uploads
   single videos up to ~2 GB with progress. Requires credentials from
   [my.telegram.org](https://my.telegram.org).
2. **Self-hosted Bot API server** (`TELEGRAM_API_URL`): run
   [telegram-bot-api](https://github.com/tdlib/telegram-bot-api) with your API
   credentials and point the bot at it. Unlocks the full `MAX_FILE_MB`.
3. **Split delivery** (default fallback): files above ~50 MB are cut into
   `SPLIT_PART_MB` parts with FFmpeg and sent as consecutive videos.

## Usage examples

```
/start                     → branded welcome menu (Download / Help / Settings / Profile)
/help                      → 5-step guide + tips
/about                     → engines, limits, and policies
/settings                  → default quality, upload style, progress verbosity
/profile                   → your completed downloads + data delivered
/download https://...      → inspect the link, then pick a quality
https://youtu.be/XXXXXXXX  → info card with genuinely available qualities
https://cdn.site/vod/master.m3u8   → native HLS engine (parsed variant options)
/cancel                    → cancel your active job (temp files removed)
/status                    → premium status card with cancel button
```

Interaction flow: send a link → info card with quality buttons → download
starts on selection → the same message shows live progress (bar, speed, ETA)
→ completion screen with Download Another / Main Menu buttons.

## Error handling

| Situation | Behaviour |
|---|---|
| Invalid URL / non-http scheme | "Please send a valid public video URL (http/https only)." |
| Private/loopback/metadata host | "This URL cannot be used: private, local and metadata addresses are blocked." |
| Host not in `ALLOWED_HOSTS` | "This host is not on the bot's allow-list." |
| Unsupported site | "This website is not currently supported." |
| Login / paywall / age / bot-check | Explains the bot cannot and will not bypass access controls, and stops |
| DRM (Widevine/FairPlay/SAMPLE-AES) | "This stream is DRM-protected…" |
| Expired playlist / signature (HTTP 410) | "The stream playlist has expired… request a fresh link." |
| HTTP 403 / datacenter block | Suggests retrying later or another link |
| Timeouts | "The download timed out. Please try again later." |
| Too large | Rejected before download when size is known; hard-enforced after |
| Telegram upload limit | MTProto → split → clear message explaining the limit |

Users never see stack traces; each failure includes a short technical detail
(HTTP status or last extractor line) for diagnosis.

## Tests

```bash
python -m unittest discover -s tests -t . -v   # stdlib only, no server needed
# or
pip install -r requirements-dev.txt && pytest tests -v
```

Coverage: URL/SSRF validation, filename sanitization, host allow-list,
platform detection, m3u8 parsing (master/media/keys/byte-ranges/DRM/live),
variant selection, segment downloading with retries/progress, format
selection, quality-option building, callback codec, UI templates, SQLite
preferences/statistics, error mapping, progress/rate metering.

## Deployment (Linux)

### systemd unit

```ini
# /etc/systemd/system/uvd-bot.service
[Unit]
Description=Corn Downloader
After=network-online.target

[Service]
WorkingDirectory=/opt/uvd-bot
EnvironmentFile=/opt/uvd-bot/.env
ExecStart=/opt/uvd-bot/.venv/bin/python bot.py
Restart=on-failure
RestartSec=5
User=uvd
# basic hardening
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo useradd -r -s /usr/sbin/nologin uvd
sudo mkdir -p /opt/uvd-bot /tmp/video_downloads
sudo chown -R uvd /opt/uvd-bot /tmp/video_downloads
# copy the project to /opt/uvd-bot, create .venv, set .env
sudo systemctl daemon-reload
sudo systemctl enable --now uvd-bot
journalctl -u uvd-bot -f
```

### Docker

```bash
docker build -t uvd-bot .
docker run -d --name uvd-bot \
  --restart unless-stopped \
  -e BOT_TOKEN=123:abc \
  -e MAX_FILE_MB=700 \
  --tmpfs /tmp:size=4g \
  uvd-bot
```

The image runs as a non-root user and includes FFmpeg.

### Railway / other PaaS

Deploy the repo (Dockerfile is detected automatically), add `BOT_TOKEN` plus
any optional variables in the service's Variables tab, and deploy. This is a
polling worker: do not add a web server or health-check port.

## Security notes

- All bot-originated HTTP fetches are SSRF-guarded per hop (scheme, userinfo,
  blocked hostnames, DNS resolution, private/reserved/metadata ranges,
  optional allow-list). yt-dlp/FFmpeg connect independently, so only the
  submitted host is validated for those engines — a residual DNS-rebinding
  window documented here for completeness.
- Secrets (`BOT_TOKEN`, `API_HASH`) are registered with a log redaction filter;
  third-party HTTP loggers are silenced.
- Filenames derived from titles are sanitized; temp files live under
  `DOWNLOAD_DIR` and are only deleted from safe roots.
- No cookies, sessions or credentials are accepted or stored.

## Limitations

- DRM-protected services (Netflix, Disney+, Prime Video, …) are never supported.
- Login-walled, age-gated, geo-blocked, private or paywalled content is refused.
- YouTube frequently challenges datacenter IPs ("Sign in to confirm you're not
  a bot"); such failures are reported as access-control limitations.
- Live HLS streams are downloaded as a single snapshot of the current window.
- Site support depends on yt-dlp extractors and changes over time; updating
  `yt-dlp` (redeploy) is the usual fix.
- `asyncio` cannot kill worker threads: an overall download timeout abandons a
  stuck yt-dlp thread, which then exits on its own socket timeout.
