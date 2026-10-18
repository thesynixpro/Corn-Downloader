import asyncio
import unittest
from unittest.mock import patch

from app import hls_extract
from app.config import Settings
from app.hls_extract import (
    METHOD_BROWSER,
    METHOD_DIRECT,
    METHOD_EMBEDDED,
    METHOD_HTML,
    METHOD_JS,
    METHOD_MASTER,
    STATUS_DENIED,
    STATUS_FOUND,
    STATUS_INVALID,
    STATUS_VERIFIED,
    ExtractionError,
    FetchResult,
    HlsFound,
    extract_hls,
    find_urls_in_text,
    normalize_candidate,
    run_extraction,
)
from app.urlguard import URLGuardError

PAGE_URL = "https://example.com/watch/video123"


async def _fake_ensure(url, allowed_hosts=frozenset(), **kwargs):
    """SSRF guard stand-in: real scheme/host/IP-literal checks, no DNS."""
    import ipaddress

    from app.urlguard import host_is_blocked, parse_http_url

    parsed = parse_http_url(url)
    if host_is_blocked(parsed.host):
        raise URLGuardError("blocked_host", "This host is not reachable from the bot.")
    host = parsed.host
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        ):
            raise URLGuardError("blocked_address", "This address is not reachable from the bot.")
    if allowed_hosts and parsed.host not in allowed_hosts:
        raise URLGuardError("host_not_allowed", "This host is not on the bot's allow-list.")
    return url


def ok(body: str, *, status: int = 200, content_type: str = "text/html", final_url: str = "") -> FetchResult:
    return FetchResult(
        ok=True,
        status=status,
        headers={"content-type": content_type},
        body=body.encode("utf-8"),
        final_url=final_url or PAGE_URL,
    )


MASTER_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
360/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720
720/index.m3u8
"""

MEDIA_PLAYLIST = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-PLAYLIST-TYPE:VOD
#EXTINF:10.0,
seg0.ts
#EXT-X-ENDLIST
"""


class FakeFetch:
    """Callable fetch stub: url -> FetchResult (or callable(max_bytes))."""

    def __init__(self, responses):
        self.responses = dict(responses)
        self.calls = []

    async def __call__(self, url, *, max_bytes=0):
        self.calls.append(url)
        value = self.responses.get(url)
        if value is None:
            return FetchResult(ok=False, status=0, error="network", final_url=url)
        if callable(value):
            return value(max_bytes)
        return value


def make_settings(**overrides) -> Settings:
    base = dict(
        hls_extract_enabled=True,
        hls_extract_timeout=30,
        hls_extract_max_results=8,
        hls_extract_verify_limit=6,
        hls_extract_page_max_mb=5,
        hls_extract_concurrent=2,
        hls_browser_enabled=False,
    )
    base.update(overrides)
    return Settings(**base)


def run(coro):
    return asyncio.run(coro)


