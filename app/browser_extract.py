"""Optional Playwright-based browser extraction fallback.

Some pages load their video players and HLS playlist URLs dynamically through
JavaScript, so the URLs never appear in the initial HTML. When enabled
(``HLS_BROWSER_ENABLED=1``) *and* Playwright plus a Chromium build are
installed, this module loads the public page in headless Chromium and
observes network responses for ``.m3u8`` requests.

It is a best-effort fallback, never a requirement: the HTTP extraction engine
(:mod:`app.hls_extract`) works without it, and the bot degrades gracefully
with an honest "unavailable" note when the runtime is missing.

Hard limits:
* only the user-submitted public URL is navigated to (already SSRF-validated
  by the caller),
* every observed response URL is SSRF-validated before it is returned,
* strict navigation/network timeouts and a hard wall-clock cap,
* the browser context and process are closed after every operation,
* no attempt is made to circumvent authentication, CAPTCHA, anti-bot
  challenges, DRM, or any other access control.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from .urlguard import URLGuardError, ensure_public_url

logger = logging.getLogger("uvd_bot.browser")

DEFAULT_NAV_TIMEOUT_MS = 20_000
POST_LOAD_SETTLE_MS = 1_500
HARD_CAP_S = 45.0


def browser_available() -> bool:
    """True when the playwright package can be imported.

    The presence of a Chromium binary is checked at launch time; a missing
    binary surfaces as a launch error handled by the caller.
    """
    try:
        import playwright.async_api  # noqa: F401
    except Exception:  # noqa: BLE001 - any import problem means unavailable
        return False
    return True


async def collect_hls_requests(
    url: str,
    *,
    allowed_hosts: frozenset[str] | set[str] | tuple[str, ...] = frozenset(),
    timeout_s: float = 25.0,
    max_urls: int = 20,
) -> list[str]:
    """Load ``url`` in headless Chromium and return observed HLS URLs.

    Returns an empty list when Playwright/Chromium is unavailable or the page
    load fails; the caller reports that honestly. Every returned URL has
    passed the shared SSRF guard.
    """
    if not browser_available():
        return []
    return await asyncio.wait_for(
        _observe(url, allowed_hosts=allowed_hosts, timeout_s=timeout_s, max_urls=max_urls),
        timeout=HARD_CAP_S,
    )


async def _observe(
    url: str,
    *,
    allowed_hosts: frozenset[str] | set[str] | tuple[str, ...],
    timeout_s: float,
    max_urls: int,
) -> list[str]:
    from playwright.async_api import async_playwright

    nav_timeout = int(max(5.0, min(float(timeout_s), 30.0)) * 1000)
    raw_seen: list[str] = []
    collected: list[str] = []

    async with async_playwright() as playwright:
        browser = await asyncio.wait_for(
            playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            ),
            timeout=15.0,
        )
        try:
            context = await browser.new_context(
                java_script_enabled=True,
                ignore_https_errors=False,
            )
            page = await context.new_page()

            def on_response(response) -> None:
                try:
                    href = response.url
                except Exception:  # noqa: BLE001 - detached/aborted responses
                    return
                if ".m3u8" not in href.lower():
                    return
                if href in raw_seen or len(raw_seen) >= max_urls * 2:
                    return
                raw_seen.append(href)

            page.on("response", on_response)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout)
            except Exception as exc:  # noqa: BLE001 - navigation errors are best-effort
                logger.info("Browser navigation failed: %s", type(exc).__name__)
                return []
            # Give late XHR/fetch players a short window to request playlists.
            try:
                await page.wait_for_timeout(POST_LOAD_SETTLE_MS)
            except Exception:  # noqa: BLE001
                pass
            await context.close()
        finally:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001 - browser may already be gone
                pass

    for href in raw_seen:
        if len(collected) >= max_urls:
            break
        try:
            collected.append(await ensure_public_url(href, allowed_hosts))
        except URLGuardError:
            logger.info("Dropped browser-observed URL (host blocked).")
            continue
    return collected


def summarize(urls: Iterable[str]) -> str:
    """Short log-safe summary of observed URLs (query strings redacted)."""
    from .urlguard import redact_url

    return ", ".join(redact_url(url) for url in urls)
