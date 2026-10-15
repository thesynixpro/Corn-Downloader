import asyncio
import os
import tempfile
import unittest

from app import hls
from app.hls import DRMProtectedError, PlaylistError  # noqa: F401  (API surface check)

BASE = "https://cdn.example.com/vod/master.m3u8"

MASTER = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",LANGUAGE="en",DEFAULT=YES,AUTOSELECT=YES,URI="audio/en.m3u8"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="French",LANGUAGE="fr",DEFAULT=NO,AUTOSELECT=YES,URI="audio/fr.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.4d401e,mp4a.40.2",AUDIO="aud"
360/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720,CODECS="avc1.64001f,mp4a.40.2",AUDIO="aud"
720/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2",AUDIO="aud"
1080/index.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:5
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x1234
#EXT-X-MAP:URI="init.mp4"
#EXTINF:10.0,
seg0.ts
#EXTINF:10.0,segment one
../seg1.ts
#EXT-X-BYTERANGE:1024@0
#EXTINF:10.0,
seg2.ts
#EXT-X-BYTERANGE:512
#EXTINF:10.0,
seg2.ts
#EXT-X-ENDLIST
"""


class DetectKindTests(unittest.TestCase):
    def test_master(self):
        self.assertEqual(hls.detect_playlist_kind(MASTER), hls.PlaylistKind.MASTER)

    def test_media(self):
        self.assertEqual(hls.detect_playlist_kind(MEDIA), hls.PlaylistKind.MEDIA)

    def test_unknown(self):
        self.assertEqual(hls.detect_playlist_kind("<html>nope</html>"), hls.PlaylistKind.UNKNOWN)
        self.assertEqual(hls.detect_playlist_kind(""), hls.PlaylistKind.UNKNOWN)


class AttributeParserTests(unittest.TestCase):
    def test_quoted_commas_survive(self):
        attrs = hls.parse_attribute_list(
            'BANDWIDTH=2400000,RESOLUTION=1280x720,CODECS="avc1.64001f,mp4a.40.2",AUDIO="aud"'
        )
        self.assertEqual(attrs["CODECS"], "avc1.64001f,mp4a.40.2")
        self.assertEqual(attrs["BANDWIDTH"], "2400000")
        self.assertEqual(attrs["RESOLUTION"], "1280x720")


class MasterParseTests(unittest.TestCase):
    def setUp(self):
        self.master = hls.parse_master(MASTER, BASE)

    def test_variants_parsed_and_resolved(self):
        self.assertEqual(len(self.master.variants), 3)
        self.assertEqual(self.master.variants[0].uri, "https://cdn.example.com/vod/360/index.m3u8")
        self.assertEqual(self.master.variants[1].width, 1280)
        self.assertEqual(self.master.variants[1].height, 720)
        self.assertEqual(self.master.variants[1].audio_group, "aud")

    def test_renditions_parsed(self):
        self.assertEqual(len(self.master.renditions), 2)
        self.assertTrue(self.master.renditions[0].default)
        self.assertEqual(self.master.renditions[0].uri, "https://cdn.example.com/vod/audio/en.m3u8")

    def test_select_respects_height_cap(self):
        variant = hls.select_variant(self.master.variants, max_height=720)
        self.assertEqual(variant.height, 720)

    def test_select_best_without_cap(self):
        variant = hls.select_variant(self.master.variants, max_height=0)
        self.assertEqual(variant.height, 1080)

    def test_select_smallest_when_all_exceed_cap(self):
        variant = hls.select_variant(self.master.variants, max_height=240)
        self.assertEqual(variant.height, 360)

    def test_select_audio_rendition_prefers_default(self):
        video = hls.select_variant(self.master.variants, max_height=720)
        audio = hls.select_audio_rendition(self.master.renditions, video)
        self.assertIsNotNone(audio)
        self.assertEqual(audio.language, "en")

    def test_empty_master_raises(self):
        with self.assertRaises(PlaylistError):
            hls.parse_master("#EXTM3U\n#EXT-X-VERSION:3\n", BASE)


class MediaParseTests(unittest.TestCase):
    def setUp(self):
        self.playlist = hls.parse_media(MEDIA, BASE)

    def test_init_segment_first(self):
        self.assertTrue(self.playlist.segments[0].is_init)
        self.assertEqual(self.playlist.segments[0].uri, "https://cdn.example.com/vod/init.mp4")

    def test_segments_resolved(self):
        self.assertEqual(self.playlist.segments[1].uri, "https://cdn.example.com/vod/seg0.ts")
        self.assertEqual(self.playlist.segments[2].uri, "https://cdn.example.com/seg1.ts")

    def test_total_duration(self):
        self.assertAlmostEqual(self.playlist.total_duration, 40.0)

    def test_aes_key_detected(self):
        self.assertTrue(self.playlist.is_encrypted)
        self.assertEqual(self.playlist.encryption_methods, {"AES-128"})
        self.assertEqual(self.playlist.key_uris, ["https://cdn.example.com/vod/key.bin"])

    def test_byte_ranges(self):
        byterange_segments = [s for s in self.playlist.segments if s.byte_range]
        self.assertEqual(byterange_segments[0].byte_range, (0, 1024))
        self.assertEqual(byterange_segments[1].byte_range, (1024, 512))

    def test_vod_not_live(self):
        self.assertFalse(self.playlist.is_live)
        self.assertTrue(self.playlist.is_endlist)

    def test_drm_sample_aes_detected(self):
        text = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:6\n"
            '#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://key"\n'
            "#EXTINF:6.0,\nseg0.ts\n#EXT-X-ENDLIST\n"
        )
        playlist = hls.parse_media(text, BASE)
        self.assertTrue(playlist.has_drm)

    def test_live_playlist_detected(self):
        text = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:10\n"
            "#EXTINF:6.0,\nseg0.ts\n#EXTINF:6.0,\nseg1.ts\n"
        )
        playlist = hls.parse_media(text, BASE)
        self.assertTrue(playlist.is_live)

    def test_rejects_empty_media_playlist(self):
        with self.assertRaises(PlaylistError):
            hls.parse_media("#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXT-X-ENDLIST\n", BASE)


class SegmentDownloadTests(unittest.IsolatedAsyncioTestCase):
    def _segments(self, count=3):
        return [hls.Segment(uri=f"https://cdn.example.com/s{i}.ts", duration=4.0) for i in range(count)]

    async def test_downloads_in_order_with_retries(self):
        segments = self._segments()
        attempts: dict[int, int] = {}

        async def fetch(segment: hls.Segment) -> bytes:
            index = int(segment.uri.rsplit("/s", 1)[1].split(".")[0])
            attempts[index] = attempts.get(index, 0) + 1
            if index == 1 and attempts[index] < 3:
                raise RuntimeError("transient")
            return f"data-{index}".encode()

        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "out.bin")
            total = await hls.download_segments(
                segments,
                fetch,
                dest,
                os.path.join(tmp, "work"),
                concurrency=2,
                retries=3,
                retry_delay=0,
                sleep_fn=lambda _seconds: asyncio.sleep(0),
            )
            with open(dest, "rb") as handle:
                content = handle.read()
        self.assertEqual(content, b"data-0data-1data-2")
        self.assertEqual(attempts[1], 3)
        self.assertEqual(total, len(content))

    async def test_gives_up_after_bounded_retries(self):
        segments = self._segments(1)
        calls = 0

        async def fetch(segment: hls.Segment) -> bytes:
            nonlocal calls
            calls += 1
            raise RuntimeError("down")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PlaylistError):
                await hls.download_segments(
                    segments,
                    fetch,
                    os.path.join(tmp, "out.bin"),
                    os.path.join(tmp, "work"),
                    retries=2,
                    retry_delay=0,
                    sleep_fn=lambda _seconds: asyncio.sleep(0),
                )
        self.assertEqual(calls, 3)

    async def test_progress_callback_reports_completion(self):
        segments = self._segments(4)
        seen: list[tuple[int, int]] = []

        async def fetch(segment: hls.Segment) -> bytes:
            return b"x" * 10

        def on_progress(done: int, total: int, _bytes_total: int) -> None:
            seen.append((done, total))

        with tempfile.TemporaryDirectory() as tmp:
            await hls.download_segments(
                segments,
                fetch,
                os.path.join(tmp, "out.bin"),
                os.path.join(tmp, "work"),
                concurrency=4,
                progress_cb=on_progress,
            )
        self.assertEqual(len(seen), 4)
        self.assertEqual(sorted(done for done, _ in seen), [1, 2, 3, 4])
        self.assertTrue(all(total == 4 for _, total in seen))

    async def test_rejects_empty_segment(self):
        async def fetch(segment: hls.Segment) -> bytes:
            return b""

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PlaylistError):
                await hls.download_segments(
                    self._segments(1),
                    fetch,
                    os.path.join(tmp, "out.bin"),
                    os.path.join(tmp, "work"),
                    retries=0,
                )


if __name__ == "__main__":
    unittest.main()