@patch("app.hls_extract.ensure_public_url", side_effect=_fake_ensure)
class DirectPlaylistTests(unittest.TestCase):
    """1. A direct, accessible M3U8 playlist URL."""

    def test_direct_media_playlist_verified(self, _guard):
        url = "https://cdn.example.com/video/media.m3u8?token=abc"
        fetch = FakeFetch({url: ok(MEDIA_PLAYLIST, content_type="application/vnd.apple.mpegurl")})
        outcome = run(extract_hls(url, make_settings(), fetch=fetch))
        self.assertEqual(len(outcome.results), 1)
        result = outcome.results[0]
        self.assertEqual(result.url, url)
        self.assertEqual(result.method, METHOD_DIRECT)
        self.assertEqual(result.status, STATUS_VERIFIED)
        self.assertEqual(result.playlist_type, "media")
        self.assertEqual(result.http_status, 200)
        self.assertIn("token=abc", result.url)

    def test_direct_master_playlist_verified_with_children(self, _guard):
        """11. A playlist containing relative variant playlist URLs."""
        url = "https://cdn.example.com/vod/master.m3u8"
        child360 = "https://cdn.example.com/vod/360/index.m3u8"
        child720 = "https://cdn.example.com/vod/720/index.m3u8"
        fetch = FakeFetch(
            {
                url: ok(MASTER_PLAYLIST, content_type="application/vnd.apple.mpegurl"),
                child360: ok(MEDIA_PLAYLIST),
                child720: ok(MEDIA_PLAYLIST),
            }
        )
        outcome = run(extract_hls(url, make_settings(), fetch=fetch))
        by_url = {item.url: item for item in outcome.results}
        self.assertIn(url, by_url)
        self.assertEqual(by_url[url].status, STATUS_VERIFIED)
        self.assertEqual(by_url[url].playlist_type, "master")
        self.assertIn(child360, by_url)
        self.assertIn(child720, by_url)
        self.assertEqual(by_url[child360].method, METHOD_MASTER)
        self.assertEqual(by_url[child360].status, STATUS_VERIFIED)

    def test_direct_url_returning_html_is_not_verified(self, _guard):
        """Do not mark verified just because the filename ends in .m3u8."""
        url = "https://cdn.example.com/fake/master.m3u8"
        fetch = FakeFetch({url: ok("<html>login page</html>")})
        outcome = run(extract_hls(url, make_settings(), fetch=fetch))
        self.assertEqual(len(outcome.results), 1)
        result = outcome.results[0]
        self.assertEqual(result.status, STATUS_FOUND)
        self.assertIn("not with HLS", result.detail)

    def test_query_parameters_preserved_exactly(self, _guard):
        url = "https://cdn.example.com/x.m3u8?a=1&b=2&token=Z%2B%2F%3D"
        fetch = FakeFetch({url: ok(MEDIA_PLAYLIST)})
        outcome = run(extract_hls(url, make_settings(), fetch=fetch))
        self.assertEqual(outcome.results[0].url, url)


@patch("app.hls_extract.ensure_public_url", side_effect=_fake_ensure)
class HtmlSourceTests(unittest.TestCase):
    """2. A webpage containing a publicly visible M3U8 URL in its HTML."""

    def test_source_tag_discovered_and_verified(self, _guard):
        html_page = """
        <html><body>
        <video controls>
          <source src="https://cdn.example.com/videos/clip.m3u8" type="application/x-mpegURL">
        </video>
        </body></html>
        """
        playlist_url = "https://cdn.example.com/videos/clip.m3u8"
        fetch = FakeFetch({PAGE_URL: ok(html_page), playlist_url: ok(MEDIA_PLAYLIST)})
        outcome = run(extract_hls(PAGE_URL, make_settings(), fetch=fetch))
        self.assertEqual(len(outcome.results), 1)
        result = outcome.results[0]
        self.assertEqual(result.url, playlist_url)
        self.assertEqual(result.method, METHOD_HTML)
        self.assertEqual(result.status, STATUS_VERIFIED)

    def test_relative_and_protocol_relative_resolved(self, _guard):
        html_page = """
        <source src="/vod/abs.m3u8">
        <source src="//cdn.other.example.com/proto.m3u8">
        """
        abs_url = "https://example.com/vod/abs.m3u8"
        proto_url = "https://cdn.other.example.com/proto.m3u8"
        fetch = FakeFetch(
            {PAGE_URL: ok(html_page), abs_url: ok(MEDIA_PLAYLIST), proto_url: ok(MEDIA_PLAYLIST)}
        )
        outcome = run(extract_hls(PAGE_URL, make_settings(), fetch=fetch))
        urls = {item.url for item in outcome.results}
        self.assertIn(abs_url, urls)
        self.assertIn(proto_url, urls)

    def test_redirect_uses_final_url_as_base(self, _guard):
        redirected = "https://example.com/new/watch.html"
        html_page = '<source src="stream.m3u8">'
        stream = "https://example.com/new/stream.m3u8"
        fetch = FakeFetch(
            {
                PAGE_URL: FetchResult(
                    ok=True, status=200, headers={"content-type": "text/html"},
                    body=html_page.encode(), final_url=redirected,
                ),
                stream: ok(MEDIA_PLAYLIST),
            }
        )
        outcome = run(extract_hls(PAGE_URL, make_settings(), fetch=fetch))
        urls = {item.url for item in outcome.results}
        self.assertIn(stream, urls)

    def test_base_href_respected(self, _guard):
        html_page = (
            '<html><head><base href="https://cdn.example.com/player/"></head>'
            '<body><video><source src="clip.m3u8"></video></body></html>'
        )
        stream = "https://cdn.example.com/player/clip.m3u8"
        fetch = FakeFetch({PAGE_URL: ok(html_page), stream: ok(MEDIA_PLAYLIST)})
        outcome = run(extract_hls(PAGE_URL, make_settings(), fetch=fetch))
        urls = {item.url for item in outcome.results}
        self.assertIn(stream, urls)


