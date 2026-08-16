from __future__ import annotations

import subprocess
import os
import tempfile
import unittest
import json
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from tvsub_mcp.service import TvsubService, should_use_applescript_position


SRT = """1
00:00:10,000 --> 00:00:12,000
First line

2
00:10:00,000 --> 00:10:02,000
Late line

"""


class ServiceTests(unittest.TestCase):
    @patch("tvsub_mcp.backends.executable_for", return_value="/bin/claude")
    def test_translate_uses_backend_from_environment_when_argument_is_omitted(self, _executable) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "subtitles").mkdir()
            (root / "subtitles" / "sample.srt").write_text(SRT, encoding="utf-8")
            service = TvsubService(root, mock=True)
            with patch.dict(os.environ, {"TVSUB_TRANSLATE_BACKEND": "claude"}, clear=False):
                result = service.translate_subtitle("sample.srt", dry_run=True)
            self.assertEqual(result["backend"], "claude")

    def test_cross_source_threshold_matches_swift_rule(self) -> None:
        self.assertTrue(should_use_applescript_position(35.8, 300.1))
        self.assertFalse(should_use_applescript_position(100.0, 101.999))
        self.assertFalse(should_use_applescript_position(100.0, 102.0))
        self.assertTrue(should_use_applescript_position(100.0, 102.001))

    def test_live_now_playing_uses_tv_applescript_on_disagreement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "src").mkdir()
            (root / "src" / "nowplaying.js").write_text("fixture", encoding="utf-8")
            media_remote = SimpleNamespace(
                returncode=0,
                stdout=(
                    '{"bundleID":"com.apple.TV","app":"TV",'
                    '"kMRMediaRemoteNowPlayingInfoTitle":"Example",'
                    '"kMRMediaRemoteNowPlayingInfoElapsedTime":35.8,'
                    '"kMRMediaRemoteNowPlayingInfoTimestamp":0,'
                    '"kMRMediaRemoteNowPlayingInfoPlaybackRate":1,'
                    '"kMRMediaRemoteNowPlayingInfoDuration":1000}'
                ),
                stderr="",
            )
            service = TvsubService(root)
            with patch("tvsub_mcp.service.platform.system", return_value="Darwin"), \
                 patch("tvsub_mcp.service.subprocess.run", return_value=media_remote), \
                 patch.object(service, "_tv_player_ground_truth", return_value=(300.1, "playing")):
                playback = service.now_playing()
            self.assertTrue(playback["sync_disagreement"])
            self.assertEqual(playback["location_ms"], 300_100)
            self.assertEqual(playback["media_remote_location_ms"], 35_800)
            self.assertEqual(playback["source"], "AppleScript/TV (MediaRemote disagreement)")

    def test_live_now_playing_quietly_falls_back_when_applescript_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "src").mkdir()
            (root / "src" / "nowplaying.js").write_text("fixture", encoding="utf-8")
            media_remote = SimpleNamespace(
                returncode=0,
                stdout=(
                    '{"bundleID":"com.apple.TV",'
                    '"kMRMediaRemoteNowPlayingInfoElapsedTime":35.8,'
                    '"kMRMediaRemoteNowPlayingInfoTimestamp":0,'
                    '"kMRMediaRemoteNowPlayingInfoPlaybackRate":0}'
                ),
                stderr="",
            )
            service = TvsubService(root)
            with patch("tvsub_mcp.service.platform.system", return_value="Darwin"), \
                 patch("tvsub_mcp.service.subprocess.run", return_value=media_remote), \
                 patch.object(service, "_tv_player_ground_truth", side_effect=subprocess.TimeoutExpired("osascript", 2)):
                playback = service.now_playing()
            self.assertFalse(playback["sync_disagreement"])
            self.assertEqual(playback["location_ms"], 35_800)
            self.assertEqual(playback["source"], "MediaRemote/JXA")

    def test_calibration_refuses_disagreeing_anchor_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "subtitles").mkdir()
            (root / "subtitles" / "sample.srt").write_text(SRT, encoding="utf-8")
            service = TvsubService(root, mock=True)
            service.load_subtitle("sample.srt", "movie-1")
            config_path = root / "config" / "movie-movie-1.json"
            before = config_path.read_text(encoding="utf-8")
            disagreement = {
                "is_playing": False,
                "location_ms": 300_100,
                "sync_disagreement": True,
                "media_remote_location_ms": 35_800,
                "apple_script_location_ms": 300_100,
            }
            with patch.object(service, "now_playing", return_value=disagreement):
                result = service.calibrate_sync("First line", store_id="movie-1")
            self.assertFalse(result["anchor_recorded"])
            self.assertEqual(result["reason"], "mediaremote-applescript-disagreement")
            self.assertEqual(config_path.read_text(encoding="utf-8"), before)

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
            style = service.set_style(
                font_family="Apple SD Gothic Neo", font_size=52,
                bottom_margin=0.09, language="ko",
            )
            self.assertTrue(style["applies_live"])
            self.assertEqual(style["positioning"]["reference"], "tv-video-window")
            self.assertEqual(style["positioning"]["bottom_margin_ratio"], 0.09)
            self.assertEqual(style["positioning"]["fallback"], "configured-screen")
            status = service.status()
            self.assertEqual(status["positioning"], style["positioning"])
            self.assertEqual(status["positioning"]["screen_index_role"], "fallback-only")
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

    def test_glossary_and_reviewed_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "subtitles").mkdir()
            source = root / "subtitles" / "show.ko.srt"
            source.write_text(SRT, encoding="utf-8")
            service = TvsubService(root, mock=True)
            result = service.set_glossary("show.ko.srt", {
                "names": {"철수": "Cheol-su"},
                "relationships": [{"from": "철수", "to": "영희", "speech": "banmal"}],
                "terms": {"김치찌개": "kimchi jjigae"},
                "forbidden_translations": ["kimchi stew"],
            })
            self.assertEqual(result["glossary"], "subtitles/show.glossary.json")
            output = root / "subtitles" / "show.en.srt"
            output.write_text(SRT, encoding="utf-8")
            provenance = output.with_suffix(".srt.provenance.json")
            provenance.write_text(json.dumps({"status": "ai_draft", "model": "fixture"}), encoding="utf-8")
            reviewed = service.mark_reviewed("show.en.srt")
            self.assertEqual(reviewed["status"], "user_reviewed")
            self.assertEqual(json.loads(provenance.read_text())["status"], "user_reviewed")

    def test_status_exposes_loaded_subtitle_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "subtitles").mkdir()
            subtitle = root / "subtitles" / "sample.srt"
            subtitle.write_text(SRT, encoding="utf-8")
            service = TvsubService(root, mock=True)
            service.load_subtitle("sample.srt", "fixture-movie-001")
            subtitle.with_suffix(".srt.provenance.json").write_text(
                json.dumps({"status": "ai_draft", "model": "fixture", "source_sha256": "abc"}),
                encoding="utf-8",
            )
            status = service.status()
            self.assertEqual(status["provenance"]["status"], "ai_draft")
            self.assertEqual(status["provenance"]["model"], "fixture")


if __name__ == "__main__":
    unittest.main()
