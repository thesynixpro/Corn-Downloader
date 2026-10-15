import ipaddress
import unittest
from unittest.mock import patch

from app import urlguard
from app.urlguard import URLGuardError


class ParseURLTests(unittest.TestCase):
    def test_accepts_public_https_url(self):
        parsed = urlguard.parse_http_url("https://example.com/video.m3u8?token=abc")
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.host, "example.com")
        self.assertEqual(parsed.query, "token=abc")

    def test_rejects_non_http_scheme(self):
        for url in ("ftp://example.com/a", "file:///etc/passwd", "javascript:alert(1)"):
            with self.assertRaises(URLGuardError) as ctx:
                urlguard.parse_http_url(url)
            self.assertEqual(ctx.exception.reason, "unsafe_scheme")

    def test_rejects_userinfo(self):
        with self.assertRaises(URLGuardError) as ctx:
            urlguard.parse_http_url("https://user:pass@example.com/video")
        self.assertEqual(ctx.exception.reason, "userinfo")

    def test_rejects_empty_and_long_urls(self):
        with self.assertRaises(URLGuardError):
            urlguard.parse_http_url("")
        with self.assertRaises(URLGuardError) as ctx:
            urlguard.parse_http_url("https://example.com/" + "a" * 5000)
        self.assertEqual(ctx.exception.reason, "too_long")

    def test_normalizes_trailing_dot(self):
        parsed = urlguard.parse_http_url("https://Example.COM./video")
        self.assertEqual(parsed.host, "example.com")


class BlockedHostTests(unittest.TestCase):
    def test_blocked_hostnames(self):
        for host in (
            "localhost",
            "LOCALHOST.",
            "metadata.google.internal",
            "metadata",
            "foo.local",
            "bar.internal",
            "x.home.arpa",
            "2130706433",
            "017700000001",
        ):
            self.assertTrue(urlguard.host_is_blocked(host), host)

    def test_allowed_hostnames(self):
        for host in ("example.com", "cdn.example.co.uk", "sub.domain.example.com"):
            self.assertFalse(urlguard.host_is_blocked(host), host)


class IPBlockTests(unittest.TestCase):
    def test_private_and_special_addresses_blocked(self):
        for raw in (
            "127.0.0.1",
            "10.1.2.3",
            "172.16.0.1",
            "192.168.1.10",
            "169.254.169.254",
            "100.64.0.1",
            "0.0.0.0",
            "::1",
            "fe80::1",
            "fc00::1",
            "::ffff:127.0.0.1",
            "fd00:ec2::254",
        ):
            self.assertTrue(urlguard.ip_is_blocked(ipaddress.ip_address(raw)), raw)

    def test_public_addresses_allowed(self):
        for raw in ("8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700::1111"):
            self.assertFalse(urlguard.ip_is_blocked(ipaddress.ip_address(raw)), raw)


class AllowedHostsTests(unittest.TestCase):
    def test_subdomain_matching(self):
        allowed = ("example.com",)
        self.assertTrue(urlguard.host_allowed("example.com", allowed))
        self.assertTrue(urlguard.host_allowed("video.example.com", allowed))
        self.assertFalse(urlguard.host_allowed("badexample.com", allowed))
        self.assertFalse(urlguard.host_allowed("example.com.evil.net", allowed))

    def test_empty_allowlist_allows_everything(self):
        self.assertTrue(urlguard.host_allowed("anything.example", frozenset()))


class EnsurePublicURLTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        urlguard.invalidate_resolution_cache()

    async def test_blocks_private_resolution(self):
        with patch.object(urlguard, "resolve_host_ips", return_value=[ipaddress.ip_address("10.0.0.5")]):
            with self.assertRaises(URLGuardError) as ctx:
                await urlguard.ensure_public_url("http://internal.example/video")
            self.assertEqual(ctx.exception.reason, "blocked_address")

    async def test_blocks_metadata_resolution(self):
        with patch.object(urlguard, "resolve_host_ips", return_value=[ipaddress.ip_address("169.254.169.254")]):
            with self.assertRaises(URLGuardError) as ctx:
                await urlguard.ensure_public_url("http://169.254.169.254/latest/meta-data/")
            self.assertIn(ctx.exception.reason, ("blocked_host", "blocked_address"))

    async def test_allows_public_resolution(self):
        with patch.object(urlguard, "resolve_host_ips", return_value=[ipaddress.ip_address("93.184.216.34")]):
            normalized = await urlguard.ensure_public_url("https://example.com/video.m3u8")
            self.assertEqual(normalized, "https://example.com/video.m3u8")

    async def test_dns_failure_reported(self):
        with patch.object(urlguard, "resolve_host_ips", side_effect=URLGuardError("dns_error", "no")):
            with self.assertRaises(URLGuardError) as ctx:
                await urlguard.ensure_public_url("https://nxdomain.example/video")
            self.assertEqual(ctx.exception.reason, "dns_error")

    async def test_resolve_false_skips_dns(self):
        normalized = await urlguard.ensure_public_url("https://example.com/v", resolve=False)
        self.assertEqual(normalized, "https://example.com/v")

    async def test_allowlist_enforced(self):
        with self.assertRaises(URLGuardError) as ctx:
            await urlguard.ensure_public_url(
                "https://blocked.example/v", ("allowed.example",), resolve=False
            )
        self.assertEqual(ctx.exception.reason, "host_not_allowed")


class FilenameTests(unittest.TestCase):
    def test_strips_directory_traversal(self):
        self.assertEqual(urlguard.safe_filename("../../etc/passwd"), "passwd")

    def test_replaces_unsafe_characters(self):
        self.assertEqual(urlguard.safe_filename("a:b*c?d"), "a_b_c_d")

    def test_drops_path_components(self):
        self.assertEqual(urlguard.safe_filename('a<b>:"c"/d\\e|f?g*h'), "e_f_g_h")

    def test_preserves_extension_and_limits_length(self):
        name = urlguard.safe_filename("x" * 500 + ".mp4")
        self.assertTrue(name.endswith(".mp4"))
        self.assertLessEqual(len(name), 120)

    def test_fallback_for_empty(self):
        self.assertEqual(urlguard.safe_filename(""), "video")
        self.assertEqual(urlguard.safe_filename("..."), "video")


class RedactTests(unittest.TestCase):
    def test_strips_query_and_userinfo(self):
        redacted = urlguard.redact_url("https://user:pass@example.com/path/video.m3u8?token=secret")
        self.assertNotIn("secret", redacted)
        self.assertNotIn("pass", redacted)
        self.assertIn("example.com", redacted)
        self.assertIn("/path/video.m3u8", redacted)


class ExtractURLTests(unittest.TestCase):
    def test_extracts_first_url(self):
        self.assertEqual(
            urlguard.extract_first_url("check this https://example.com/a?b=1 now"),
            "https://example.com/a?b=1",
        )

    def test_strips_trailing_punctuation(self):
        self.assertEqual(
            urlguard.extract_first_url("(see https://example.com/a)."),
            "https://example.com/a",
        )

    def test_returns_none_without_url(self):
        self.assertIsNone(urlguard.extract_first_url("no link here"))


if __name__ == "__main__":
    unittest.main()
