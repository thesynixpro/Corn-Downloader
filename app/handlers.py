"""Telegram command/message handlers and application wiring."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Settings
from .jobs import JobManager, run_download_job
from .platforms import canonicalize_url
from .urlguard import URLGuardError, ensure_public_url, extract_first_url

logger = logging.getLogger("uvd_bot.handlers")


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.bot_data["settings"]


def _manager(context: ContextTypes.DEFAULT_TYPE) -> JobManager:
    return context.bot_data["jobs"]


def _welcome(settings: Settings) -> str:
    return (
        "👋 Welcome to Universal Video Downloader Bot!\n\n"
        "Send me a public video URL and I will download it and send the video back here.\n"
        "Direct .m3u8 HLS playlist links are supported too.\n\n"
        "Commands:\n"
        "/start – show this message\n"
        "/help – how to use\n"
        "/about – features and limits\n"
        "/download <url> – start a download\n"
        "/cancel – cancel the active download\n"
        "/status – show your download status\n\n"
        f"Max file size: {settings.effective_max_mb} MB."
    )


def _help() -> str:
    return (
        "❓ How to use\n\n"
        "1. Copy a public video URL (or a direct .m3u8 HLS playlist link).\n"
        "2. Paste it here as a message, or send /download <url>.\n"
        "3. Wait for: checking → extracting → downloading → processing → uploading.\n\n"
        "Notes:\n"
        "• Only publicly accessible videos. Private, login-only, DRM or paywalled "
        "videos are not supported and are never bypassed.\n"
        "• HLS playlists are downloaded by a dedicated engine and remuxed with FFmpeg.\n"
        "• Live HLS streams are saved as a snapshot of the current window.\n"
        "• One active download per user. Use /cancel to stop it."
    )


def _about(settings: Settings) -> str:
    return (
        "Universal Video Downloader Bot\n\n"
        "• yt-dlp for platform pages, a native HLS engine for .m3u8 links, "
        "and direct HTTP streaming for media file URLs.\n"
        "• Picks the best quality within the configured height limit and merges "
        "audio/video with FFmpeg when needed.\n"
        "• SSRF-guarded fetches, bounded retries, per-user task limits, and a "
        "global download queue.\n\n"
        "Limitations:\n"
        f"• Max deliverable size: {settings.effective_max_mb} MB (Telegram's cloud "
        f"API caps bot uploads at ~50 MB; a local Bot API server or MTProto upload "
        f"unlocks up to {settings.max_file_mb} MB).\n"
        "• No DRM, paywall, login, captcha or private-content bypass.\n"
        "• Website support depends on yt-dlp extractors; not every URL will work."
    )


def _url_error_text(exc: URLGuardError) -> str:
    if exc.reason == "unsafe_scheme":
        return "Please send a valid public video URL (http/https only)."
    if exc.reason == "host_not_allowed":
        return "This host is not on the bot's allow-list."
    if exc.reason in ("blocked_host", "blocked_address"):
        return "This URL cannot be used: private, local and metadata addresses are blocked."
    if exc.reason == "dns_error":
        return "This host could not be resolved. Please check the URL."
    return "Please send a valid public video URL."


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_welcome(_settings(context)))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_help())


async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_about(_settings(context)))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    manager = _manager(context)
    user_id = update.effective_user.id
    active = await manager.has_active(user_id)
    status = await manager.get_status(user_id)
    position = manager.queue_position(user_id)
    if active:
        text = f"⏳ Active: {status}"
        if position:
            text += f" (queue position {position})"
    else:
        text = f"ℹ️ Status: {status}"
    await update.message.reply_text(text)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    manager = _manager(context)
    user_id = update.effective_user.id
    if await manager.has_active(user_id):
        await update.message.reply_text("🛑 Cancelling the active download…")
    cancelled = await manager.cancel(user_id)
    if not cancelled:
        await update.message.reply_text("ℹ️ No active download to cancel.")


async def cmd_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args) if context.args else ""
    url = extract_first_url(text)
    if not url:
        await update.message.reply_text("Usage: /download <video URL>")
        return
    await start_job(update, context, url)


async def on_url_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    url = extract_first_url(update.message.text)
    if not url:
        await update.message.reply_text("Please send a valid public video URL.")
        return
    await start_job(update, context, url)


async def on_unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message and update.message.text and not update.message.text.startswith("/"):
        await update.message.reply_text(
            "Please send a valid public video URL. See /help for details."
        )


async def start_job(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    manager = _manager(context)
    settings = _settings(context)
    user_id = update.effective_user.id

    if await manager.has_active(user_id):
        await update.message.reply_text(
            "⏳ You already have an active download. Send /cancel to stop it first."
        )
        return

    canonical = canonicalize_url(url)
    try:
        validated = await ensure_public_url(canonical, settings.allowed_hosts)
    except URLGuardError as exc:
        await update.message.reply_text(_url_error_text(exc))
        return

    status_msg = await update.message.reply_text("🔗 URL received. Checking URL…")
    try:
        tempdir = tempfile.mkdtemp(prefix="uvd_", dir=str(settings.download_dir))
    except OSError as exc:
        logger.error("Failed to create tempdir: %s", type(exc).__name__)
        await status_msg.edit_text(
            "❌ Unable to start a download: temporary storage is unavailable."
        )
        return

    task = asyncio.create_task(
        run_download_job(validated, update, context, status_msg, user_id, tempdir, manager)
    )
    await manager.register(user_id, task, tempdir)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception in handler", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        with contextlib.suppress(Exception):
            await update.effective_message.reply_text(
                "❌ An unexpected error occurred. Please try again."
            )


def build_application(settings: Settings) -> Application:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")
    with contextlib.suppress(OSError):
        settings.download_dir.mkdir(parents=True, exist_ok=True)
    manager = JobManager(settings)
    manager.cleanup_stale()

    builder = Application.builder().token(settings.bot_token).concurrent_updates(True)
    if settings.telegram_api_url:
        base = settings.telegram_api_url.rstrip("/")
        builder = builder.base_url(f"{base}/bot").base_file_url(f"{base}/file/bot")

    async def _shutdown(application: Application) -> None:
        await manager.close()

    application = builder.post_shutdown(_shutdown).build()
    application.bot_data["settings"] = settings
    application.bot_data["jobs"] = manager

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("about", cmd_about))
    application.add_handler(CommandHandler("download", cmd_download))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(r"https?://"), on_url_message)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_unknown_text)
    )
    application.add_error_handler(on_error)
    return application
