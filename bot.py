"""Universal Video Downloader Bot — entry point.

Configuration comes exclusively from environment variables (see .env.example
and README.md). Run with:

    python bot.py
"""

from __future__ import annotations

import logging

from telegram import Update

from app.config import load_settings
from app.handlers import build_application
from app.logging_setup import setup_logging

logger = logging.getLogger("uvd_bot")


def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level, secrets=(settings.bot_token, settings.api_hash))

    if not settings.bot_token:
        logger.error("BOT_TOKEN is missing. Set it as an environment variable or in .env.")
        raise SystemExit(1)
    logger.info("BOT_TOKEN is configured (value hidden).")

    try:
        import yt_dlp

        logger.info("yt-dlp version: %s", yt_dlp.version.__version__)
    except Exception:
        pass

    logger.info(
        "Starting bot | max_file=%d MB | effective_max=%d MB | download_dir=%s | "
        "max_concurrent=%d | max_height=%d | allowed_hosts=%s",
        settings.max_file_mb,
        settings.effective_max_mb,
        settings.download_dir,
        settings.max_concurrent_downloads,
        settings.max_height,
        ",".join(sorted(settings.allowed_hosts)) or "(any public host)",
    )
    if settings.telegram_api_url:
        logger.info("Custom Bot API server configured (value hidden).")
    if settings.mtproto_enabled:
        logger.info("MTProto direct upload enabled.")
    elif settings.max_file_mb > 50 and not settings.telegram_api_url:
        logger.warning(
            "MAX_FILE_MB=%d MB exceeds the cloud Bot API ~50 MB bot-upload limit. "
            "Files above ~50 MB will be split, or need TELEGRAM_API_URL / API_ID+API_HASH.",
            settings.max_file_mb,
        )

    application = build_application(settings)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
