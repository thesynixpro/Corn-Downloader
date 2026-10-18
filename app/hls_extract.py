"""HLS link extraction engine.

Discovers publicly accessible HLS playlist URLs (``.m3u8``) from a submitted
webpage URL or a direct playlist URL using several strategies:

* **direct inspection** — the submitted URL itself is (or redirects to) an
  HLS playlist,
* **HTML source scanning** — tag attributes (``<source>``, ``<video>``,
  ``<meta>``, ``<link>`` …), ``<script>`` blocks (player configuration,
  JSON-LD, JavaScript config) and a raw-text pass that also catches escaped
  representations such as ``https:\\/\\/cdn\\/master.m3u8`` and ``\\u0026``,
* **browser network observation** — optional Playwright fallback (see
  :mod:`app.browser_extract`), merged in by :func:`run_extraction`.

Discovery is deliberately separated from verification: a URL may be found in
page source without its contents ever being fetched. Verification fetches a
bounded amount of each candidate and checks for the ``#EXTM3U`` marker, then
classifies the result (``VERIFIED_PLAYLIST``, ``URL_FOUND_NOT_VERIFIED``,
``ACCESS_DENIED``, ``EXPIRED_OR_INVALID``). An HTTP 200 alone never marks a
stream as playable.

This module is stdlib-only at import time. The HTTP implementation is injected
through a ``fetch`` callable so unit tests run without network access or
third-party packages. Every candidate URL passes the shared SSRF guard
(:func:`app.urlguard.ensure_public_url`) before it is fetched, returned, or
shown to the user.
"""

from __future__ import annotations

import asyncio
import dataclasses
import html
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable, Sequence
from urllib.parse import urljoin, urlsplit

from .config import Settings
from .hls import PlaylistError, PlaylistKind, detect_playlist_kind, parse_master
from .platforms import is_probable_hls
from .urlguard import URLGuardError, ensure_public_url, parse_http_url

logger = logging.getLogger("uvd_bot.hls_extract")

# Discovery methods (shown to the user exactly as listed here).
METHOD_DIRECT = "Direct M3U8 URL"
METHOD_HTML = "HTML Source"
METHOD_EMBEDDED = "Embedded Player"
METHOD_JS = "JavaScript Configuration"
METHOD_BROWSER = "Browser Network Request"
METHOD_MASTER = "Master Playlist Reference"

# Verification states.
STATUS_VERIFIED = "VERIFIED_PLAYLIST"
STATUS_FOUND = "URL_FOUND_NOT_VERIFIED"
STATUS_DENIED = "ACCESS_DENIED"
STATUS_INVALID = "EXPIRED_OR_INVALID"
STATUS_FAILED = "EXTRACTION_FAILED"

DEFAULT_PAGE_MAX_BYTES = 5 * 1024 * 1024
VERIFY_MAX_BYTES = 256 * 1024
MAX_CANDIDATES_SCANNED = 25
MAX_REDIRECT_HOPS = 5
MASTER_CHILD_VERIFY_BUDGET = 3

# Absolute / protocol-relative playlist references in free text.
_ABS_RE = re.compile(
    r"(?:https?:)?//[^\s\"'<>\\)\]]+?\.m3u8(?:\?[^\s\"'<>\\)\]]*)?",
    re.IGNORECASE,
)
# Root- or dot-relative playlist references in free text.
_REL_RE = re.compile(
    r"(?<![A-Za-z0-9])((?:\./|\.\./|/)[^\s\"'<>\\)\]]+?\.m3u8(?:\?[^\s\"'<>\\)\]]*)?)",
    re.IGNORECASE,
)

# Heuristic markers that a <script> block contains an embedded player config.
_PLAYER_MARKERS = (
    "sources",
    "file:",
    "playlist",
    "hlsurl",
    '"hls"',
    "'hls'",
    "contenturl",
    "content_url",
    "mediaurl",
    "media_url",
    "video_url",
    "streamurl",
    "stream_url",
    "m3u8_url",
    "videourl",
    "videojs",
    "jwplayer",
    "player.src",
    "mpegurl",
)