@patch("app.hls_extract.ensure_public_url", side_effect=_fake_ensure)
class EmbeddedPlayerTests(unittest.TestCase):
    """3. A webpage containing an M3U8 URL in player configuration."""

    def test_player_config_script_labelled_embedded(self, _guard):
        html_page = """
        <html><body>
        <script>
          var player = videojs('player');
          player.src({type: "application/x-mpegURL", src: "https://cdn.example.com/hls/master.m3u8"});
        </script>
        </body></html>
        """
        stream = "https://cdn.example.com/hls/master.m3u8"
        fetch = FakeFetch({PAGE_URL: ok(html_page), stream: ok(MEDIA_PLAYLIST)})
        outcome = run(extract_hls(PAGE_URL, make_settings(), fetch=fetch))
        self.assertEqual(len(outcome.results), 1)
        self.assertEqual(outcome.results[0].method, METHOD_EMBEDDED)
        self.assertEqual(outcome.results[0].status, STATUS_VERIFIED)

    def test_player_config_master_expands_children(self, _guard):
        """Same page but the discovered URL is a master: variants appended."""
        html_page = """
        <script>
          var player = videojs('player');
          player.src({type: "application/x-mpegURL", src: "https://cdn.example.com/hls/master.m3u8"});
        </script>
        """
        master = "https://cdn.example.com/hls/master.m3u8"
        child360 = "https://cdn.example.com/hls/360/index.m3u8"
        child720 = "https://cdn.example.com/hls/720/index.m3u8"
        fetch = FakeFetch(
            {
                PAGE_URL: ok(html_page),
                master: ok(MASTER_PLAYLIST),
                child360: ok(MEDIA_PLAYLIST),
                child720: ok(MEDIA_PLAYLIST),
            }
        )
        outcome = run(extract_hls(PAGE_URL, make_settings(), fetch=fetch))
        by_url = {item.url: item for item in outcome.results}
        self.assertEqual(by_url[master].method, METHOD_EMBEDDED)
        self.assertEqual(by_url[master].status, STATUS_VERIFIED)
        self.assertEqual(by_url[child360].method, METHOD_MASTER)
        self.assertEqual(by_url[child360].status, STATUS_VERIFIED)
        self.assertIn(child720, by_url)

    def test_json_ld_labelled_js_config(self, _guard):
        html_page = """
        <script type="application/ld+json">
        {"@type": "VideoObject", "contentUrl": "https://cdn.example.com/ld/clip.m3u8"}
        </script>
        """
        stream = "https://cdn.example.com/ld/clip.m3u8"
        fetch = FakeFetch({PAGE_URL: ok(html_page), stream: ok(MEDIA_PLAYLIST)})
        outcome = run(extract_hls(PAGE_URL, make_settings(), fetch=fetch))
        self.assertEqual(outcome.results[0].method, METHOD_JS)

    def test_escaped_js_url_unescaped(self, _guard):
        html_page = r'<script>var cfg = {"url": "https:\/\/cdn.example.com\/esc\/v.m3u8?token=a&x=1"};</script>'
        stream = "https://cdn.example.com/esc/v.m3u8?token=a&x=1"
        fetch = FakeFetch({PAGE_URL: ok(html_page), stream: ok(MEDIA_PLAYLIST)})
        outcome = run(extract_hls(PAGE_URL, make_settings(), fetch=fetch))
        urls = {item.url for item in outcome.results}
        self.assertIn(stream, urls)


