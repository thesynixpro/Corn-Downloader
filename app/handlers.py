"""Telegram command/message/callback handlers and application wiring."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
import time

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import downloader, errors, hls_extract, platforms, tgui, ui
from .callbacks import CallbackError, decode
from .config import Settings
from .format_select import build_quality_options
from .formatting import format_bytes, format_duration
from .hls import DRMProtectedError, PlaylistError
from .hls_download import describe_variant_choices, fetch_master_playlist
from .hls_extract import ExtractionError
from .jobs import (
    JobManager,
    PendingOption,
    failure_card,
    run_download_job,
)
from .net import NetworkError
from .platforms import (
    PLATFORMS,
    canonicalize_url,
    detect_platform,
    is_probable_direct_media,
    is_probable_hls,
)
from .selections import QualitySelection, effective_height_cap
from .urlguard import URLGuardError, ensure_public_url, extract_first_url

logger = logging.getLogger("uvd_bot.handlers")


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.bot_data["settings"]


def _manager(context: ContextTypes.DEFAULT_TYPE) -> JobManager:
    return context.bot_data["jobs"]


def _display_name(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return "there"
    return user.first_name or user.username or "there"


def _support_enabled(settings: Settings) -> bool:
    return settings.support_chat_id is not None


def _url_guard_card(exc: URLGuardError, settings: Settings) -> ui.Card:
    if exc.reason == "unsafe_scheme":
        return ui.error_card(
            kind="invalid",
            message="Please send a valid public video URL (http/https only).",
            include_platforms=True,
            support_enabled=_support_enabled(settings),
        )
    if exc.reason == "host_not_allowed":
        return ui.error_card(
            kind="blocked",
            message="This host is not on the bot's allow-list.",
            support_enabled=_support_enabled(settings),
        )
    if exc.reason in ("blocked_host", "blocked_address"):
        return ui.error_card(
            kind="blocked",
            message="This URL cannot be used: private, local, and metadata addresses are blocked.",
            support_enabled=_support_enabled(settings),
        )
    if exc.reason == "dns_error":
        return ui.error_card(
            kind="network",
            message="This host could not be resolved. Please check the URL.",
            retry=True,
            support_enabled=_support_enabled(settings),
        )
    return ui.error_card(
        kind="invalid",
        message="Please send a valid public video URL.",
        include_platforms=True,
        support_enabled=_support_enabled(settings),
    )


def _duration_text(duration: float) -> str:
    if duration and duration > 0:
        return format_duration(duration)
    return "unknown"


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    await tgui.reply_card(
        update.message, ui.welcome_card(_display_name(update), settings.effective_max_mb)
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await tgui.reply_card(update.message, ui.help_card())


async def cmd_about(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = _settings(context)
    await tgui.reply_card(
        update.message, ui.about_card(settings.effective_max_mb, settings.max_file_mb)
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    manager = _manager(context)
    prefs = await manager.get_prefs(update.effective_user.id)
    await tgui.reply_card(update.message, ui.settings_card(prefs))


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    manager = _manager(context)
    user_id = update.effective_user.id
    stats = await manager.get_stats(user_id)
    active = await manager.has_active(user_id)
    status = await manager.get_status(user_id)
    await tgui.reply_card(
        update.message,
        ui.profile_card(user_id, stats, status if active else "idle", _last_text(stats)),
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    manager = _manager(context)
    user_id = update.effective_user.id
    active = await manager.has_active(user_id)
    status = await manager.get_status(user_id)
    await tgui.reply_card(
        update.message, ui.status_screen(active, status, manager.queue_position(user_id))
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    manager = _manager(context)
    user_id = update.effective_user.id
    manager.clear_pending_for_user(user_id)
    manager.clear_feedback_pending(user_id)
    cancelled_any = False
    if await manager.cancel_extraction(user_id):
        cancelled_any = True
    if await manager.has_active(user_id):
        await update.message.reply_text("🛑 Cancelling the active download…")
    cancelled = await manager.cancel(user_id)
    if cancelled or cancelled_any:
        return
    await tgui.reply_card(update.message, ui.no_active_card())


async def cmd_download(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args) if context.args else ""
    url = extract_first_url(text)
    if not url:
        await tgui.reply_card(update.message, ui.download_prompt_card())
        return
    await handle_url_submission(update, context, url)


async def cmd_hls(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args) if context.args else ""
    url = extract_first_url(text)
    if not url:
        await tgui.reply_card(update.message, ui.hls_prompt_card())
        return
    await start_hls_extraction(update, context, url)


def _hls_guard_card(exc: URLGuardError, settings: Settings) -> ui.Card:
    if exc.reason == "unsafe_scheme":
        return ui.hls_error_card(
            "invalid", "Please provide a valid public HTTP or HTTPS URL."
        )
    if exc.reason == "host_not_allowed":
        return ui.hls_error_card("blocked", "This host is not on the bot's allow-list.")
    if exc.reason in ("blocked_host", "blocked_address"):
        return ui.hls_error_card(
            "blocked",
            "This URL cannot be used: private, local, and metadata addresses are blocked.",
        )
    if exc.reason == "dns_error":
        return ui.hls_error_card(
            "network", "This host could not be resolved. Please check the URL."
        )
    return ui.hls_error_card("invalid", "Please provide a valid public HTTP or HTTPS URL.")


async def _deliver_hls_card(update: Update, edit_message, card: ui.Card) -> None:
    if edit_message is not None:
        await tgui.edit_card(edit_message, card)
    elif update.message:
        await tgui.reply_card(update.message, card)


async def start_hls_extraction(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    *,
    edit_message=None,
) -> None:
    manager = _manager(context)
    settings = _settings(context)
    user_id = update.effective_user.id
    if not settings.hls_extract_enabled:
        await _deliver_hls_card(
            update,
            edit_message,
            ui.hls_error_card("disabled", "HLS extraction is currently disabled on this bot."),
        )
        return
    if await manager.has_active_extraction(user_id):
        await _deliver_hls_card(
            update,
            edit_message,
            ui.hls_error_card(
                "busy",
                "You already have an extraction running. Wait for it to finish or send /cancel.",
            ),
        )
        return
    canonical = canonicalize_url(url)
    try:
        validated = await ensure_public_url(canonical, settings.allowed_hosts)
    except URLGuardError as exc:
        await _deliver_hls_card(update, edit_message, _hls_guard_card(exc, settings))
        return
    if edit_message is not None:
        prompt = edit_message
        await tgui.edit_card(prompt, ui.hls_progress_card("scanning"))
    else:
        prompt = await tgui.reply_card(update.message, ui.hls_progress_card("scanning"))
    if not await manager.try_reserve_extraction(user_id):
        await tgui.edit_card(
            prompt,
            ui.hls_error_card(
                "busy",
                "You already have an extraction running. Wait for it to finish or send /cancel.",
            ),
        )
        return
    task = asyncio.create_task(
        _run_hls_extraction(validated, prompt, context, user_id),
        name=f"hls_extract_{user_id}",
    )
    await manager.register_extraction(user_id, task)


async def _run_hls_extraction(
    url: str, prompt_msg, context: ContextTypes.DEFAULT_TYPE, user_id: int
) -> None:
    manager = _manager(context)
    settings = _settings(context)
    slack = 45.0 if settings.hls_browser_enabled else 10.0
    try:
        async def phase(name: str) -> None:
            await tgui.edit_card(prompt_msg, ui.hls_progress_card(name))

        async with manager.extract_semaphore:
            outcome = await asyncio.wait_for(
                hls_extract.run_extraction(url, settings, phase_cb=phase),
                timeout=float(settings.hls_extract_timeout) + slack,
            )
        if outcome.results:
            session = manager.create_hls_session(
                user_id, url, tuple(outcome.results)
            )
            card = ui.hls_results_card(
                url,
                tuple(outcome.results),
                0,
                session.id,
                browser_note=outcome.browser_note,
            )
            await tgui.edit_card(prompt_msg, card)
        else:
            kind = outcome.error_kind or "no_results"
            message = outcome.error or "No detectable HLS stream was found."
            await tgui.edit_card(
                prompt_msg,
                ui.hls_error_card(kind, message, browser_note=outcome.browser_note),
            )
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await tgui.edit_card(prompt_msg, ui.hls_cancelled_card())
        logger.info("HLS extraction cancelled for user %d", user_id)
        raise
    except asyncio.TimeoutError:
        await tgui.edit_card(
            prompt_msg,
            ui.hls_error_card(
                "timeout", "The extraction timed out. Please try again later."
            ),
        )
    except URLGuardError as exc:
        await tgui.edit_card(prompt_msg, _hls_guard_card(exc, settings))
    except ExtractionError as exc:
        await tgui.edit_card(prompt_msg, ui.hls_error_card(exc.kind, exc.message))
    except Exception:
        logger.error("HLS extraction failed for user %d", user_id, exc_info=True)
        await tgui.edit_card(
            prompt_msg,
            ui.hls_error_card(
                "failed", "Extraction failed due to an unexpected error. Please try again."
            ),
        )
    finally:
        await manager.finish_extraction(user_id)


async def on_url_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    manager = _manager(context)
    user_id = update.effective_user.id
    if manager.is_feedback_pending(user_id) and _settings(context).support_chat_id is not None:
        if not update.message.text.startswith("/") and not extract_first_url(update.message.text):
            await _forward_feedback(update, context)
            return
        manager.clear_feedback_pending(user_id)
    url = extract_first_url(update.message.text)
    if not url:
        await tgui.reply_card(update.message, ui.download_prompt_card())
        return
    await handle_url_submission(update, context, url)


async def on_unknown_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    if update.message.text.startswith("/"):
        return
    manager = _manager(context)
    user_id = update.effective_user.id
    if manager.is_feedback_pending(user_id) and _settings(context).support_chat_id is not None:
        await _forward_feedback(update, context)
        return
    await tgui.reply_card(update.message, ui.download_prompt_card())


async def on_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    await tgui.reply_card(update.message, ui.unknown_command_card(update.message.text))


async def _forward_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    manager = _manager(context)
    settings = _settings(context)
    user_id = update.effective_user.id
    text = (update.message.text or "").strip()
    manager.clear_feedback_pending(user_id)
    if settings.support_chat_id is None or not text:
        await tgui.reply_card(update.message, ui.download_prompt_card())
        return
    try:
        await context.bot.send_message(
            chat_id=settings.support_chat_id,
            text=f"📝 Bot feedback\n👤 User: {user_id}\n\n{text[:1000]}",
        )
    except Exception as exc:
        logger.warning("Forwarding feedback failed: %s", type(exc).__name__)
        await tgui.reply_card(
            update.message,
            ui.error_card(
                kind="network",
                message="Feedback could not be delivered right now. Please try again later.",
                support_enabled=False,
            ),
        )
        return
    await tgui.reply_card(update.message, ui.feedback_sent_card())


async def handle_url_submission(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    *,
    edit_message=None,
) -> None:
    manager = _manager(context)
    settings = _settings(context)
    user_id = update.effective_user.id

    if await manager.has_active(user_id):
        status = await manager.get_status(user_id)
        card = ui.status_screen(True, status, manager.queue_position(user_id))
        if edit_message is not None:
            await tgui.edit_card(edit_message, card)
        else:
            await tgui.reply_card(update.message, card)
        return

    canonical = canonicalize_url(url)
    try:
        validated = await ensure_public_url(canonical, settings.allowed_hosts)
    except URLGuardError as exc:
        card = _url_guard_card(exc, settings)
        if edit_message is not None:
            await tgui.edit_card(edit_message, card)
        else:
            await tgui.reply_card(update.message, card)
        return

    manager.set_last_request(user_id, validated, QualitySelection())
    prefs = await manager.get_prefs(user_id)
    if edit_message is not None:
        await tgui.edit_card(edit_message, ui.analyzing_card())
        prompt = edit_message
    else:
        prompt = await tgui.reply_card(update.message, ui.analyzing_card())

    try:
        payload = await _inspect_for_selection(validated, settings, prefs)
    except Exception as exc:
        logger.info(
            "Link inspection failed for user %d: %s: %s",
            user_id,
            type(exc).__name__,
            str(exc)[:300],
        )
        await tgui.edit_card(
            prompt,
            failure_card(
                exc,
                settings,
                retry_available=True,
                support_enabled=_support_enabled(settings),
            ),
        )
        return

    session = manager.create_pending_session(
        user_id=user_id,
        url=validated,
        kind=payload["kind"],
        title=payload["title"],
        duration=payload["duration"],
        label=payload["label"],
        platform=payload["platform"],
        options=payload["options"],
        live=payload["live"],
    )
    choices = tuple(
        ui.Choice(key=option.key, label=option.label) for option in payload["options"]
    )
    await tgui.edit_card(
        prompt,
        ui.detection_card(
            kind=payload["kind"],
            platform_label=payload["platform_label"],
            title=payload["title"],
            duration_text=_duration_text(payload["duration"]),
            quality_text=payload["quality_text"],
            choices=choices,
            session_id=session.id,
            live=payload["live"],
            note=payload.get("note", ""),
        ),
    )


async def _inspect_for_selection(url: str, settings: Settings, prefs: dict[str, str]) -> dict:
    platform = detect_platform(url)
    cap = effective_height_cap(settings.max_height, prefs.get("quality", "auto"), QualitySelection())
    if is_probable_hls(url):
        try:
            master = await fetch_master_playlist(url, settings, None)
        except DRMProtectedError:
            raise
        except Exception:
            inspection = await downloader.inspect_source(url, settings, None)
            if inspection.kind == "hls" and inspection.hls is not None:
                return {
                    "kind": "hls",
                    "title": inspection.title or "HLS stream",
                    "duration": inspection.duration,
                    "label": inspection.label,
                    "platform": platform.key,
                    "platform_label": platform.label,
                    "quality_text": inspection.label,
                    "options": (PendingOption(key="0", label="⚡ Download stream", target_height=None),),
                    "live": inspection.is_live,
                }
            if inspection.kind == "direct":
                return _direct_payload(url, platform)
            return await _ytdlp_payload(url, settings, prefs, platform, cap)
        choices = describe_variant_choices(master)
        usable = [
            choice
            for choice in choices
            if choice.height and (cap <= 0 or choice.height <= cap)
        ]
        options: list[PendingOption] = [
            PendingOption(key="0", label="⚡ Auto (recommended)", target_height=None)
        ]
        seen: set[int] = set()
        for choice in sorted(usable, key=lambda item: item.height or 0, reverse=True):
            if choice.height in seen:
                continue
            seen.add(choice.height)
            suffix = f" • {choice.bitrate_kbps}k" if choice.bitrate_kbps else ""
            audio = " + audio" if choice.has_audio else ""
            options.append(
                PendingOption(
                    key=str(len(options)),
                    label=f"{choice.height}p{suffix}{audio}",
                    target_height=choice.height,
                )
            )
            if len(options) >= 6:
                break
        quality_text = "Auto (recommended)"
        if len(options) > 1:
            heights = ", ".join(f"{option.target_height}p" for option in options[1:])
            quality_text = f"Auto + {heights}"
        auto_variant = next(
            (choice for choice in sorted(usable, key=lambda item: item.height or 0, reverse=True)),
            None,
        )
        return {
            "kind": "hls",
            "title": "HLS stream",
            "duration": 0.0,
            "label": f"HLS {auto_variant.resolution}" if auto_variant else "HLS stream",
            "platform": platform.key,
            "platform_label": platform.label,
            "quality_text": quality_text,
            "options": tuple(options),
            "live": False,
            "note": "",
        }
    if is_probable_direct_media(url):
        return _direct_payload(url, platform)
    return await _ytdlp_payload(url, settings, prefs, platform, cap)


def _direct_payload(url: str, platform) -> dict:
    from urllib.parse import urlsplit

    name = urlsplit(url).path.rsplit("/", 1)[-1] or "video"
    return {
        "kind": "direct",
        "title": name,
        "duration": 0.0,
        "label": "direct link",
        "platform": platform.key,
        "platform_label": platform.label,
        "quality_text": "Source file",
        "options": (PendingOption(key="0", label="⚡ Download file", target_height=None),),
        "live": False,
        "note": "",
    }


async def _ytdlp_payload(
    url: str, settings: Settings, prefs: dict[str, str], platform, cap: int
) -> dict:
    info = await asyncio.to_thread(downloader.extract_metadata, url, settings)
    title = str(info.get("title") or "video")
    try:
        duration = float(info.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    candidates = build_quality_options(info, settings.effective_max_bytes, cap, limit=5)
    options: list[PendingOption] = [
        PendingOption(key="0", label="⚡ Auto (recommended)", target_height=None)
    ]
    for candidate in candidates:
        size = f" • {format_bytes(candidate.estimated_bytes)}" if candidate.estimated_bytes else ""
        ext = f" • {candidate.ext.upper()}" if candidate.ext else ""
        options.append(
            PendingOption(
                key=str(len(options)),
                label=f"{candidate.target_height}p{ext}{size}",
                target_height=candidate.target_height,
            )
        )
    if len(options) > 1:
        heights = ", ".join(f"{option.target_height}p" for option in options[1:])
        quality_text = f"Auto + {heights}"
    else:
        quality_text = "Auto (best within limits)"
    return {
        "kind": "ytdlp",
        "title": title,
        "duration": duration,
        "label": platform.label,
        "platform": platform.key,
        "platform_label": platform.label,
        "quality_text": quality_text,
        "options": tuple(options),
        "live": False,
        "note": "",
    }


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    try:
        action, parts = decode(query.data or "")
    except CallbackError:
        await tgui.answer_query(query, "This button has expired. Send a link to begin again.")
        return
    try:
        if action == "menu":
            await _on_menu(query, context, parts)
        elif action == "settings_menu":
            await _on_settings_menu(query, context, parts)
        elif action == "settings_set":
            await _on_settings_set(query, context, parts)
        elif action == "start":
            await _on_start_selection(update, context, query, parts)
        elif action == "flow_cancel":
            await _on_flow_cancel(query, context, parts)
        elif action == "flow_retry":
            await _on_flow_retry(update, context, query)
        elif action == "job_cancel":
            await _on_job_cancel(query, context)
        elif action == "hls_view":
            await _on_hls_view(query, context, parts)
        elif action == "hls_page":
            await _on_hls_page(query, context, parts)
        elif action == "hls_again":
            await tgui.answer_query(query)
            await tgui.edit_card(query.message, ui.hls_prompt_card())
        elif action == "hls_run":
            await _on_hls_run(update, context, query, parts)
        else:
            await tgui.answer_query(query, "This button has expired. Send a link to begin again.")
    except Exception:
        logger.error("Callback handling failed", exc_info=True)
        await tgui.answer_query(query, "Something went wrong. Please try again.")


async def _on_menu(query, context: ContextTypes.DEFAULT_TYPE, parts: tuple[str, ...]) -> None:
    manager = _manager(context)
    settings = _settings(context)
    user_id = query.from_user.id if query.from_user else 0
    target = parts[0] if parts else "home"
    await tgui.answer_query(query)
    if target == "home" or target == "start":
        await tgui.edit_card(
            query.message, ui.welcome_card(_display_name_from_query(query), settings.effective_max_mb)
        )
    elif target == "help":
        await tgui.edit_card(query.message, ui.help_card())
    elif target == "about":
        await tgui.edit_card(
            query.message, ui.about_card(settings.effective_max_mb, settings.max_file_mb)
        )
    elif target == "platforms":
        labels = tuple(platform.label for platform in PLATFORMS) + ("HLS stream", "Direct video link")
        await tgui.edit_card(query.message, ui.platforms_card(labels))
    elif target == "settings":
        prefs = await manager.get_prefs(user_id)
        await tgui.edit_card(query.message, ui.settings_card(prefs))
    elif target == "profile":
        stats = await manager.get_stats(user_id)
        active = await manager.has_active(user_id)
        status = await manager.get_status(user_id)
        await tgui.edit_card(
            query.message,
            ui.profile_card(user_id, stats, status if active else "idle", _last_text(stats)),
        )
    elif target == "status":
        active = await manager.has_active(user_id)
        status = await manager.get_status(user_id)
        await tgui.edit_card(
            query.message, ui.status_screen(active, status, manager.queue_position(user_id))
        )
    elif target == "download":
        await tgui.edit_card(query.message, ui.download_prompt_card())
    elif target == "hls":
        await tgui.edit_card(query.message, ui.hls_prompt_card())
    elif target == "feedback":
        if settings.support_chat_id is None:
            await tgui.answer_query(query, "Support forwarding is not configured by the bot owner.")
            return
        manager.set_feedback_pending(user_id)
        await tgui.edit_card(query.message, ui.feedback_prompt_card())
    else:
        await tgui.answer_query(query, "This button has expired. Send a link to begin again.")


def _display_name_from_query(query) -> str:
    user = query.from_user
    if user is None:
        return "there"
    return user.first_name or user.username or "there"


def _last_text(stats: dict) -> str:
    last_at = stats.get("last_at")
    if not last_at:
        return "never"
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(last_at)))
    except (TypeError, ValueError, OverflowError):
        return "unknown"


async def _on_settings_menu(query, context: ContextTypes.DEFAULT_TYPE, parts: tuple[str, ...]) -> None:
    manager = _manager(context)
    user_id = query.from_user.id if query.from_user else 0
    section = parts[0] if parts else ""
    await tgui.answer_query(query)
    prefs = await manager.get_prefs(user_id)
    if section == "quality":
        await tgui.edit_card(query.message, ui.settings_quality_card(prefs.get("quality", "auto")))
    elif section == "upload":
        await tgui.edit_card(query.message, ui.settings_upload_card(prefs.get("upload_mode", "stream")))
    elif section == "progress":
        await tgui.edit_card(
            query.message, ui.settings_progress_card(prefs.get("progress_mode", "standard"))
        )
    else:
        await tgui.edit_card(query.message, ui.settings_card(prefs))


async def _on_settings_set(query, context: ContextTypes.DEFAULT_TYPE, parts: tuple[str, ...]) -> None:
    manager = _manager(context)
    user_id = query.from_user.id if query.from_user else 0
    if len(parts) != 2:
        await tgui.answer_query(query, "This button has expired. Send a link to begin again.")
        return
    key, value = parts
    try:
        prefs = await manager.set_pref(user_id, key, value)
    except Exception:
        await tgui.answer_query(query, "That value is not supported.")
        return
    labels = {
        ("quality", prefs.get("quality", "auto")): f"Default quality: {prefs.get('quality', 'auto')}",
        ("upload_mode", prefs.get("upload_mode", "stream")): "Upload preference saved",
        ("progress_mode", prefs.get("progress_mode", "standard")): "Progress preference saved",
    }
    await tgui.answer_query(query, labels.get((key, value), "Preference saved."))
    await tgui.edit_card(query.message, ui.settings_card(prefs))


async def _on_start_selection(update, context, query, parts: tuple[str, ...]) -> None:
    manager = _manager(context)
    settings = _settings(context)
    user_id = query.from_user.id if query.from_user else 0
    if len(parts) != 2:
        await tgui.answer_query(query, "This button has expired. Send a link to begin again.")
        return
    session_id, option_key = parts
    session = manager.peek_pending_session(user_id, session_id)
    if session is None:
        await tgui.answer_query(query, "This menu expired. Send the link again.")
        return
    option = next((item for item in session.options if item.key == option_key), None)
    if option is None:
        await tgui.answer_query(query, "That option is no longer available.")
        return
    if option.target_height is None:
        selection = QualitySelection()
    else:
        selection = QualitySelection(mode="height", target_height=option.target_height)
    prefs = await manager.get_prefs(user_id)
    if not await manager.try_reserve(user_id):
        await tgui.answer_query(query, "You already have an active download. Cancel it first.")
        return
    session = manager.pop_pending_session(user_id, session_id)
    if session is None:
        await manager.release_reservation(user_id)
        await tgui.answer_query(query, "This menu expired. Send the link again.")
        return
    manager.set_last_request(user_id, session.url, selection)
    try:
        tempdir = tempfile.mkdtemp(prefix="uvd_", dir=str(settings.download_dir))
    except OSError as exc:
        await manager.release_reservation(user_id)
        logger.error("Failed to create tempdir: %s", type(exc).__name__)
        await tgui.answer_query(query, "Temporary storage is unavailable.")
        await tgui.edit_card(
            query.message,
            ui.error_card(
                kind="failed",
                message="Unable to start a download: temporary storage is unavailable.",
                retry=True,
                support_enabled=_support_enabled(settings),
            ),
        )
        return
    progress_interval = 10.0 if prefs.get("progress_mode", "standard") == "quiet" else 3.0
    await tgui.edit_card(
        query.message,
        ui.status_card(
            title=session.title,
            label=session.label,
            phase="inspecting",
            show_cancel=True,
        ),
    )
    task = asyncio.create_task(
        run_download_job(
            session.url,
            update,
            context,
            query.message,
            user_id,
            tempdir,
            manager,
            selection=selection,
            default_quality=prefs.get("quality", "auto"),
            upload_mode=prefs.get("upload_mode", "stream"),
            progress_interval=progress_interval,
        )
    )
    await manager.register(user_id, task, tempdir)
    await tgui.answer_query(query, "Starting download…")


async def _on_flow_cancel(query, context: ContextTypes.DEFAULT_TYPE, parts: tuple[str, ...]) -> None:
    manager = _manager(context)
    user_id = query.from_user.id if query.from_user else 0
    session_id = parts[0] if parts else ""
    manager.pop_pending_session(user_id, session_id)
    await tgui.answer_query(query, "Selection cancelled.")
    await tgui.edit_card(query.message, ui.download_prompt_card())


async def _on_flow_retry(update, context, query) -> None:
    manager = _manager(context)
    user_id = query.from_user.id if query.from_user else 0
    last = manager.get_last_request(user_id)
    if last is None:
        await tgui.answer_query(query, "No recent request to retry. Send a link first.")
        return
    if await manager.has_active(user_id):
        await tgui.answer_query(query, "You already have an active download. Cancel it first.")
        return
    await tgui.answer_query(query, "Retrying…")
    await handle_url_submission(update, context, last.url, edit_message=query.message)


async def _on_job_cancel(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    manager = _manager(context)
    user_id = query.from_user.id if query.from_user else 0
    cancelled = await manager.cancel(user_id)
    if cancelled:
        await tgui.answer_query(query, "Download cancelled.")
        return
    await tgui.answer_query(query, "No active download to cancel.")


async def _on_hls_view(query, context: ContextTypes.DEFAULT_TYPE, parts: tuple[str, ...]) -> None:
    manager = _manager(context)
    user_id = query.from_user.id if query.from_user else 0
    if len(parts) != 2:
        await tgui.answer_query(query, "This button has expired. Run the extraction again.")
        return
    session_id, index_text = parts
    session = manager.get_hls_session(user_id, session_id)
    if session is None:
        await tgui.answer_query(query, "This result set has expired. Run the extraction again.")
        return
    try:
        index = int(index_text)
    except ValueError:
        index = -1
    if index < 0 or index >= len(session.results):
        await tgui.answer_query(query, "This result no longer exists.")
        return
    result = session.results[index]
    try:
        await context.bot.send_message(chat_id=query.message.chat_id, text=result.url)
    except Exception:
        logger.warning("Sending full HLS URL failed for user %d", user_id)
        await tgui.answer_query(query, "Could not send the URL right now.")
        return
    await tgui.answer_query(query, "Full URL sent as a separate message.")


async def _on_hls_page(query, context: ContextTypes.DEFAULT_TYPE, parts: tuple[str, ...]) -> None:
    manager = _manager(context)
    user_id = query.from_user.id if query.from_user else 0
    if len(parts) != 2:
        await tgui.answer_query(query, "This button has expired. Run the extraction again.")
        return
    session_id, page_text = parts
    session = manager.get_hls_session(user_id, session_id)
    if session is None:
        await tgui.answer_query(query, "This result set has expired. Run the extraction again.")
        return
    try:
        page = int(page_text)
    except ValueError:
        page = 0
    await tgui.answer_query(query)
    await tgui.edit_card(
        query.message,
        ui.hls_results_card(session.url, session.results, page, session.id),
    )


async def _on_hls_run(update, context: ContextTypes.DEFAULT_TYPE, query, parts: tuple[str, ...]) -> None:
    manager = _manager(context)
    user_id = query.from_user.id if query.from_user else 0
    session_id = parts[0] if parts else ""
    session = manager.peek_pending_session(user_id, session_id)
    if session is None:
        await tgui.answer_query(query, "This menu expired. Send the link again.")
        return
    await tgui.answer_query(query, "Starting HLS extraction…")
    await start_hls_extraction(update, context, session.url, edit_message=query.message)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception in handler", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        with contextlib.suppress(Exception):
            await tgui.reply_card(
                update.effective_message,
                ui.error_card(
                    kind="unexpected",
                    message="An unexpected error occurred. Please try again.",
                    support_enabled=False,
                ),
            )


def build_application(settings: Settings) -> Application:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")
    with contextlib.suppress(OSError):
        settings.download_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
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
    application.add_handler(CommandHandler("settings", cmd_settings))
    application.add_handler(CommandHandler("profile", cmd_profile))
    application.add_handler(CommandHandler("download", cmd_download))
    application.add_handler(CommandHandler("hls", cmd_hls))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CallbackQueryHandler(on_callback, pattern=r"^v1:"))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.Regex(r"https?://"), on_url_message)
    )
    application.add_handler(MessageHandler(filters.COMMAND, on_unknown_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_unknown_text)
    )
    application.add_error_handler(on_error)
    return application
