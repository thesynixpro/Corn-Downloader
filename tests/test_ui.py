import unittest

from app import callbacks, ui
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
        self.assertIn("NOVA FETCH", card.text)
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


if __name__ == "__main__":
    unittest.main()