@patch("app.hls_extract.ensure_public_url", side_effect=_fake_ensure)
class MultipleAndDedupTests(unittest.TestCase):
    """5. Multiple distinct HLS URLs. 6. Duplicate HLS URLs."""

    def test_multiple_distinct_urls_all_returned(self, _guard):
        html_page = """
        <source src="https://cdn.example.com/a.m3u8">
        <source src="https://cdn.example.com/b.m3u8">
        <source src="https://cdn.example.com/a.m3u8?token=different">
        """
        urls = [
            "https://cdn.example.com/a.m3u8",
            "https://cdn.example.com/b.m3u8",
            "https://cdn.example.com/a.m3u8?token=different",
        ]
        fetch = FakeFetch({PAGE_URL: ok(html_page), **{u: ok(MEDIA_PLAYLIST) for u in urls}})
        outcome = run(extract_hls(PAGE_URL, make_settings(), fetch=fetch))
        found = {item.url for item in outcome.results}
        self.assertEqual(found, set(urls))

    def test_duplicates_collapse_to_one(self, _guard):
        html_page = """
        <source src="https://cdn.example.com/one.m3u8">
        <script>var p = "https://cdn.example.com/one.m3u8";</script>
        <a href="https://cdn.example.com/one.m3u8">link</a>
        """
        stream = "https://cdn.example.com/one.m3u8"
        fetch = FakeFetch({PAGE_URL: ok(html_page), stream: ok(MEDIA_PLAYLIST)})
        outcome = run(extract_hls(PAGE_URL, make_settings(), fetch=fetch))
        self.assertEqual(len(outcome.results), 1)

    def test_distinct_query_params_not_merged(self, _guard):
        a = "https://cdn.example.com/v.m3u8?token=aaa"
        b = "https://cdn.example.com/v.m3u8?token=bbb"
        html_page = f'<source src="{a}"><source src="{b}">'
        fetch = FakeFetch({PAGE_URL: ok(html_page), a: ok(MEDIA_PLAYLIST), b: ok(MEDIA_PLAYLIST)})
        outcome = run(extract_hls(PAGE_URL, make_settings(), fetch=fetch))
        self.assertEqual({item.url for item in outcome.results}, {a, b})


@patch("app.hls_extract.ensure_public_url", side_effect=_fake_ensure)
class InvalidInputTests(unittest.TestCase):
    """7. An invalid or unsupported input URL. 15. Localhost / private address."""

    def test_unsupported_scheme_rejected(self, _guard):
        with self.assertRaises(ExtractionError) as ctx:
            run(extract_hls("ftp://example.com/file", make_settings(), fetch=FakeFetch({})))
        self.assertEqual(ctx.exception.kind, "invalid")

    def test_javascript_scheme_rejected(self, _guard):
        with self.assertRaises(ExtractionError):
            run(extract_hls("javascript:alert(1)", make_settings(), fetch=FakeFetch({})))

    def test_localhost_input_rejected(self, _guard):
        with self.assertRaises(ExtractionError) as ctx:
            run(extract_hls("http://localhost:8080/x.m3u8", make_settings(), fetch=FakeFetch({})))
        self.assertEqual(ctx.exception.kind, "blocked")

    def test_private_ip_candidate_dropped(self, _guard):
        html_page = '<source src="http://127.0.0.1/secret.m3u8"><source src="https://cdn.example.com/ok.m3u8">'
        ok_url = "https://cdn.example.com/ok.m3u8"
        fetch = FakeFetch({PAGE_URL: ok(html_page), ok_url: ok(MEDIA_PLAYLIST)})
        outcome = run(extract_hls(PAGE_URL, make_settings(), fetch=fetch))
        self.assertEqual([item.url for item in outcome.results], [ok_url])


