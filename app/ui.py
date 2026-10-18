"""Premium Telegram presentation layer.

This module is deliberately free of Telegram imports so every template,
progress bar, keyboard payload, and quality label can be unit tested with the
standard library. ``app.tgui`` converts :class:`Card` objects into
python-telegram-bot markup at runtime.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Sequence

from . import callbacks, formatting
from .selections import quality_label

BRAND_NAME = "CORN DOWNLOADER"
BRAND_TAGLINE = "Smart video and HLS toolkit"
RULE = "━━━━━━━━━━━━━━"
MAX_TEXT_LENGTH = 4000


@dataclass(frozen=True)
class Button:
    label: str
    data: str = ""
    url: str = ""


@dataclass(frozen=True)
class Card:
    text: str
    buttons: tuple[tuple[Button, ...], ...] = ()
    parse_mode: str = "HTML"

    def clipped(self) -> "Card":
        text = self.text
        if len(text) > MAX_TEXT_LENGTH:
            text = text[: MAX_TEXT_LENGTH - 1] + "…"
        return Card(text=text, buttons=self.buttons, parse_mode=self.parse_mode)


@dataclass(frozen=True)
class Choice:
    key: str
    label: str
    detail: str = ""


def escape(value: object) -> str:
    return html.escape(str(value or ""), quote=False)


def clip(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def menu_button(label: str, target: str) -> Button:
    return Button(label, callbacks.encode("menu", target))


def progress_bar(downloaded: int | float, total: int | float, width: int = 12) -> str | None:
    """Render an accurate block progress bar, or None when unknown."""
    try:
        done = float(downloaded)
        size = float(total)
    except (TypeError, ValueError):
        return None
    if size <= 0 or done < 0 or width <= 0:
        return None
    fraction = min(1.0, max(0.0, done / size))
    filled = min(width, int(fraction * width))
    empty = width - filled
    percent = min(100, int(fraction * 100))
    return f"{'▰' * filled}{'▱' * empty} {percent}%"


def _head(title: str, subtitle: str = "") -> str:
    lines = [f"<b>⚡ {escape(BRAND_NAME)}</b>", f"<i>{escape(BRAND_TAGLINE)}</i>", RULE, f"<b>{escape(title)}</b>"]
    if subtitle:
        lines.append(escape(subtitle))
    return "\n".join(lines)


def welcome_card(display_name: str, max_mb: int) -> Card:
    name = escape(clip(display_name or "there", 40))
    text = "\n".join(
        [
            _head("WELCOME", f"Hello, {name}!"),
            "",
            "Turn public video links into downloadable media in seconds.",
            "",
            "<b>⚡ FAST PROCESSING</b>",
            "<b>🎬 HIGH-QUALITY DOWNLOADS</b>",
            "<b>🔗 HLS, DIRECT, AND PLATFORM LINKS</b>",
            "<b>📊 LIVE PROGRESS</b>",
            "<b>🛡️ SECURE BY DESIGN</b>",
            "",
            RULE,
            f"📥 Send a supported video URL to begin. Max file size: <b>{int(max_mb)} MB</b>.",
        ]
    )
    return Card(
        text=text,
        buttons=(
            (menu_button("📥 Download Video", "download"), menu_button("📖 How to Use", "help")),
            (menu_button("⚙️ Settings", "settings"), menu_button("👤 Profile", "profile")),
            (menu_button("🔗 Platforms", "platforms"), menu_button("ℹ️ About", "about")),
            (menu_button("🔍 Extract HLS Links", "hls"),),
        ),
    ).clipped()


def help_card() -> Card:
    text = "\n".join(
        [
            _head("HOW TO USE"),
            "",
            "<b>01</b> — 🔗 Copy a public video URL.",
            "<b>02</b> — 📩 Send the URL here or use /download.",
            "<b>03</b> — 🎬 Choose a genuinely available quality.",
            "<b>04</b> — ⚡ Follow live progress.",
            "<b>05</b> — ✅ Receive your delivered file.",
            "<b>06</b> — 🔍 Use /hls &lt;url&gt; to find HLS playlist links on a page.",
            "",
            RULE,
            "<b>💡 QUICK TIPS</b>",
            "• Use valid, publicly accessible links.",
            "• Only offered qualities are actually available.",
            "• Large files take longer and may be split.",
            "• /hls discovers public .m3u8 links without downloading video.",
            "• Download only content you may access.",
        ]
    )
    return Card(
        text=text,
        buttons=(
            (menu_button("🔗 Supported Platforms", "platforms"), menu_button("📥 Download", "download")),
            (menu_button("🔍 Extract HLS Links", "hls"), menu_button("🏠 Main Menu", "home")),
        ),
    ).clipped()


def about_card(max_mb: int, configured_mb: int) -> Card:
    text = "\n".join(
        [
            _head("ABOUT CORN DOWNLOADER"),
            "",
            "• yt-dlp for platform pages.",
            "• Native HLS engine for <code>.m3u8</code> playlists.",
            "• Streaming HTTP download for direct media links.",
            "• FFmpeg merging, validation, splitting, and delivery.",
            "• SSRF protection, queues, budgets, and cleanup.",
            "",
            RULE,
            f"Deliverable limit: <b>{int(max_mb)} MB</b> (configured max: {int(configured_mb)} MB).",
            "Private, login-walled, DRM, paywalled, and geo-blocked media are refused.",
        ]
    )
    return Card(
        text=text,
        buttons=((menu_button("🏠 Main Menu", "home"), menu_button("📖 How to Use", "help")),),
    ).clipped()


def platforms_card(labels: tuple[str, ...]) -> Card:
    shown = ", ".join(escape(label) for label in labels[:24])
    text = "\n".join(
        [
            _head("SUPPORTED SOURCES"),
            "",
            "Detection covers these source families. Actual availability still depends on the link and extractor:",
            "",
            shown,
            "",
            RULE,
            "Direct <code>.mp4</code> links and direct <code>.m3u8</code> playlists are also accepted.",
        ]
    )
    return Card(
        text=text,
        buttons=((menu_button("📥 Download", "download"), menu_button("🏠 Main Menu", "home")),),
    ).clipped()


def settings_card(prefs: dict[str, str]) -> Card:
    quality = quality_label(prefs.get("quality", "auto"))
    upload = "Streaming video" if prefs.get("upload_mode", "stream") == "stream" else "File document"
    updates = "Live progress" if prefs.get("progress_mode", "standard") == "standard" else "Quiet milestones"
    text = "\n".join(
        [
            _head("SETTINGS", "Customize your download experience."),
            "",
            f"🎬 Default quality: <b>{escape(quality)}</b>",
            f"📤 Preferred upload: <b>{escape(upload)}</b>",
            f"🔔 Progress updates: <b>{escape(updates)}</b>",
            "",
            RULE,
            "Every option below applies immediately and is saved.",
        ]
    )
    return Card(
        text=text,
        buttons=(
            (
                Button("🎬 Quality", callbacks.encode("settings_menu", "quality")),
                Button("📤 Upload", callbacks.encode("settings_menu", "upload")),
            ),
            (
                Button("🔔 Updates", callbacks.encode("settings_menu", "progress")),
                menu_button("🏠 Main Menu", "home"),
            ),
        ),
    ).clipped()


def settings_quality_card(current: str) -> Card:
    rows: list[tuple[Button, ...]] = []
    values = ("auto", "2160", "1440", "1080", "720", "480", "360")
    for index in range(0, len(values), 3):
        chunk = values[index : index + 3]
        rows.append(
            tuple(
                Button(
                    f"{'✅ ' if value == current else ''}{quality_label(value)}",
                    callbacks.encode("settings_set", "quality", value),
                )
                for value in chunk
            )
        )
    rows.append((Button("↩️ Back to Settings", callbacks.encode("menu", "settings")),))
    return Card(
        text="\n".join(
            [
                _head("DEFAULT QUALITY"),
                "",
                "Auto chooses the best quality inside global and Telegram limits.",
                f"Current: <b>{escape(quality_label(current))}</b>",
            ]
        ),
        buttons=tuple(rows),
    ).clipped()


def settings_upload_card(current: str) -> Card:
    text = "\n".join(
        [
            _head("PREFERRED UPLOAD"),
            "",
            "Streaming sends watchable video first and falls back to a file.",
            "File sends a document first and falls back to streaming video.",
            f"Current: <b>{'Streaming video' if current == 'stream' else 'File document'}</b>",
        ]
    )
    return Card(
        text=text,
        buttons=(
            (
                Button("🎬 Streaming", callbacks.encode("settings_set", "upload_mode", "stream")),
                Button("📁 File", callbacks.encode("settings_set", "upload_mode", "file")),
            ),
            (Button("↩️ Back to Settings", callbacks.encode("menu", "settings")),),
        ),
    ).clipped()


def settings_progress_card(current: str) -> Card:
    text = "\n".join(
        [
            _head("PROGRESS UPDATES"),
            "",
            "Live progress edits the same message frequently with speed and ETA.",
            "Quiet milestones update rarely and still report completion and errors.",
            f"Current: <b>{'Live progress' if current == 'standard' else 'Quiet milestones'}</b>",
        ]
    )
    return Card(
        text=text,
        buttons=(
            (
                Button("📊 Live", callbacks.encode("settings_set", "progress_mode", "standard")),
                Button("🔕 Quiet", callbacks.encode("settings_set", "progress_mode", "quiet")),
            ),
            (Button("↩️ Back to Settings", callbacks.encode("menu", "settings")),),
        ),
    ).clipped()


def profile_card(user_id: int, stats: dict, active_text: str, last_text: str) -> Card:
    completed = int(stats.get("completed", 0) or 0)
    data = formatting.format_bytes(stats.get("bytes", 0))
    text = "\n".join(
        [
            _head("YOUR PROFILE"),
            "",
            f"🆔 User ID: <code>{int(user_id)}</code>",
            f"📥 Completed downloads: <b>{completed}</b>",
            f"📦 Data delivered: <b>{escape(data)}</b>",
            f"📊 Current activity: <b>{escape(active_text)}</b>",
            f"🕘 Last completed: <b>{escape(last_text)}</b>",
            "",
            RULE,
            "Statistics count only successfully delivered files.",
        ]
    )
    return Card(
        text=text,
        buttons=(
            (
                Button("🔄 Refresh", callbacks.encode("menu", "profile")),
                menu_button("⚙️ Settings", "settings"),
            ),
            (menu_button("🏠 Main Menu", "home"),),
        ),
    ).clipped()


def status_screen(active: bool, status_text: str, position: int | None) -> Card:
    if active:
        body = f"⏳ <b>Active:</b> {escape(clip(status_text, 160))}"
        if position:
            body += f"\n🧭 Queue position: <b>{int(position)}</b>"
        buttons = ((Button("🛑 Cancel Download", callbacks.encode("job_cancel")),),)
    else:
        body = f"ℹ️ <b>Status:</b> {escape(clip(status_text, 160))}"
        buttons = ((menu_button("📥 Download", "download"), menu_button("🏠 Main Menu", "home")),)
    return Card(text="\n".join([_head("DOWNLOAD STATUS"), "", body]), buttons=buttons).clipped()


def unknown_command_card(command: str) -> Card:
    shown = escape(clip(command.split()[0] if command else "", 32) or "unknown command")
    return Card(
        text="\n".join(
            [
                _head("UNKNOWN COMMAND"),
                "",
                f"I don’t recognize <code>{shown}</code>.",
                "Use the menu below or send a video URL.",
            ]
        ),
        buttons=((menu_button("🏠 Main Menu", "home"), menu_button("📖 How to Use", "help")),),
    ).clipped()


def analyzing_card() -> Card:
    return Card(
        text="\n".join([_head("ANALYZING LINK"), "", "🔍 Detecting platform and available streams…"]),
    ).clipped()


def download_prompt_card() -> Card:
    return Card(
        text="\n".join(
            [
                _head("NEW DOWNLOAD"),
                "",
                "📥 Send a public video URL now.",
                "Direct <code>.mp4</code> and <code>.m3u8</code> links are welcome.",
            ]
        ),
        buttons=((menu_button("🔗 Platforms", "platforms"), menu_button("🏠 Main Menu", "home")),),
    ).clipped()


def detection_card(
    *,
    kind: str,
    platform_label: str,
    title: str,
    duration_text: str,
    quality_text: str,
    choices: tuple[Choice, ...],
    session_id: str,
    live: bool = False,
    note: str = "",
) -> Card:
    kind_title = {"hls": "VIDEO DETECTED", "direct": "DIRECT LINK DETECTED"}.get(kind, "VIDEO DETECTED")
    lines = [
        _head(kind_title, "Video ready to process"),
        "",
        f"🌐 Platform: <b>{escape(platform_label)}</b>",
        f"📝 Title: <b>{escape(clip(title, 140))}</b>",
        f"⏱️ Duration: <b>{escape(duration_text)}</b>",
        f"📦 Available quality: <b>{escape(quality_text)}</b>",
    ]
    if live:
        lines.append("📡 Live playlist: the current window will be saved as a snapshot.")
    if note:
        lines.append(f"ℹ️ {escape(note)}")
    lines.extend(["", "Choose a genuinely available option below."])
    rows: list[tuple[Button, ...]] = []
    for position in range(0, len(choices), 2):
        chunk = choices[position : position + 2]
        rows.append(
            tuple(Button(choice.label, callbacks.encode("start", session_id, choice.key)) for choice in chunk)
        )
    rows.append(
        (Button("🔍 Find HLS Links", callbacks.encode("hls_run", session_id)),)
    )
    rows.append((Button("❌ Cancel", callbacks.encode("flow_cancel", session_id)),))
    return Card(text="\n".join(lines), buttons=tuple(rows)).clipped()


def status_card(
    *,
    title: str,
    label: str,
    phase: str,
    downloaded: int = 0,
    total: int = 0,
    speed: float = 0.0,
    eta: float | None = None,
    detail: str = "",
    queue_position: int | None = None,
    show_cancel: bool = True,
) -> Card:
    phase_titles = {
        "inspecting": "ANALYZING LINK",
        "extracting": "READING VIDEO INFO",
        "queued": "QUEUED",
        "downloading": "DOWNLOADING YOUR VIDEO",
        "processing": "PROCESSING VIDEO",
        "uploading": "UPLOADING TO TELEGRAM",
        "finalizing": "FINALIZING",
    }
    lines = [_head(phase_titles.get(phase, "WORKING"), escape(clip(title, 120))), "", f"🎞️ Source: <b>{escape(label)}</b>"]
    if phase == "queued":
        if queue_position:
            lines.append(f"🧭 Queue position: <b>{int(queue_position)}</b>")
        else:
            lines.append("🧭 Waiting for a download slot…")
    elif phase in ("downloading", "uploading"):
        bar = progress_bar(downloaded, total)
        if bar is not None:
            verb = "Progress" if phase == "downloading" else "Uploaded"
            lines.append(f"📊 {verb}: <b>{escape(bar)}</b>")
            lines.append(
                f"📦 {escape(formatting.format_bytes(downloaded))} / {escape(formatting.format_bytes(total))}"
            )
        elif detail:
            lines.append(f"📊 Progress: <b>{escape(detail)}</b>")
            lines.append(f"📦 Transferred: <b>{escape(formatting.format_bytes(downloaded))}</b>")
        else:
            lines.append(f"📦 Transferred: <b>{escape(formatting.format_bytes(downloaded))}</b>")
        if speed:
            eta_text = f" • ⏳ {escape(formatting.format_eta(eta))}" if eta is not None else ""
            lines.append(f"🚀 {escape(formatting.format_speed(speed))}{eta_text}")
    elif phase == "processing":
        lines.append("🎞️ Merging, remuxing, validating, and preparing delivery…")
    elif phase in ("inspecting", "extracting", "finalizing"):
        lines.append("🔄 Status: processing your request…")
    lines.extend(["", RULE, "🔄 Status: the same message updates automatically."])
    buttons = ((Button("🛑 Cancel Download", callbacks.encode("job_cancel")),),) if show_cancel else ()
    return Card(text="\n".join(lines), buttons=buttons).clipped()


def completion_card(
    *,
    file_name: str,
    size_bytes: int,
    quality: str,
    mode: str,
    parts: int,
    support_enabled: bool,
) -> Card:
    mode_text = {"single": "Single delivery", "mtproto": "Direct large-file upload", "split": f"Split delivery ({parts} parts)"}.get(
        mode, "Delivered"
    )
    text = "\n".join(
        [
            _head("DOWNLOAD COMPLETE", "Your video is ready!"),
            "",
            f"📁 File: <b>{escape(clip(file_name, 120))}</b>",
            f"📦 Size: <b>{escape(formatting.format_bytes(size_bytes))}</b>",
            f"🎬 Quality: <b>{escape(quality)}</b>",
            f"⚡ Status: <b>{escape(mode_text)}</b>",
            "",
            "Thank you for using Corn Downloader!",
        ]
    )
    rows = [
        (
            Button("🔄 Download Another", callbacks.encode("menu", "download")),
            menu_button("🏠 Main Menu", "home"),
        )
    ]
    if support_enabled:
        rows.append((Button("💬 Feedback", callbacks.encode("menu", "feedback")),))
    return Card(text=text, buttons=tuple(rows)).clipped()


def cancelled_card() -> Card:
    return Card(
        text="\n".join([_head("DOWNLOAD CANCELLED"), "", "🛑 The active download was stopped and temporary files were removed."]),
        buttons=((menu_button("📥 Download", "download"), menu_button("🏠 Main Menu", "home")),),
    ).clipped()


def no_active_card() -> Card:
    return Card(
        text="\n".join(
            [
                _head("NOTHING TO CANCEL"),
                "",
                "ℹ️ There is no active download or extraction for your account right now.",
            ]
        ),
        buttons=((menu_button("📥 Download", "download"), menu_button("🏠 Main Menu", "home")),),
    ).clipped()


def error_card(
    *,
    kind: str,
    message: str,
    detail: str = "",
    retry: bool = False,
    support_enabled: bool = False,
    include_platforms: bool = False,
) -> Card:
    titles = {
        "invalid": "⚠️ INVALID VIDEO LINK",
        "blocked": "🛡️ LINK BLOCKED",
        "unsupported": "🌐 PLATFORM NOT SUPPORTED",
        "auth": "🔐 ACCESS REQUIRED",
        "drm": "🔐 DRM-PROTECTED STREAM",
        "geo": "🌍 REGION RESTRICTED",
        "expired": "⌛ LINK EXPIRED",
        "too_large": "📦 FILE TOO LARGE",
        "network": "📡 NETWORK ISSUE",
        "quality": "🎬 QUALITY UNAVAILABLE",
        "failed": "❌ DOWNLOAD FAILED",
        "unexpected": "❌ UNEXPECTED ERROR",
    }
    lines = [_head("CORN DOWNLOADER", titles.get(kind, "❌ DOWNLOAD FAILED")), "", escape(clip(message, 700))]
    if detail:
        lines.extend(["", f"🔧 Detail: <code>{escape(clip(detail, 180))}</code>"])
    rows: list[tuple[Button, ...]] = []
    first: list[Button] = []
    if retry:
        first.append(Button("🔄 Retry", callbacks.encode("flow_retry")))
    if include_platforms:
        first.append(menu_button("🔗 Platforms", "platforms"))
    first.append(menu_button("🏠 Main Menu", "home"))
    rows.append(tuple(first))
    second: list[Button] = [menu_button("📖 Help", "help")]
    if support_enabled and kind in ("failed", "network", "unexpected", "quality"):
        second.append(Button("💬 Feedback", callbacks.encode("menu", "feedback")))
    rows.append(tuple(second))
    return Card(text="\n".join(lines), buttons=tuple(rows)).clipped()


def feedback_prompt_card() -> Card:
    return Card(
        text="\n".join(
            [
                _head("FEEDBACK"),
                "",
                "💬 Send one text message describing the issue or idea.",
                "Links and commands cancel feedback mode and are processed normally.",
            ]
        ),
        buttons=((menu_button("🏠 Main Menu", "home"),),),
    ).clipped()


def feedback_sent_card() -> Card:
    return Card(
        text="\n".join([_head("FEEDBACK RECEIVED"), "", "✅ Thank you! Your message was forwarded to support."]),
        buttons=((menu_button("📥 Download", "download"), menu_button("🏠 Main Menu", "home")),),
    ).clipped()


def choice_rows_for_test() -> tuple[str, ...]:
    return ("menu", "settings", "start", "flow_cancel", "flow_retry", "job_cancel")


# ---------------------------------------------------------------------------
# HLS link extractor cards
# ---------------------------------------------------------------------------

HLS_VERIFICATION_LABELS = {
    "VERIFIED_PLAYLIST": "✅ Verified playlist (content checked)",
    "URL_FOUND_NOT_VERIFIED": "🔍 URL Found — Playback Not Verified",
    "ACCESS_DENIED": "🚫 Access Denied",
    "EXPIRED_OR_INVALID": "⌛ Expired or Invalid",
    "EXTRACTION_FAILED": "❌ Could Not Verify",
}
HLS_TYPE_LABELS = {"master": "Master Playlist", "media": "Media Playlist", "unknown": "Unknown"}
_HLS_PHASES = {
    "scanning": ("SCANNING WEBPAGE", "🌐 Fetching the public page…"),
    "inspecting": ("INSPECTING PLAYLIST", "🔍 Fetching the playlist…"),
    "analyzing": ("ANALYZING HTML SOURCE", "🧩 Parsing page source for stream references…"),
    "searching": ("SEARCHING FOR HLS PLAYLISTS", "🔎 Collecting candidate .m3u8 URLs…"),
    "verifying": ("VERIFYING PLAYLISTS", "✅ Checking discovered playlists…"),
}


def hls_prompt_card() -> Card:
    text = "\n".join(
        [
            _head("HLS LINK EXTRACTOR", "Find public HLS playlist URLs"),
            "",
            "Send a webpage URL or a direct <code>.m3u8</code> link.",
            "",
            "<b>EXAMPLE</b>",
            "<code>/hls https://example.com/watch/video123</code>",
            "",
            RULE,
            "The bot scans public page source and browser network traffic",
            "(when enabled) for HLS playlists, then verifies what it finds.",
            "",
            "🔒 Only public pages are scanned. Login-walled, DRM, CAPTCHA,",
            "and paywalled streams are not bypassed. Not every site exposes",
            "a detectable HLS URL.",
        ]
    )
    return Card(
        text=text,
        buttons=((menu_button("🏠 Main Menu", "home"),),),
    ).clipped()


def hls_progress_card(phase: str) -> Card:
    title, body = _HLS_PHASES.get(phase, ("WORKING", "🔄 Processing…"))
    return Card(
        text="\n".join(
            [_head(title), "", body, "", RULE, "🔄 The same message updates automatically."]
        )
    ).clipped()


def _hls_result_block(number: int, result) -> str:
    lines = [
        f"<b>Result {int(number)}</b>",
        f"🔍 Source method: <b>{escape(result.method)}</b>",
        f"📋 Playlist type: <b>{escape(HLS_TYPE_LABELS.get(result.playlist_type, 'Unknown'))}</b>",
        f"✔️ Verification: <b>{escape(HLS_VERIFICATION_LABELS.get(result.status, result.status))}</b>",
    ]
    if result.http_status:
        lines.append(f"🌐 HTTP status: <b>{int(result.http_status)}</b>")
    if result.detail:
        lines.append(f"ℹ️ {escape(clip(result.detail, 160))}")
    url_text = escape(result.url)
    if len(result.url) > 3500:
        url_text = escape(result.url[:3400]) + "…"
        lines.append("⚠️ URL is very long — use View Full URL for the complete link.")
    lines.extend(["", f"<code>{url_text}</code>"])
    return "\n".join(lines)


def _paginate_hls_results(results: Sequence, *, max_per_page: int = 3, max_chars: int = 3400):
    """Split results into pages that respect Telegram's message limit."""
    pages: list[tuple] = []
    current: list = []
    size = 500
    for result in results:
        block = 240 + len(result.url) + len(result.detail or "")
        if current and (len(current) >= max_per_page or size + block > max_chars):
            pages.append(tuple(current))
            current = []
            size = 500
        current.append(result)
        size += block
    if current:
        pages.append(tuple(current))
    return pages or [tuple()]


