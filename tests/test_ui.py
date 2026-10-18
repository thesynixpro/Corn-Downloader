import unittest

from app import callbacks, ui
from app.hls_extract import (
    METHOD_BROWSER,
    METHOD_HTML,
    STATUS_DENIED,
    STATUS_FOUND,
    STATUS_VERIFIED,
    HlsFound,
)
from app.progress import Progress


class ProgressBarTests(unittest.TestCase):
    def test_half_progress(self):
        self.assertEqual(ui.progress_bar(50, 100, width=10), "▰▰▰▰▰▱▱▱▱▱ 50%")

    def test_clamps_overflow(self):
        self.assertTrue(ui.progress_bar(150, 100, width=4).endswith("100%"))

    def test_unknown_total_returns_none(self):
        self.assertIsNone(ui.progress_bar(10, 0))
        self.assertIsNone(ui.progress_bar(-1, 100))


class CardTests(unittest.TestCase):
    def test_welcome_card_has_working_menu_payloads(self):
        card = ui.welcome_card("Ada", 700)
        self.assertIn("CORN DOWNLOADER", card.text)
        self.assertIn("700", card.text)
        self.assertEqual(card.parse_mode, "HTML")
        payloads = [button.data for row in card.buttons for button in row]
        self.assertTrue(payloads)
        for payload in payloads:
            action, parts = callbacks.decode(payload)
            self.assertEqual(action, "menu")
            self.assertEqual(len(parts), 1)

    def test_detection_card_encodes_compact_safe_payloads(self):
        card = ui.detection_card(
            kind="ytdlp",
            platform_label="YouTube",
            title="Example <video>",
            duration_text="1:00",
            quality_text="Auto + 720p",
            choices=(ui.Choice(key="0", label="Auto"), ui.Choice(key="1", label="720p")),
            session_id="abc123def456",
        )
        self.assertIn("Example &lt;video&gt;", card.text)
        self.assertNotIn("<video>", card.text)
        action, parts = callbacks.decode(card.buttons[0][1].data)
        self.assertEqual((action, parts), ("start", ("abc123def456", "1")))
        cancel_action, _ = callbacks.decode(card.buttons[-1][0].data)
        self.assertEqual(cancel_action, "flow_cancel")

    def test_status_card_shows_accurate_progress_and_cancel(self):
        progress = Progress(stage="downloading", downloaded=50, total=100, speed=1024, eta=50)
        card = ui.status_card(
            title="Demo",
            label="YouTube",
            phase=progress.stage,
            downloaded=progress.downloaded,
            total=progress.total,
            speed=progress.speed,
            eta=progress.eta,
        )
        self.assertIn("▰▰▰▰▰▰▱▱▱▱▱▱ 50%", card.text)
        self.assertIn("50.0 B / 100.0 B", card.text)
        self.assertEqual(card.buttons[0][0].data, "v1:job_cancel")
        self.assertEqual(card.buttons[0][0].label, "🛑 Cancel Download")

    def test_status_card_handles_unknown_progress_honestly(self):
        card = ui.status_card(
            title="Demo",
            label="HLS stream",
            phase="downloading",
            downloaded=123,
            total=0,
            detail="12/40 segments",
        )
        self.assertIn("12/40 segments", card.text)
        self.assertNotIn("%", card.text.split("Progress:")[1].split("\n")[0])

    def test_error_card_offers_retry_and_platforms(self):
        card = ui.error_card(
            kind="invalid",
            message="Please send a valid public video URL.",
            retry=True,
            include_platforms=True,
        )
        payloads = [button.data for row in card.buttons for button in row]
        actions = [callbacks.decode(payload)[0] for payload in payloads]
        self.assertIn("flow_retry", actions)
        self.assertIn("menu", actions)

    def test_completion_card_reports_split_parts(self):
        card = ui.completion_card(
            file_name="video.mp4",
            size_bytes=60 * 1024 * 1024,
            quality="720p",
            mode="split",
            parts=3,
            support_enabled=False,
        )
        self.assertIn("Split delivery (3 parts)", card.text)
        self.assertIn("60.0 MB", card.text)


def _fake_result(url="https://cdn.example.com/v.m3u8", *, status=STATUS_VERIFIED, method=METHOD_HTML, **kwargs) -> HlsFound:
    defaults = dict(playlist_type="media", http_status=200, detail="")
    defaults.update(kwargs)
    return HlsFound(url=url, method=method, status=status, **defaults)