@patch("app.hls_extract.ensure_public_url", side_effect=_fake_ensure)
class NetworkFailureTests(unittest.TestCase):
    """8. DNS failure / timeout. 9. HTTP 403 and 404. Page-level errors."""

    def test_page_timeout_maps_to_timeout_error(self, _guard):
        fetch = FakeFetch({PAGE_URL: FetchResult(ok=False, error="timeout", final_url=PAGE_URL)})
        with self.assertRaises(ExtractionError) as ctx:
            run(extract_hls(PAGE_URL, make_settings(), fetch=fetch))
        self.assertEqual(ctx.exception.kind, "timeout")

    def test_page_dns_failure_maps_to_network_error(self, _guard):
        fetch = FakeFetch({PAGE_URL: FetchResult(ok=False, error="dns", final_url=PAGE_URL)})
        with self.assertRaises(ExtractionError) as ctx:
            run(extract_hls(PAGE_URL, make_settings(), fetch=fetch))
        self.assertEqual(ctx.exception.kind, "network")

    def test_page_403_maps_to_forbidden(self, _guard):
        fetch = FakeFetch({PAGE_URL: ok("nope", status=403)})
        with self.assertRaises(ExtractionError) as ctx:
            run(extract_hls(PAGE_URL, make_settings(), fetch=fetch))
        self.assertEqual(ctx.exception.kind, "forbidden")
        self.assertIn("403", ctx.exception.message)

    def test_page_404_maps_to_not_found(self, _guard):
        fetch = FakeFetch({PAGE_URL: ok("gone", status=404)})
        with self.assertRaises(ExtractionError) as ctx:
            run(extract_hls(PAGE_URL, make_settings(), fetch=fetch))
        self.assertEqual(ctx.exception.kind, "not_found")

    def test_candidate_403_verification_access_denied(self, _guard):
        """A URL found in HTML that 403s is ACCESS_DENIED, not expired."""
        html_page = '<source src="https://cdn.example.com/locked.m3u8">'
        locked = "https://cdn.example.com/locked.m3u8"
        fetch = FakeFetch({PAGE_URL: ok(html_page), locked: ok("no", status=403)})
        outcome = run(extract_hls(PAGE_URL, make_settings(), fetch=fetch))
        self.assertEqual(outcome.results[0].status, STATUS_DENIED)
        self.assertEqual(outcome.results[0].http_status, 403)

    def test_candidate_404_verification_invalid(self, _guard):
        """12. A malformed or expired playlist URL."""
        html_page = '<source src="https://cdn.example.com/expired.m3u8">'
        expired = "https://cdn.example.com/expired.m3u8"
        fetch = FakeFetch({PAGE_URL: ok(html_page), expired: ok("gone", status=404)})
        outcome = run(extract_hls(PAGE_URL, make_settings(), fetch=fetch))
        self.assertEqual(outcome.results[0].status, STATUS_INVALID)

    def test_page_429_rate_limited(self, _guard):
        fetch = FakeFetch({PAGE_URL: ok("slow down", status=429)})
        with self.assertRaises(ExtractionError) as ctx:
            run(extract_hls(PAGE_URL, make_settings(), fetch=fetch))
        self.assertEqual(ctx.exception.kind, "rate_limited")