def hls_results_card(
    source_url: str,
    results: Sequence,
    page: int,
    session_id: str,
    *,
    browser_note: str = "",
) -> Card:
    pages = _paginate_hls_results(tuple(results))
    page = max(0, min(int(page), len(pages) - 1))
    shown = pages[page]
    verified_count = sum(1 for item in results if item.status == "VERIFIED_PLAYLIST")
    status_line = "HLS URLs detected"
    if verified_count:
        status_line = f"HLS URLs detected ({len(results)} found, {verified_count} verified)"
    else:
        status_line = f"HLS URLs detected ({len(results)} found)"
    lines = [
        _head("HLS EXTRACTION RESULTS", "Scan complete"),
        "",
        f"🌐 Source: <code>{escape(clip(source_url, 300))}</code>",
        f"📊 Status: <b>{escape(status_line)}</b>",
        "",
    ]
    base_index = 0
    for prior_page in pages[:page]:
        base_index += len(prior_page)
    for offset, result in enumerate(shown):
        lines.append(_hls_result_block(base_index + offset + 1, result))
        if offset < len(shown) - 1:
            lines.append(RULE)
    if browser_note:
        lines.extend(["", f"ℹ️ {escape(clip(browser_note, 200))}"])
    lines.extend(["", RULE, "📄 Full URLs are shown above — copy them directly."])
    rows: list[tuple[Button, ...]] = []
    for offset, result in enumerate(shown):
        index = base_index + offset
        rows.append(
            (
                Button("🔗 Open Playlist", url=result.url),
                Button(
                    f"👁 View Full URL {index + 1}",
                    callbacks.encode("hls_view", session_id, str(index)),
                ),
            )
        )
    if len(pages) > 1:
        nav: list[Button] = []
        if page > 0:
            nav.append(Button("⬅️ Prev", callbacks.encode("hls_page", session_id, str(page - 1))))
        nav.append(Button(f"Page {page + 1}/{len(pages)}", callbacks.encode("hls_page", session_id, str(page))))
        if page < len(pages) - 1:
            nav.append(Button("Next ➡️", callbacks.encode("hls_page", session_id, str(page + 1))))
        rows.append(tuple(nav))
    rows.append(
        (
            Button("🔍 Extract Another", callbacks.encode("hls_again")),
            menu_button("🏠 Main Menu", "home"),
        )
    )
    return Card(text="\n".join(lines), buttons=tuple(rows)).clipped()


