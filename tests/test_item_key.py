from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tvsub_mcp.service import TvsubService, _atomic_json


class ContentItemKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.service = TvsubService(self.root)
        (self.root / "src").mkdir()
        (self.root / "src" / "nowplaying.js").write_text("fixture", encoding="utf-8")
        (self.root / "subtitles" / "sample.srt").write_text(
            "1\n00:00:10,000 --> 00:00:12,000\nFirst line\n", encoding="utf-8"
        )
        self.payload = {
            "bundleID": "com.apple.TV",
            "kMRMediaRemoteNowPlayingInfoTitle": "Example Movie",
            "kMRMediaRemoteNowPlayingInfoContentItemIdentifier": "123",
        }
        self.addCleanup(patch.stopall)
        patch("tvsub_mcp.service.platform.system", return_value="Darwin").start()
        patch("tvsub_mcp.service.subprocess.run", side_effect=lambda *args, **kwargs:
              SimpleNamespace(returncode=0, stdout=json.dumps(self.payload), stderr="")).start()
        patch.object(self.service, "_tv_player_ground_truth", return_value=(0.0, "paused")).start()

    def legacy(self, title: str | None = "Example Movie") -> tuple[Path, dict]:
        config = {
            "storeId": "123", "title": title, "subtitle": "subtitles/sample.srt",
            "anchors": [{"subTime": 600.0, "actualTime": 603.0}],
            "scale": 1.0, "offset": 3.0, "custom": {"preserve": True},
        }
        path = self.service.config_dir / "movie-123.json"
        _atomic_json(path, config)
        return path, config

    def test_store_identifier_has_priority_and_exposes_both_originals(self) -> None:
        self.payload["kMRMediaRemoteNowPlayingInfoiTunesStoreIdentifier"] = 456
        playback = self.service.now_playing()
        self.assertEqual(playback["store_id"], "456")
        self.assertEqual(playback["store_identifier"], "456")
        self.assertEqual(playback["content_item_identifier"], "123")

    def test_item_identifier_gets_prefix_without_writing_config(self) -> None:
        self.legacy()
        for store in (None, ""):
            with self.subTest(store=store):
                self.payload["kMRMediaRemoteNowPlayingInfoiTunesStoreIdentifier"] = store
                playback = self.service.now_playing()
                self.assertEqual(playback["store_id"], "item-123")
                self.assertIsNone(playback["store_identifier"])
                self.assertEqual(playback["content_item_identifier"], "123")
                self.assertFalse((self.service.config_dir / "movie-item-123.json").exists())

    def test_same_identifier_has_separate_paths_and_preserves_store_config(self) -> None:
        legacy, original = self.legacy("Store Movie")
        self.payload["kMRMediaRemoteNowPlayingInfoiTunesStoreIdentifier"] = "123"
        store_key = self.service.now_playing()["store_id"]
        self.assertEqual(self.service.status()["subtitle"], "subtitles/sample.srt")
        del self.payload["kMRMediaRemoteNowPlayingInfoiTunesStoreIdentifier"]
        item_key = self.service.now_playing()["store_id"]
        self.assertNotEqual(self.service._movie_path(store_key), self.service._movie_path(item_key))
        self.service.load_subtitle("sample.srt")
        self.assertEqual(json.loads(legacy.read_text()), original)
        self.assertEqual(json.loads(self.service._movie_path(item_key).read_text())["anchors"], [])

    def test_missing_identifiers_reject_config_operations_without_unknown_file(self) -> None:
        for missing in (None, ""):
            with self.subTest(missing=missing):
                self.payload["kMRMediaRemoteNowPlayingInfoContentItemIdentifier"] = missing
                self.payload["kMRMediaRemoteNowPlayingInfoiTunesStoreIdentifier"] = missing
                playback = self.service.now_playing()
                self.assertFalse(playback["store_id"])
                self.assertIsNone(playback["store_identifier"])
                self.assertIsNone(playback["content_item_identifier"])
                for operation in (
                    lambda: self.service.load_subtitle("sample.srt"),
                    lambda: self.service.calibrate_sync(spoken_text="First line", actual_time=12),
                    self.service.start_overlay,
                ):
                    with self.assertRaisesRegex(ValueError, "콘텐츠 ID"):
                        operation()
                self.assertIsNone(self.service.status()["subtitle"])
        self.assertEqual(list(self.service.config_dir.glob("movie-*.json")), [])

    def test_matching_title_copies_once_atomically_preserving_old_file(self) -> None:
        legacy, original = self.legacy()
        before = legacy.read_bytes()
        target = self.service.config_dir / "movie-item-123.json"
        with patch("tvsub_mcp.service._atomic_json", wraps=_atomic_json) as write:
            self.assertEqual(self.service.status()["subtitle"], original["subtitle"])
            self.service.status()
        write.assert_called_once_with(target, {**original, "storeId": "item-123"})
        self.assertEqual(json.loads(target.read_text()), {**original, "storeId": "item-123"})
        self.assertEqual(legacy.read_bytes(), before)

    def test_mismatching_or_missing_titles_do_not_migrate(self) -> None:
        for old_title, current_title in (
            ("Other Movie", "Example Movie"), ("", "Example Movie"),
            (None, "Example Movie"), ("Example Movie", ""),
            ("Example Movie", None), ("", ""), (None, None),
        ):
            with self.subTest(old=old_title, current=current_title):
                legacy, original = self.legacy(old_title)
                self.payload["kMRMediaRemoteNowPlayingInfoTitle"] = current_title
                self.assertIsNone(self.service.status()["subtitle"])
                self.assertFalse((self.service.config_dir / "movie-item-123.json").exists())
                self.assertEqual(json.loads(legacy.read_text()), original)

    def test_existing_item_config_is_never_overwritten(self) -> None:
        self.legacy()
        target = self.service.config_dir / "movie-item-123.json"
        target.write_text('{"storeId":"item-123","subtitle":"kept.srt"}\n', encoding="utf-8")
        before = target.read_bytes()
        self.assertEqual(self.service.status()["subtitle"], "kept.srt")
        self.assertEqual(target.read_bytes(), before)

    def test_load_subtitle_migrates_before_preserving_anchors(self) -> None:
        legacy, original = self.legacy()
        result = self.service.load_subtitle("sample.srt")
        self.assertEqual(result["store_id"], "item-123")
        migrated = json.loads((self.service.config_dir / "movie-item-123.json").read_text())
        self.assertEqual(migrated, {**original, "storeId": "item-123"})
        self.assertEqual(json.loads(legacy.read_text()), original)

    def test_calibration_migrates_before_adding_anchor(self) -> None:
        legacy, original = self.legacy()
        result = self.service.calibrate_sync(spoken_text="First line", actual_time=13)
        self.assertEqual(result["store_id"], "item-123")
        self.assertEqual(result["anchor_count"], 2)
        self.assertEqual(json.loads(legacy.read_text()), original)

    def test_start_overlay_passes_item_key_to_old_app(self) -> None:
        self.legacy()
        self.service.binary.parent.mkdir()
        self.service.binary.touch()
        process = Mock(pid=12345)
        process.poll.return_value = None
        with patch("tvsub_mcp.service.subprocess.Popen", return_value=process) as popen, \
             patch("tvsub_mcp.service.time.sleep"):
            result = self.service.start_overlay(subtitle="sample.srt")
        self.assertEqual(result["store_id"], "item-123")
        self.assertEqual(popen.call_args.args[0], [
            str(self.service.binary), "run", "--movie", "item-123", "--sub", "subtitles/sample.srt",
        ])
        config = json.loads((self.service.config_dir / "movie-item-123.json").read_text())
        self.assertEqual(len(config["anchors"]), 1)

    def test_start_overlay_without_subtitle_migrates(self) -> None:
        self.legacy()
        # Keep real now_playing parsing while avoiding a GUI process.
        playback = self.service.now_playing()
        self.service.mock = True
        with patch.object(self.service, "now_playing", return_value=playback) as now:
            self.assertEqual(self.service.start_overlay()["store_id"], "item-123")
        now.assert_called_once_with()

    def test_explicit_item_key_never_migrates(self) -> None:
        legacy, original = self.legacy()
        self.service.load_subtitle("sample.srt", store_id="item-123")
        target = self.service.config_dir / "movie-item-123.json"
        self.assertEqual(json.loads(target.read_text())["anchors"], [])
        self.assertEqual(json.loads(legacy.read_text()), original)

    def test_explicit_store_key_keeps_old_path_even_when_now_playing_is_item(self) -> None:
        legacy, original = self.legacy()
        self.service.load_subtitle("sample.srt", store_id="123")
        self.assertEqual(json.loads(legacy.read_text()), original)
        self.assertFalse((self.service.config_dir / "movie-item-123.json").exists())

    def test_store_identifier_with_item_prefix_does_not_trigger_migration(self) -> None:
        self.legacy()
        self.payload["kMRMediaRemoteNowPlayingInfoiTunesStoreIdentifier"] = "item-123"
        self.assertIsNone(self.service.status()["subtitle"])
        self.assertFalse((self.service.config_dir / "movie-item-123.json").exists())

    def test_explicit_key_is_used_verbatim_without_playback_identifiers(self) -> None:
        self.payload.pop("kMRMediaRemoteNowPlayingInfoContentItemIdentifier")
        result = self.service.load_subtitle("sample.srt", store_id=" explicit-key ")
        self.assertEqual(result["store_id"], " explicit-key ")
        self.assertTrue((self.service.config_dir / "movie- explicit-key .json").exists())

    def test_explicit_item_key_in_start_and_calibrate_does_not_migrate(self) -> None:
        self.legacy()
        for operation in (
            lambda: self.service.start_overlay(store_id="item-123"),
            lambda: self.service.calibrate_sync(store_id="item-123", spoken_text="First line", actual_time=13),
        ):
            with self.assertRaises(ValueError):
                operation()
        self.assertFalse((self.service.config_dir / "movie-item-123.json").exists())

    def test_unsafe_keys_are_rejected_before_read_or_write(self) -> None:
        for value in (".", "..", "../escape", "bad/name", "bad\\name", "item-../escape"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.service.load_subtitle("sample.srt", store_id=value)
                self.payload["kMRMediaRemoteNowPlayingInfoiTunesStoreIdentifier"] = value
                with self.assertRaises(ValueError):
                    self.service.status()
        self.payload.pop("kMRMediaRemoteNowPlayingInfoiTunesStoreIdentifier")
        for value in ("../escape", "bad/name", "bad\\name"):
            self.payload["kMRMediaRemoteNowPlayingInfoContentItemIdentifier"] = value
            with self.assertRaises(ValueError):
                self.service.status()

    def test_mock_returns_same_key_contract(self) -> None:
        self.service.mock = True
        with patch.dict(os.environ, {"TVSUB_MOCK_STORE_ID": "", "TVSUB_MOCK_CONTENT_ITEM_ID": "123"}):
            playback = self.service.now_playing()
        self.assertEqual(playback["store_id"], "item-123")
        self.assertIsNone(playback["store_identifier"])
        self.assertEqual(playback["content_item_identifier"], "123")


if __name__ == "__main__":
    unittest.main()
