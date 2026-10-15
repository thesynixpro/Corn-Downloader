import unittest

from app import errors, formatting
from app.progress import Progress, RateMeter


class MapDownloadErrorTests(unittest.TestCase):
    def test_login_required_is_explained_not_bypassed(self):
        message = errors.map_download_error(Exception("Sign in to confirm you're not a bot"))
        self.assertIn("login", message.lower())

    def test_drm_is_refused(self):
        message = errors.map_download_error(Exception("This stream is protected by Widevine DRM"))
        self.assertIn("drm", message.lower())

    def test_auth_detection(self):
        self.assertTrue(errors.is_auth_required(Exception("Private video, login required")))
        self.assertTrue(errors.is_auth_required(Exception("cookies are needed")))
        self.assertFalse(errors.is_auth_required(Exception("HTTP 404")))

    def test_unsupported_url(self):
        message = errors.map_download_error(Exception("Unsupported URL: https://unknown.example"))
        self.assertIn("not currently supported", message)

    def test_403_mentions_blocking(self):
        message = errors.map_download_error(Exception("HTTP Error 403: Forbidden"))
        self.assertIn("403", message)

    def test_timeout(self):
        message = errors.map_download_error(Exception("Read timed out"))
        self.assertIn("timed out", message.lower())

    def test_expired_playlist(self):
        message = errors.map_download_error(Exception("HTTP 410 Gone: playlist expired"))
        self.assertIn("expired", message.lower())

    def test_generic_fallback(self):
        message = errors.map_download_error(Exception("something odd"))
        self.assertIn("Unable to download", message)


class ShortDetailTests(unittest.TestCase):
    def test_truncates(self):
        detail = errors.short_detail(Exception("x" * 500))
        self.assertLessEqual(len(detail), 160)

    def test_strips_userinfo(self):
        detail = errors.short_detail(Exception("failed to fetch https://user:secret@cdn.example/a"))
        self.assertNotIn("secret", detail)

    def test_uses_last_line(self):
        detail = errors.short_detail(Exception("ERROR: line one\nERROR: real reason here"))
        self.assertEqual(detail, "ERROR: real reason here")


class FormattingTests(unittest.TestCase):
    def test_format_bytes(self):
        self.assertEqual(formatting.format_bytes(0), "0.0 B")
        self.assertEqual(formatting.format_bytes(1536), "1.5 KB")
        self.assertEqual(formatting.format_bytes(5 * 1024 * 1024), "5.0 MB")
        self.assertEqual(formatting.format_bytes(None), "unknown size")

    def test_format_duration(self):
        self.assertEqual(formatting.format_duration(59), "0:59")
        self.assertEqual(formatting.format_duration(61), "1:01")
        self.assertEqual(formatting.format_duration(3661), "1:01:01")
        self.assertEqual(formatting.format_duration(None), "unknown")

    def test_format_speed(self):
        self.assertEqual(formatting.format_speed(1024), "1.0 KB/s")
        self.assertEqual(formatting.format_speed(0), "unknown")

    def test_render_downloading_progress(self):
        progress = Progress(stage="downloading", downloaded=50, total=100, speed=1024, eta=50)
        text = formatting.render_progress(progress)
        self.assertIn("50%", text)
        self.assertIn("50.0 B / 100.0 B", text)
        self.assertIn("1.0 KB/s", text)

    def test_render_queued_with_position(self):
        progress = Progress(stage="queued")
        self.assertIn("position 2", formatting.render_progress(progress, 2))

    def test_render_unknown_stage_is_none(self):
        self.assertIsNone(formatting.render_progress(Progress(stage="wat")))


class RateMeterTests(unittest.TestCase):
    def test_speed_and_eta(self):
        now = [0.0]
        meter = RateMeter(window=10.0, clock=lambda: now[0])
        meter.update(0, 100)
        now[0] = 1.0
        meter.update(10, 100)
        speed, eta = meter.update(20, 100)
        self.assertAlmostEqual(speed, 20.0)
        self.assertAlmostEqual(eta, 4.0)

    def test_no_eta_without_total(self):
        now = [0.0]
        meter = RateMeter(clock=lambda: now[0])
        meter.update(0, 0)
        now[0] = 1.0
        speed, eta = meter.update(5, 0)
        self.assertAlmostEqual(speed, 5.0)
        self.assertIsNone(eta)

    def test_single_sample_has_no_speed(self):
        meter = RateMeter(clock=lambda: 0.0)
        speed, eta = meter.update(10, 100)
        self.assertEqual(speed, 0.0)
        self.assertIsNone(eta)


if __name__ == "__main__":
    unittest.main()
