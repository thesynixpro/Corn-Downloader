import unittest

from app import platforms


class DetectPlatformTests(unittest.TestCase):
    def test_youtube_variants(self):
        for url in (
            "https://www.youtube.com/watch?v=abc",
            "https://m.youtube.com/watch?v=abc",
            "https://youtu.be/abc",
        ):
            self.assertEqual(platforms.detect_platform(url).key, "youtube")

    def test_x_and_twitter(self):
        self.assertEqual(platforms.detect_platform("https://x.com/u/status/1").key, "twitter")
        self.assertEqual(platforms.detect_platform("https://twitter.com/u/status/1").key, "twitter")

    def test_adult_platforms(self):
        self.assertEqual(platforms.detect_platform("https://www.pornhub.com/view_video.php?viewkey=x").key, "pornhub")
        self.assertEqual(platforms.detect_platform("https://xhamster.com/videos/x").key, "xhamster")

    def test_unknown_host_is_generic(self):
        self.assertEqual(platforms.detect_platform("https://random-site.example/video").key, "generic")

    def test_does_not_match_suffix_confusion(self):
        self.assertEqual(platforms.detect_platform("https://notyoutube.com/watch").key, "generic")


class RoutingHintTests(unittest.TestCase):
    def test_hls_detection(self):
        self.assertTrue(platforms.is_probable_hls("https://cdn.example.com/hls/master.m3u8?token=1"))
        self.assertTrue(platforms.is_probable_hls("https://cdn.example.com/hls/master.m3u"))
        self.assertFalse(platforms.is_probable_hls("https://example.com/watch?v=1"))

    def test_direct_media_detection(self):
        for url in (
            "https://cdn.example.com/a/video.mp4",
            "https://cdn.example.com/a/video.webm?x=1",
            "https://cdn.example.com/a/video.mkv",
        ):
            self.assertTrue(platforms.is_probable_direct_media(url), url)
        self.assertFalse(platforms.is_probable_direct_media("https://example.com/page"))


class CanonicalizeTests(unittest.TestCase):
    def test_xhamster_mirror_rewritten(self):
        self.assertEqual(
            platforms.canonicalize_url("https://xhamster46.desi/videos/abc"),
            "https://xhamster.com/videos/abc",
        )
        self.assertEqual(
            platforms.canonicalize_url("https://www.xhamster2.com/videos/abc"),
            "https://xhamster.com/videos/abc",
        )

    def test_canonical_host_unchanged(self):
        url = "https://xhamster.com/videos/abc"
        self.assertEqual(platforms.canonicalize_url(url), url)

    def test_unrelated_host_unchanged(self):
        url = "https://example.com/videos/abc"
        self.assertEqual(platforms.canonicalize_url(url), url)


if __name__ == "__main__":
    unittest.main()
