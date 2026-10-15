import unittest

from app import format_select


def fmt(format_id, ext="mp4", size=None, height=None, vcodec="avc1.64001f", acodec="mp4a.40.2"):
    entry = {
        "format_id": format_id,
        "url": f"https://cdn.example.com/{format_id}",
        "ext": ext,
        "vcodec": vcodec,
        "acodec": acodec,
    }
    if size is not None:
        entry["filesize"] = size
    if height is not None:
        entry["height"] = height
    return entry


MB = 1024 * 1024


class HasFormatWithinLimitTests(unittest.TestCase):
    def test_finds_fitting_smallest(self):
        info = {"formats": [fmt("a", size=80 * MB), fmt("b", size=20 * MB), fmt("c", size=200 * MB)]}
        fits, smallest = format_select.has_format_within_limit(info, 50 * MB)
        self.assertTrue(fits)
        self.assertEqual(smallest, 20 * MB)

    def test_reports_not_fitting(self):
        info = {"formats": [fmt("a", size=120 * MB), fmt("b", size=300 * MB)]}
        fits, smallest = format_select.has_format_within_limit(info, 50 * MB)
        self.assertFalse(fits)
        self.assertEqual(smallest, 120 * MB)

    def test_unknown_sizes_allow_attempt(self):
        info = {"formats": [fmt("a")]}
        fits, smallest = format_select.has_format_within_limit(info, 50 * MB)
        self.assertTrue(fits)
        self.assertIsNone(smallest)


class PickFormatTests(unittest.TestCase):
    def test_picks_largest_fitting_combined(self):
        info = {
            "formats": [
                fmt("360", size=10 * MB, height=360),
                fmt("720", size=40 * MB, height=720),
                fmt("1080", size=90 * MB, height=1080),
            ]
        }
        self.assertEqual(format_select.pick_format_string(info, 50 * MB), "720")

    def test_prefers_combined_over_video_only(self):
        info = {
            "formats": [
                fmt("video-only", ext="webm", size=30 * MB, height=720, acodec="none"),
                fmt("combined", size=28 * MB, height=720),
            ]
        }
        self.assertEqual(format_select.pick_format_string(info, 50 * MB), "combined")

    def test_returns_none_when_nothing_fits(self):
        info = {"formats": [fmt("huge", size=900 * MB, height=1080)]}
        self.assertIsNone(format_select.pick_format_string(info, 50 * MB))

    def test_returns_none_without_sizes(self):
        info = {"formats": [fmt("a"), fmt("b")]}
        self.assertIsNone(format_select.pick_format_string(info, 50 * MB))

    def test_respects_height_cap(self):
        info = {
            "formats": [
                fmt("720", size=30 * MB, height=720),
                fmt("1080", size=40 * MB, height=1080),
            ]
        }
        self.assertEqual(format_select.pick_format_string(info, 100 * MB, max_height=720), "720")

    def test_prefers_mp4_on_tie(self):
        info = {
            "formats": [
                fmt("webm-tie", ext="webm", size=20 * MB, height=480),
                fmt("mp4-tie", ext="mp4", size=20 * MB, height=480),
            ]
        }
        self.assertEqual(format_select.pick_format_string(info, 50 * MB), "mp4-tie")


class SelectorTests(unittest.TestCase):
    def test_selector_embeds_cap(self):
        selector = format_select.build_format_selector(50 * MB)
        self.assertIn(str(50 * MB), selector)
        self.assertIn("best[ext=mp4]", selector)


class BestDisplayFormatTests(unittest.TestCase):
    def test_selects_highest(self):
        info = {"formats": [fmt("360", height=360), fmt("720", height=720)]}
        self.assertEqual(format_select.best_display_format(info)["format_id"], "720")

    def test_falls_back_to_info(self):
        info = {"title": "x"}
        self.assertEqual(format_select.best_display_format(info), info)


if __name__ == "__main__":
    unittest.main()