_DRM_MARKERS = ("sample-aes", "widevine", "fairplay", "playready")


class ExtractionError(Exception):
    """Raised when the submitted URL cannot be processed at all.

    ``kind`` is a short machine-readable token mapped to user-facing text by
    the UI layer (``invalid``, ``blocked``, ``forbidden``, ``not_found``,
    ``gone``, ``rate_limited``, ``server``, ``network``, ``timeout``).
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class FetchResult:
    """Outcome of one bounded HTTP fetch.

    ``ok`` is False only for transport-level failures; HTTP error statuses
    are returned with ``ok=True`` so callers can classify them.
    """

    ok: bool
    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    final_url: str = ""
    error: str = ""  # timeout | dns | network | blocked | too_many_redirects

    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


@dataclass(frozen=True)
class HlsFound:
    """One discovered playlist URL and everything known about it."""

    url: str
    method: str
    playlist_type: str = "unknown"  # master | media | unknown
    status: str = STATUS_FOUND
    http_status: int = 0
    detail: str = ""


@dataclass
class ExtractionOutcome:
    """Result of one extraction run."""

    source_url: str
    results: list[HlsFound] = field(default_factory=list)
    error: str = ""
    error_kind: str = ""
    browser_note: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.results)


FetchFn = Callable[..., Awaitable[FetchResult]]
PhaseFn = Callable[[str], Awaitable[None]]


# ---------------------------------------------------------------------------
# URL text normalization and candidate discovery
# ---------------------------------------------------------------------------


def _unescape_url_text(raw: str) -> str:
    """Undo HTML entities, JS string escapes, and double-escaping."""
    text = html.unescape(raw or "")
    for _ in range(2):
        text = text.replace("\\/", "/")
    text = (
        text.replace("\\u0026", "&")
        .replace("\\u003d", "=")
        .replace("\\u003f", "?")
        .replace("\\u002f", "/")
        .replace("\\u003a", ":")
    )
    return text


def normalize_candidate(raw: str, base_url: str) -> str | None:
    """Turn a discovered reference into an absolute http(s) URL, or None.

    Relative and protocol-relative references are resolved against
    ``base_url``. Query parameters and fragments are preserved exactly;
    nothing is stripped or reordered.
    """
    text = _unescape_url_text(raw).strip().strip("'\"").strip()
    if not text or ".m3u8" not in text.lower():
        return None
    text = text.rstrip(");,")
    if text.startswith("//"):
        scheme = urlsplit(base_url).scheme or "https"
        text = f"{scheme}:{text}"
    try:
        absolute = urljoin(base_url, text)
        parts = urlsplit(absolute)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password:
        return None
    return absolute


def find_urls_in_text(text: str, base_url: str) -> list[str]:
    """Extract absolute/protocol-relative/relative playlist URLs from text."""
    prepared = _unescape_url_text(text or "")
    found: list[str] = []
    absolute_spans: list[tuple[int, int]] = []
    for match in _ABS_RE.finditer(prepared):
        candidate = normalize_candidate(match.group(0), base_url)
        if candidate:
            found.append(candidate)
            absolute_spans.append(match.span())
    for match in _REL_RE.finditer(prepared):
        start, end = match.span()
        # Skip references already covered by an absolute/protocol-relative
        # match; re-matching the tail of "https://host/x.m3u8" as a
        # root-relative path would fabricate a second, wrong URL.
        if any(span_start <= start < span_end for span_start, span_end in absolute_spans):
            continue
        candidate = normalize_candidate(match.group(1), base_url)
        if candidate:
            found.append(candidate)
    return found


# ---------------------------------------------------------------------------
# HTML scanning (stdlib parser, no third-party dependency)
# ---------------------------------------------------------------------------


class _PageScanner:
    """Collects playlist references from a page using stdlib html.parser."""

    def __init__(self, base_url: str) -> None:
        from html.parser import HTMLParser

        self.base_url = base_url
        self.attribute_hits: list[str] = []
        self.script_blocks: list[tuple[str, str]] = []  # (content, type attr)
        self.base_href: str = ""
        outer = self

        class _Parser(HTMLParser):
            def __init__(self) -> None:
                super().__init__(convert_charrefs=True)
                self._in_script = False
                self._script_type = ""
                self._script_buf: list[str] = []

            def handle_starttag(self, tag, attrs):
                attrs_dict = {str(key).lower(): (value or "") for key, value in attrs}
                if tag == "base" and not outer.base_href:
                    href = attrs_dict.get("href", "").strip()
                    if href:
                        outer.base_href = href
                if tag == "script":
                    self._in_script = True
                    self._script_type = attrs_dict.get("type", "").strip().lower()
                    self._script_buf = []
                    return
                for value in attrs_dict.values():
                    if value and ".m3u8" in value.lower():
                        outer.attribute_hits.append(value)

            def handle_startendtag(self, tag, attrs):
                self.handle_starttag(tag, attrs)

            def handle_data(self, data):
                if self._in_script:
                    self._script_buf.append(data)

            def handle_endtag(self, tag):
                if tag == "script" and self._in_script:
                    self._in_script = False
                    outer.script_blocks.append(("".join(self._script_buf), self._script_type))

        self._parser = _Parser()

    def feed(self, html_text: str) -> None:
        try:
            self._parser.feed(html_text or "")
            self._parser.close()
        except Exception:  # noqa: BLE001 - malformed HTML must not abort a scan
            logger.debug("HTML parser recovered from malformed markup.", exc_info=True)


def _looks_like_player_config(script: str) -> bool:
    low = (script or "").lower()
    return any(marker in low for marker in _PLAYER_MARKERS)


def _scan_page(html_text: str, base_url: str) -> list[tuple[str, str]]:
    """Return ``(raw_reference, method)`` pairs found in the page."""
    scanner = _PageScanner(base_url)
    scanner.feed(html_text)
    resolution_base = base_url
    if scanner.base_href:
        resolved = urljoin(base_url, scanner.base_href)
        if urlsplit(resolved).scheme in ("http", "https"):
            resolution_base = resolved
    pairs: list[tuple[str, str]] = []
    for raw in scanner.attribute_hits:
        # srcset may hold several URLs: "a.m3u8 640w, b.m3u8 1280w"
        if "," in raw and ".m3u8" in raw.lower():
            for part in raw.split(","):
                candidate = part.strip().split()[0] if part.strip() else ""
                if candidate and ".m3u8" in candidate.lower():
                    pairs.append((candidate, METHOD_HTML))
        else:
            pairs.append((raw, METHOD_HTML))
    for content, script_type in scanner.script_blocks:
        if not content or ".m3u8" not in content.lower():
            continue
        if "ld+json" in script_type:
            method = METHOD_JS
        elif _looks_like_player_config(content):
            method = METHOD_EMBEDDED
        else:
            method = METHOD_JS
        for reference in find_urls_in_text(content, resolution_base):
            pairs.append((reference, method))
    for reference in find_urls_in_text(html_text, resolution_base):
        pairs.append((reference, METHOD_HTML))
    # Attribute hits need resolution too (they may be relative).
    resolved_attributes: list[tuple[str, str]] = []
    for raw, method in pairs:
        candidate = normalize_candidate(raw, resolution_base)
        if candidate:
            resolved_attributes.append((candidate, method))
    return resolved_attributes


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _classify_verified(text: str) -> tuple[str, str]:
    """Return ``(playlist_type, detail)`` for playlist-looking content."""
    kind = detect_playlist_kind(text)
    if kind == PlaylistKind.MASTER:
        ptype = "master"
    elif kind == PlaylistKind.MEDIA:
        ptype = "media"
    else:
        ptype = "unknown"
    detail = ""
    lowered = text.lower()
    if any(marker in lowered for marker in _DRM_MARKERS):
        detail = "DRM markers detected in the playlist."
    return ptype, detail


async def _verify_candidate(
    fetch: FetchFn, result: HlsFound, *, max_bytes: int = VERIFY_MAX_BYTES
) -> tuple[HlsFound, str]:
    """Verify one discovered URL. Returns ``(updated_result, body_text_or_empty)``."""
    response = await fetch(result.url, max_bytes=max_bytes)
    if not response.ok:
        return replace(result, status=STATUS_FOUND, detail=_transport_detail(response.error)), ""
    status = response.status
    if status in (401, 403):
        return (
            replace(
                result,
                status=STATUS_DENIED,
                http_status=status,
                detail=f"Access denied (HTTP {status}).",
            ),
            "",
        )
    if status in (404, 410):
        return (
            replace(
                result,
                status=STATUS_INVALID,
                http_status=status,
                detail=f"The playlist is missing or expired (HTTP {status}).",
            ),
            "",
        )
    if status == 429:
        return (
            replace(
                result,
                status=STATUS_DENIED,
                http_status=status,
                detail="Rate limited (HTTP 429).",
            ),
            "",
        )
    if status >= 400:
        return (
            replace(
                result,
                status=STATUS_FOUND,
                http_status=status,
                detail=f"Verification got HTTP {status}.",
            ),
            "",
        )
    text = response.text()
    if "#EXTM3U" not in text[:2048]:
        return (
            replace(
                result,
                status=STATUS_FOUND,
                http_status=status,
                detail="The URL responded, but not with HLS playlist content.",
            ),
            "",
        )
    ptype, detail = _classify_verified(text)
    return (
        replace(
            result,
            status=STATUS_VERIFIED,
            playlist_type=ptype,
            http_status=status,
            detail=detail,
        ),
        text,
    )


def _transport_detail(error: str) -> str:
    return {
        "timeout": "Verification timed out.",
        "dns": "The playlist host could not be resolved.",
        "blocked": "The playlist host is blocked.",
        "too_many_redirects": "Too many redirects while verifying.",
    }.get(error, "The playlist could not be verified.")


def _page_failure(status: int, error: str) -> ExtractionError:
    if status == 403:
        return ExtractionError("forbidden", "The webpage returned HTTP 403 Forbidden.")
    if status == 401:
        return ExtractionError("forbidden", "The webpage requires authentication (HTTP 401).")
    if status == 404:
        return ExtractionError("not_found", "The webpage returned HTTP 404 Not Found.")
    if status == 410:
        return ExtractionError("gone", "The webpage is gone or expired (HTTP 410).")
    if status == 429:
        return ExtractionError(
            "rate_limited", "The website is rate-limiting requests (HTTP 429). Try again later."
        )
    if status >= 500:
        return ExtractionError("server", f"The webpage returned a server error (HTTP {status}).")
    if status >= 400:
        return ExtractionError("network", f"The webpage could not be reached (HTTP {status}).")
    if error == "timeout":
        return ExtractionError("timeout", "The extraction timed out while fetching the page.")
    if error == "dns":
        return ExtractionError("network", "The host could not be resolved. Please check the URL.")
    if error == "blocked":
        return ExtractionError(
            "blocked", "This URL cannot be used: private, local, and metadata addresses are blocked."
        )
    if error == "too_many_redirects":
        return ExtractionError("network", "The webpage redirected too many times.")
    return ExtractionError("network", "The network connection failed while fetching the page.")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def _noop_phase(_name: str) -> None:
    return None


async def extract_hls(
    url: str,
    settings: Settings,
    *,
    fetch: FetchFn | None = None,
    browser_urls: Sequence[str] = (),
    phase_cb: PhaseFn | None = None,
) -> ExtractionOutcome:
    """Discover and verify HLS playlists for ``url``.

    ``fetch`` defaults to the SSRF-guarded httpx implementation; tests inject
    a fake. ``browser_urls`` are pre-collected by the optional browser
    fallback and labelled accordingly.
    """
    phase = phase_cb or _noop_phase
    page_max_bytes = max(1, settings.hls_extract_page_max_bytes)
    outcome = ExtractionOutcome(source_url=url)

    try:
        parsed = parse_http_url(url)
    except URLGuardError as exc:
        raise ExtractionError("invalid", "Please provide a valid public HTTP or HTTPS URL.") from exc
    if parsed.scheme not in ("http", "https"):
        raise ExtractionError("invalid", "Please provide a valid public HTTP or HTTPS URL.")

    try:
        validated = await ensure_public_url(url, settings.allowed_hosts)
    except URLGuardError as exc:
        kind = "blocked" if exc.reason in ("blocked_host", "blocked_address") else "invalid"
        raise ExtractionError(kind, str(exc) or "This URL cannot be used.") from exc
    outcome.source_url = validated

    do_verify = settings.hls_extract_verify_limit > 0
    verify_budget = settings.hls_extract_verify_limit

    candidates: list[tuple[str, str]] = []  # (url, method), discovery order
    seen: set[str] = set()

    def add_candidate(candidate: str | None, method: str) -> None:
        if not candidate or candidate in seen or len(seen) >= MAX_CANDIDATES_SCANNED:
            return
        seen.add(candidate)
        candidates.append((candidate, method))

    if is_probable_hls(validated):
        add_candidate(validated, METHOD_DIRECT)
    else:
        if fetch is None:
            fetch = _make_default_fetch(settings)
        await phase("scanning")
        response = await fetch(validated, max_bytes=page_max_bytes)
        if not response.ok or response.status >= 400:
            raise _page_failure(response.status, response.error)
        content_type = (response.headers.get("content-type") or "").lower()
        body_text = response.text()
        final_base = response.final_url or validated
        # The submitted page URL itself may serve a playlist ( redirects or
        # content-type), even when the path does not end in .m3u8.
        if "#EXTM3U" in body_text[:2048] or "mpegurl" in content_type:
            add_candidate(validated, METHOD_DIRECT)
        else:
            await phase("analyzing")
            for reference, method in _scan_page(body_text, final_base):
                add_candidate(reference, method)
            await phase("searching")

    for browser_url in browser_urls:
        candidate = normalize_candidate(browser_url, validated)
        add_candidate(candidate, METHOD_BROWSER)

    if not candidates:
        outcome.error_kind = "no_results"
        outcome.error = "No detectable HLS stream was found."
        return outcome

    # SSRF-validate every candidate before it is fetched or shown.
    safe_candidates: list[HlsFound] = []
    for candidate, method in candidates:
        try:
            safe_url = await ensure_public_url(candidate, settings.allowed_hosts)
        except URLGuardError:
            logger.info("Dropped unsafe HLS candidate (host blocked).")
            continue
        safe_candidates.append(HlsFound(url=safe_url, method=method))
        if len(safe_candidates) >= settings.hls_extract_max_results + MASTER_CHILD_VERIFY_BUDGET:
            break

    if not safe_candidates:
        outcome.error_kind = "no_results"
        outcome.error = "No detectable HLS stream was found."
        return outcome

    if do_verify:
        await phase("verifying")

    verified: list[HlsFound] = []
    child_queue: list[str] = []
    for index, candidate in enumerate(safe_candidates):
        if index >= verify_budget:
            verified.append(candidate)
            continue
        updated, body_text = await _verify_candidate(fetch, candidate)
        verified.append(updated)
        if updated.status == STATUS_VERIFIED and updated.playlist_type == "master" and body_text:
            try:
                master = parse_master(body_text, updated.url)
            except PlaylistError:
                master = None
            if master is not None:
                for variant in master.variants[:8]:
                    child = normalize_candidate(variant.uri, updated.url)
                    if child and child not in seen and child not in child_queue:
                        seen.add(child)
                        child_queue.append(child)

    # One bounded round of child-playlist verification for verified masters.
    for child_url in child_queue[:MASTER_CHILD_VERIFY_BUDGET]:
        if len(verified) >= settings.hls_extract_max_results:
            break
        try:
            safe_child = await ensure_public_url(child_url, settings.allowed_hosts)
        except URLGuardError:
            continue
        child = HlsFound(url=safe_child, method=METHOD_MASTER)
        if do_verify:
            child, _ = await _verify_candidate(fetch, child)
        verified.append(child)

    outcome.results = verified[: settings.hls_extract_max_results]
    if not outcome.results:
        outcome.error_kind = "no_results"
        outcome.error = "No detectable HLS stream was found."
    return outcome


async def run_extraction(
    url: str,
    settings: Settings,
    *,
    fetch: FetchFn | None = None,
    phase_cb: PhaseFn | None = None,
) -> ExtractionOutcome:
    """Full extraction entry point: HTTP engine plus optional browser fallback."""
    browser_urls: list[str] = []
    browser_note = ""
    direct = is_probable_hls(url)
    if settings.hls_browser_enabled and not direct:
        from . import browser_extract

        if browser_extract.browser_available():
            try:
                browser_urls = await browser_extract.collect_hls_requests(
                    url,
                    allowed_hosts=settings.allowed_hosts,
                    timeout_s=min(30.0, float(settings.hls_extract_timeout)),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - browser is best-effort
                logger.info("Browser extraction failed: %s", type(exc).__name__)
                browser_note = "JavaScript-based extraction could not complete on this attempt."
        else:
            browser_note = "JavaScript-based extraction is unavailable on this server."
    outcome = await extract_hls(
        url, settings, fetch=fetch, browser_urls=browser_urls, phase_cb=phase_cb
    )
    if browser_note and not outcome.browser_note:
        outcome.browser_note = browser_note
    return outcome


def _make_default_fetch(settings: Settings) -> FetchFn:
    """SSRF-guarded httpx fetch with per-hop redirect validation.

    httpx is imported lazily so this module stays importable (and unit
    testable) without third-party packages installed.
    """
    import httpx  # noqa: PLC0415 - lazy by design

    from . import net  # noqa: PLC0415 - lazy by design

    extract_settings = dataclasses.replace(
        settings,
        connect_timeout=min(settings.connect_timeout, 15),
        request_timeout=min(settings.request_timeout, 30),
    )

    async def fetch(url: str, *, max_bytes: int = VERIFY_MAX_BYTES) -> FetchResult:
        current = url
        for _hop in range(MAX_REDIRECT_HOPS + 1):
            try:
                current = await ensure_public_url(current, settings.allowed_hosts)
            except URLGuardError:
                return FetchResult(ok=False, error="blocked", final_url=current)
            try:
                async with net.make_client(extract_settings) as client:
                    async with client.stream("GET", current) as response:
                        if 300 <= response.status_code < 400 and response.headers.get("location"):
                            current = urljoin(str(response.url), response.headers["location"])
                            continue
                        headers = {key.lower(): value for key, value in response.headers.items()}
                        buffer = bytearray()
                        async for chunk in response.aiter_bytes():
                            buffer.extend(chunk)
                            if len(buffer) > max_bytes:
                                break
                        return FetchResult(
                            ok=True,
                            status=response.status_code,
                            headers=headers,
                            body=bytes(buffer),
                            final_url=str(response.url),
                        )
            except httpx.TimeoutException:
                return FetchResult(ok=False, error="timeout", final_url=current)
            except httpx.HTTPError:
                return FetchResult(ok=False, error="network", final_url=current)
        return FetchResult(ok=False, error="too_many_redirects", final_url=current)

    return fetch
