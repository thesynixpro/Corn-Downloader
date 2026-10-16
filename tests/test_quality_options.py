import unittest

from app.format_select import build_quality_options, selector_for_height
from app.selections import QualitySelection, effective_height_cap


def entry(format_id, height, size, ext="mp4", width=None):
    return {
        "format_id": format_id,
        "url": f"https://cdn.example/{format_id}",
        "ext": ext,
        "height": height,
        "width": width or (height * 16 // 9 if height else None),
        "vcodec": "avc1",
        "acodec": "mp4a",
        "filesize": size,
    }


class QualityOptionTests(unittest.TestCase):
    def test_builds_only_genuine_fitting_options(self):
        info = {
            "formats": [
                entry("low", 360, 10),
                entry("mid", 720, 40),
                entry("high", 1080, 90),
                entry("silent", 720, 20, ext="mp4") | {"acodec": "none"},
                entry("unknown-size", 480, 0),
            ]
        }
        options = build_quality_options(info, cap_bytes=50, max_height=1080)
        self.assertEqual([option.target_height for option in options], [720, 360])
        self.assertEqual(options[0].format_id, "mid")

    def test_selector_honours_explicit_height(self):
        info = {"formats": [entry("low", 360, 10), entry("mid", 720, 40), entry("high", 1080, 90)]}
        self.assertEqual(selector_for_height(info, 100, 720), "mid")
        self.assertIsNone(selector_for_height(info, 100, 240))

    def test_effective_cap_combines_settings_defaults_and_choice(self):
        selection = QualitySelection(mode="height", target_height=1080)
        self.assertEqual(effective_height_cap(2160, "720", selection), 720)
        self.assertEqual(
            effective_height_cap(2160, "auto", QualitySelection()), 2160
        )
        self.assertEqual(effective_height_cap(0, "auto", QualitySelection()), 0)


if __name__ == "__main__":
    unittest.main()
