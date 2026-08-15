from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from tvsub_mcp.service import TvsubService


SRT = """1
00:00:10,000 --> 00:00:12,000
First line

2
00:10:00,000 --> 00:10:02,000
Late line

"""


class ServiceTests(unittest.TestCase):
    def test_mock_flow_and_compatible_movie_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "subtitles").mkdir()
            (root / "subtitles" / "sample.srt").write_text(SRT, encoding="utf-8")
            service = TvsubService(root, mock=True)
            self.assertEqual(service.now_playing()["title"], "Example Movie (Mock)")
            self.assertEqual(service.now_playing()["store_id"], "fixture-movie-001")
            listed = service.list_subtitles()
            self.assertEqual(listed["count"], 1)
            self.assertEqual(listed["library"], "subtitles")
            self.assertEqual(listed["subtitles"][0]["path"], "subtitles/sample.srt")
            loaded = service.load_subtitle("sample.srt", "movie-1")
            self.assertTrue(loaded["loaded"])
            self.assertEqual(loaded["subtitle"]["path"], "subtitles/sample.srt")
            first = service.calibrate_sync("First line", store_id="movie-1", actual_time=15)
            second = service.calibrate_sync("Late line", store_id="movie-1", actual_time=620)
            self.assertEqual(second["anchor_count"], 2)
            config = (root / "config" / "movie-movie-1.json").read_text(encoding="utf-8")
            self.assertIn('"subTime"', config)
            self.assertIn('"actualTime"', config)
            started = service.start_overlay(store_id="movie-1")
            self.assertTrue(started["started"])
            self.assertEqual(started["subtitle"], "subtitles/sample.srt")
            self.assertNotIn(str(root), str(service.status()))
            style = service.set_style(font_family="Apple SD Gothic Neo", font_size=52, language="ko")
            self.assertTrue(style["applies_live"])
            self.assertTrue(service.stop_overlay()["stopped"])

    def test_start_overlay_movie_override_does_not_bypass_tv_source_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "subtitles").mkdir()
            (root / "subtitles" / "sample.srt").write_text(SRT, encoding="utf-8")
            service = TvsubService(root, mock=True)
            service.load_subtitle("sample.srt", "movie-1")

            for bundle_id in ("com.apple.Music", ""):
                with self.subTest(bundle_id=bundle_id or "missing"):
                    with patch.dict(
                        "os.environ",
                        {"TVSUB_MOCK_APP": "Music", "TVSUB_MOCK_BUNDLE_ID": bundle_id},
                    ):
                        with self.assertRaisesRegex(RuntimeError, "소스 검사는 우회하지 않습니다"):
                            service.start_overlay(store_id="movie-1")
                    self.assertFalse(service.overlay_state_path.exists())

    def test_missing_subtitle_error_does_not_expose_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = TvsubService(root, mock=True)
            with self.assertRaises(FileNotFoundError) as caught:
                service.load_subtitle("missing.srt", "movie-1")
            self.assertNotIn(str(root), str(caught.exception))
            self.assertIn("subtitles/missing.srt", str(caught.exception))

    def test_parse_error_does_not_expose_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "subtitles").mkdir()
            (root / "subtitles" / "broken.srt").write_text("not a subtitle", encoding="utf-8")
            service = TvsubService(root, mock=True)
            listed = service.list_subtitles()
            self.assertNotIn(str(root), str(listed))
            with self.assertRaises(ValueError) as caught:
                service.load_subtitle("broken.srt", "movie-1")
            self.assertNotIn(str(root), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