@patch("app.hls_extract.ensure_public_url", side_effect=_fake_ensure)
class NoResultsTests(unittest.TestCase):
    """10. A webpage with no detectable HLS stream."""

    def test_plain_page_reports_no_results(self, _guard):
        html_page = "<html><body><p>Just text, no video.</p></body></html>"
        fetch = FakeFetch({PAGE_URL: ok(html_page)})
        outcome = run(extract_hls(PAGE_URL, make_settings(), fetch=fetch))
        self.assertEqual(outcome.results, [])
        self.assertEqual(outcome.error_kind, "no_results")
        self.assertIn("No detectable HLS", outcome.error)

    def test_non_playlist_200_candidate_not_verified(self, _guard):
        html_page = '<source src="https://cdn.example.com/not.m3u8">'
        target = "https://cdn.example.com/not.m3u8"
        fetch = FakeFetch({PAGE_URL: ok(html_page), target: ok("<html>404-ish page</html>")})
        outcome = run(extract_hls(PAGE_URL, make_settings(), fetch=fetch))
        self.assertEqual(outcome.results[0].status, STATUS_FOUND)
        self.assertIn("not with HLS", outcome.results[0].detail)


@patch("app.hls_extract.ensure_public_url", side_effect=_fake_ensure)
class CancellationTests(unittest.TestCase):
    """13. A user cancelling an extraction."""

    def test_cancel_propagates(self, _guard):
        async def slow_fetch(url, *, max_bytes=0):
            await asyncio.sleep(5)
            return ok("<html></html>")

        async def scenario():
            task = asyncio.create_task(
                extract_hls(PAGE_URL, make_settings(), fetch=slow_fetch)
            )
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        run(scenario())


class BrowserTests(unittest.TestCase):
    """4/14. Browser extraction unavailable; browser URLs labelled correctly."""

    @patch("app.hls_extract.ensure_public_url", side_effect=_fake_ensure)
    def test_browser_urls_labelled_and_merged(self, _guard):
        html_page = "<html><body>JS-rendered, nothing in source.</body></html>"
        browser_url = "https://cdn.example.com/js/late.m3u8"
        fetch = FakeFetch({PAGE_URL: ok(html_page), browser_url: ok(MEDIA_PLAYLIST)})
        outcome = run(
            extract_hls(
                PAGE_URL, make_settings(), fetch=fetch, browser_urls=[browser_url]
            )
        )
        self.assertEqual(len(outcome.results), 1)
        self.assertEqual(outcome.results[0].method, METHOD_BROWSER)
        self.assertEqual(outcome.results[0].status, STATUS_VERIFIED)

    @patch("app.browser_extract.browser_available", return_value=False)
    @patch("app.hls_extract.ensure_public_url", side_effect=_fake_ensure)
    def test_run_extraction_notes_browser_unavailable(self, _guard, _available):
        html_page = "<html><body>JS-rendered only.</body></html>"
        fetch = FakeFetch({PAGE_URL: ok(html_page)})
        outcome = run(
            run_extraction(PAGE_URL, make_settings(hls_browser_enabled=True), fetch=fetch)
        )
        self.assertEqual(outcome.results, [])
        self.assertIn("unavailable", outcome.browser_note.lower())

    @patch("app.browser_extract.browser_available", return_value=True)
    @patch("app.browser_extract.collect_hls_requests")
    @patch("app.hls_extract.ensure_public_url", side_effect=_fake_ensure)
    def test_run_extraction_merges_browser_results(self, _guard, collect, _available):
        async def fake_collect(url, **kwargs):
            return ["https://cdn.example.com/browser/only.m3u8"]

        collect.side_effect = fake_collect
        html_page = "<html><body>nothing server-side</body></html>"
        browser_url = "https://cdn.example.com/browser/only.m3u8"
        fetch = FakeFetch({PAGE_URL: ok(html_page), browser_url: ok(MEDIA_PLAYLIST)})
        outcome = run(
            run_extraction(PAGE_URL, make_settings(hls_browser_enabled=True), fetch=fetch)
        )
        self.assertEqual(len(outcome.results), 1)
        self.assertEqual(outcome.results[0].method, METHOD_BROWSER)