def hls_error_card(kind: str, message: str, *, browser_note: str = "") -> Card:
    titles = {
        "invalid": "⚠️ INVALID LINK",
        "blocked": "🛡️ LINK BLOCKED",
        "forbidden": "🚫 ACCESS DENIED",
        "not_found": "📄 PAGE NOT FOUND",
        "gone": "⌛ LINK EXPIRED",
        "rate_limited": "⏳ RATE LIMITED",
        "server": "📡 SERVER ERROR",
        "network": "📡 NETWORK ISSUE",
        "timeout": "⏱️ TIMED OUT",
        "no_results": "🔎 NO HLS STREAM FOUND",
        "busy": "⏳ EXTRACTION IN PROGRESS",
        "disabled": "⛔ FEATURE DISABLED",
        "failed": "❌ EXTRACTION FAILED",
    }
    lines = [_head("HLS LINK EXTRACTOR", titles.get(kind, "❌ EXTRACTION FAILED")), "", escape(clip(message, 700))]
    if browser_note:
        lines.extend(["", f"ℹ️ {escape(clip(browser_note, 200))}"])
    return Card(
        text="\n".join(lines),
        buttons=(
            (
                Button("🔍 Extract Another", callbacks.encode("hls_again")),
                menu_button("🏠 Main Menu", "home"),
            ),
            (menu_button("📖 Help", "help"),),
        ),
    ).clipped()


def hls_cancelled_card() -> Card:
    return Card(
        text="\n".join(
            [
                _head("EXTRACTION CANCELLED"),
                "",
                "🛑 The HLS extraction was stopped. No results were kept.",
            ]
        ),
        buttons=(
            (
                Button("🔍 Extract Another", callbacks.encode("hls_again")),
                menu_button("🏠 Main Menu", "home"),
            ),
        ),
    ).clipped()