class HlsCardTests(unittest.TestCase):
    def test_prompt_card_offers_main_menu(self):
        card = ui.hls_prompt_card()
        self.assertIn("HLS LINK EXTRACTOR", card.text)
        self.assertIn("/hls", card.text)
        payload = card.buttons[0][0].data
        action, parts = callbacks.decode(payload)
        self.assertEqual((action, parts), ("menu", ("home",)))

    def test_progress_card_uses_known_phase_labels(self):
        card = ui.hls_progress_card("verifying")
        self.assertIn("VERIFYING PLAYLISTS", card.text)
        unknown = ui.hls_progress_card("unknown-phase")
        self.assertIn("WORKING", unknown.text)

    def test_results_card_labels_and_url_buttons(self):
        results = (
            _fake_result("https://cdn.example.com/a.m3u8", status=STATUS_VERIFIED),
            _fake_result("https://cdn.example.com/b.m3u8", status=STATUS_FOUND, http_status=0),
            _fake_result("https://cdn.example.com/c.m3u8", status=STATUS_DENIED, http_status=403),
        )
        card = ui.hls_results_card("https://example.com/watch/1", results, 0, "sess1")
        self.assertIn("3 found, 1 verified", card.text)
        self.assertIn("✅ Verified playlist", card.text)
        self.assertIn("🔍 URL Found", card.text)
        self.assertIn("🚫 Access Denied", card.text)
        self.assertIn("HTTP status: <b>403</b>", card.text)
        url_buttons = [
            button for row in card.buttons for button in row if button.url
        ]
        self.assertEqual(len(url_buttons), 3)
        self.assertEqual(url_buttons[0].url, "https://cdn.example.com/a.m3u8")
        self.assertTrue(all(button.url.startswith("https://") for button in url_buttons))
        payload = card.buttons[0][1].data
        action, parts = callbacks.decode(payload)
        self.assertEqual((action, parts), ("hls_view", ("sess1", "0")))

    def test_results_card_paginates_and_numbers_from_one(self):
        results = tuple(_fake_result(f"https://cdn.example.com/{i}.m3u8") for i in range(5))
        page0 = ui.hls_results_card("https://example.com/", results, 0, "sess2")
        self.assertIn("<b>Result 1</b>", page0.text)
        self.assertIn("<b>Result 3</b>", page0.text)
        self.assertNotIn("<b>Result 4</b>", page0.text)
        page1 = ui.hls_results_card("https://example.com/", results, 1, "sess2")
        self.assertIn("<b>Result 4</b>", page1.text)
        self.assertIn("<b>Result 5</b>", page1.text)
        nav_buttons = [
            button
            for row in page1.buttons
            for button in row
            if not button.url and button.label.startswith(("Page", "Next", "⬅️"))
        ]
        self.assertTrue(any(button.label == "Page 2/2" for button in nav_buttons))

    def test_results_card_page_out_of_range_clamped(self):
        results = (_fake_result(),)
        card = ui.hls_results_card("https://example.com/", results, 99, "sess3")
        self.assertIn("<b>Result 1</b>", card.text)

    def test_results_card_escapes_hostile_url(self):
        results = (
            _fake_result('https://cdn.example.com/<script>alert(1)</script>.m3u8'),
        )
        card = ui.hls_results_card("https://example.com/", results, 0, "sess4")
        self.assertNotIn("<script>alert(1)</script>", card.text)

    def test_error_card_kinds_and_actions(self):
        card = ui.hls_error_card("forbidden", "The page returned 403.")
        self.assertIn("ACCESS DENIED", card.text)
        self.assertIn("403", card.text)
        actions = [
            callbacks.decode(button.data)[0]
            for row in card.buttons
            for button in row
            if button.data
        ]
        self.assertIn("hls_again", actions)
        self.assertIn("menu", actions)

    def test_error_card_unknown_kind_falls_back(self):
        card = ui.hls_error_card("weird", "Something happened.")
        self.assertIn("EXTRACTION FAILED", card.text)

    def test_cancelled_card(self):
        card = ui.hls_cancelled_card()
        self.assertIn("EXTRACTION CANCELLED", card.text)
        self.assertIn("No results were kept", card.text)

    def test_detection_card_includes_hls_shortcut(self):
        card = ui.detection_card(
            kind="generic",
            platform_label="Example",
            title="Clip",
            duration_text=None,
            quality_text=None,
            choices=(),
            session_id="abc123def456",
        )
        payloads = [button.data for row in card.buttons for button in row]
        actions = [callbacks.decode(payload)[0] for payload in payloads]
        self.assertIn("hls_run", actions)
        hls_payload = next(p for p in payloads if callbacks.decode(p)[0] == "hls_run")
        action, parts = callbacks.decode(hls_payload)
        self.assertEqual((action, parts), ("hls_run", ("abc123def456",)))

    def test_welcome_card_has_extract_hls_entry(self):
        card = ui.welcome_card("Ada", 700)
        payloads = [button.data for row in card.buttons for button in row]
        actions = [callbacks.decode(payload)[0] for payload in payloads]
        self.assertIn("menu", actions)
        hls_payload = next(
            p
            for p in payloads
            if callbacks.decode(p) == ("menu", ("hls",))
        )
        self.assertTrue(hls_payload)

    def test_browser_label_used_in_results(self):
        results = (_fake_result(method=METHOD_BROWSER, status=STATUS_FOUND),)
        card = ui.hls_results_card("https://example.com/", results, 0, "sess5", browser_note="Browser network capture unavailable on this host.")
        self.assertIn("Browser Network Request", card.text)
        self.assertIn("Browser network capture unavailable", card.text)


if __name__ == "__main__":
    unittest.main()