@patch("app.hls_extract.ensure_public_url", side_effect=_fake_ensure)
class LimitsAndPhasesTests(unittest.TestCase):
    """Resource limits, phase callbacks, and verification budgets."""

    def test_max_results_cap(self, _guard):
        sources = "\n".join(
            f'<source src="https://cdn.example.com/{i}.m3u8">' for i in range(15)
        )
        urls = {f"https://cdn.example.com/{i}.m3u8": ok(MEDIA_PLAYLIST) for i in range(15)}
        fetch = FakeFetch({PAGE_URL: ok(f"<html>{sources}</html>"), **urls})
        outcome = run(
            extract_hls(PAGE_URL, make_settings(hls_extract_max_results=3), fetch=fetch)
        )
        self.assertLessEqual(len(outcome.results), 3)

    def test_verify_limit_zero_skips_verification(self, _guard):
        html_page = '<source src="https://cdn.example.com/unverified.m3u8">'
        stream = "https://cdn.example.com/unverified.m3u8"
        fetch = FakeFetch({PAGE_URL: ok(html_page), stream: ok(MEDIA_PLAYLIST)})
        outcome = run(
            extract_hls(PAGE_URL, make_settings(hls_extract_verify_limit=0), fetch=fetch)
        )
        self.assertEqual(outcome.results[0].status, STATUS_FOUND)
        # The candidate itself must never have been fetched for verification.
        self.assertEqual(fetch.calls, [PAGE_URL])

    def test_phase_callbacks_in_order(self, _guard):
        html_page = '<source src="https://cdn.example.com/p.m3u8">'
        stream = "https://cdn.example.com/p.m3u8"
        fetch = FakeFetch({PAGE_URL: ok(html_page), stream: ok(MEDIA_PLAYLIST)})
        phases = []

        async def phase(name):
            phases.append(name)

        run(extract_hls(PAGE_URL, make_settings(), fetch=fetch, phase_cb=phase))
        self.assertEqual(phases, ["scanning", "analyzing", "searching", "verifying"])

    def test_direct_playlist_phase_inspecting(self, _guard):
        url = "https://cdn.example.com/direct.m3u8"
        fetch = FakeFetch({url: ok(MEDIA_PLAYLIST)})
        phases = []

        async def phase(name):
            phases.append(name)

        run(extract_hls(url, make_settings(), fetch=fetch, phase_cb=phase))
        self.assertEqual(phases, ["verifying"])

    def test_drm_markers_noted(self, _guard):
        url = "https://cdn.example.com/drm.m3u8"
        drm_body = MEDIA_PLAYLIST + '\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key.bin"\n'
        fetch = FakeFetch({url: ok(drm_body)})
        outcome = run(extract_hls(url, make_settings(), fetch=fetch))
        self.assertIn("DRM", outcome.results[0].detail)


class NormalizeTests(unittest.TestCase):
    def test_normalize_preserves_query(self):
        url = normalize_candidate(
            "https://cdn.example.com/v.m3u8?a=1&b=2", "https://example.com/"
        )
        self.assertEqual(url, "https://cdn.example.com/v.m3u8?a=1&b=2")

    def test_normalize_rejects_non_http(self):
        self.assertIsNone(normalize_candidate("ftp://x/y.m3u8", "https://example.com"))
        self.assertIsNone(normalize_candidate("javascript:a.m3u8", "https://example.com"))

    def test_normalize_rejects_userinfo(self):
        self.assertIsNone(
            normalize_candidate("https://user:pass@cdn.example.com/v.m3u8", "https://example.com")
        )

    def test_find_urls_handles_escapes_and_relatives(self):
        text = r'cfg "https:\/\/cdn.example.com\/a.m3u8?x=1" and /rel/b.m3u8 and //cdn.example.com/c.m3u8'
        urls = find_urls_in_text(text, "https://example.com/page")
        self.assertIn("https://cdn.example.com/a.m3u8?x=1", urls)
        self.assertIn("https://example.com/rel/b.m3u8", urls)
        self.assertIn("https://cdn.example.com/c.m3u8", urls)


if __name__ == "__main__":
    unittest.main()
