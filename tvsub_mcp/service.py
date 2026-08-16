from __future__ import annotations

import json
import os
import platform
import re
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .calibration import calculate
from .subtitles import Cue, load_subtitle
from .translation import translate
from .glossary import glossary_path, validate_glossary
from .translation import provenance_path

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")
DEFAULT_SETTINGS = {
    "fontFamily": ".AppleSystemUIFont",
    "fontSize": 40.0,
    "fontColor": "#FFFFFF",
    "outlineColor": "#000000",
    "outlineWidth": 5.0,
    "bottomMargin": 0.075,
    "backgroundAlpha": 0.45,
    "maxLines": 3,
    "pollInterval": 0.5,
    "level": "statusBar",
    "screenIndex": -1,
    "lineSpacing": 6.0,
}
LANGUAGE_SAMPLES = {
    "ko": "한글 자막",
    "ja": "日本語字幕",
    "zh": "中文字幕",
    "zh-hans": "简体中文字幕",
    "zh-hant": "繁體中文字幕",
    "ar": "ترجمة عربية",
    "ru": "Русские субтитры",
    "th": "คำบรรยายไทย",
    "hi": "हिन्दी उपशीर्षक",
}
PLAYBACK_SYNC_THRESHOLD = 2.0


def should_use_applescript_position(
    media_remote_position: float,
    apple_script_position: float,
    threshold: float = PLAYBACK_SYNC_THRESHOLD,
) -> bool:
    """Return True only when the independent TV.app position differs beyond the shared threshold."""
    return abs(media_remote_position - apple_script_position) > threshold


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class TvsubService:
    def __init__(self, tvsub_root: Path, *, mock: bool = False):
        self.root = tvsub_root.expanduser().resolve()
        self.mock = mock
        self.subtitles_dir = self.root / "subtitles"
        self.config_dir = self.root / "config"
        self.binary = self.root / "build" / "tvsub"
        self.settings_path = self.config_dir / "settings.json"
        self.overlay_state_path = self.config_dir / "tvsub-mcp-overlay.json"
        self.server_dir = Path(__file__).resolve().parent.parent
        self.subtitles_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)

    def _relative_path(self, path: str | Path) -> str:
        """Return a root-relative path for tool results without exposing the user home path."""
        candidate = Path(path)
        try:
            return candidate.resolve().relative_to(self.root).as_posix()
        except (OSError, ValueError):
            return candidate.name

    def _public_payload(self, value: Any) -> Any:
        """Redact absolute filesystem paths before returning state through MCP."""
        if isinstance(value, dict):
            return {key: self._public_payload(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._public_payload(item) for item in value]
        if isinstance(value, str) and Path(value).is_absolute():
            return self._relative_path(value)
        return value

    def _public_error(self, error: Exception | str) -> str:
        return str(error).replace(str(self.root), ".")

    def now_playing(self) -> dict:
        if self.mock:
            elapsed = float(os.getenv("TVSUB_MOCK_ELAPSED", "65.0"))
            rate = float(os.getenv("TVSUB_MOCK_RATE", "0"))
            bundle_id = os.getenv("TVSUB_MOCK_BUNDLE_ID", "com.apple.TV") or None
            disagreement = False
            media_remote_elapsed = elapsed
            apple_script_elapsed = os.getenv("TVSUB_MOCK_AS_ELAPSED")
            if bundle_id == "com.apple.TV" and apple_script_elapsed is not None:
                as_elapsed = float(apple_script_elapsed)
                disagreement = should_use_applescript_position(elapsed, as_elapsed)
                if disagreement:
                    elapsed = as_elapsed
                    rate = 1.0 if os.getenv("TVSUB_MOCK_AS_STATE", "paused") == "playing" else 0.0
            return {
                "title": os.getenv("TVSUB_MOCK_TITLE", "Example Movie (Mock)"),
                "store_id": os.getenv("TVSUB_MOCK_STORE_ID", "fixture-movie-001"),
                "location_ms": round(elapsed * 1000),
                "duration_ms": 8_609_152,
                "is_playing": rate > 0.01,
                "playback_rate": rate,
                "app": os.getenv("TVSUB_MOCK_APP", "TV"),
                "bundle_id": bundle_id,
                "is_tv_app": bundle_id == "com.apple.TV",
                "source": "mock/AppleScript" if disagreement else "mock",
                "sync_disagreement": disagreement,
                "media_remote_location_ms": round(media_remote_elapsed * 1000),
                "apple_script_location_ms": (
                    round(float(apple_script_elapsed) * 1000) if apple_script_elapsed is not None else None
                ),
            }
        if platform.system() != "Darwin":
            raise RuntimeError("MediaRemote 조회는 macOS에서만 가능합니다. Linux에서는 --mock을 사용하세요.")
        script = self.root / "src" / "nowplaying.js"
        if not script.exists():
            raise FileNotFoundError("기존 JXA 헬퍼가 없습니다: src/nowplaying.js")
        completed = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", str(script)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"MediaRemote JXA 실패: {self._public_error(completed.stderr.strip())}")
        raw = completed.stdout.strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end < start:
            raise RuntimeError(f"MediaRemote 응답이 JSON이 아닙니다: {raw[:200]}")
        payload = json.loads(raw[start:end + 1])
        if payload.get("_error"):
            raise RuntimeError(f"재생 항목 없음: {payload['_error']}")

        def number(key: str, default: float = 0.0) -> float:
            try:
                return float(payload.get(key, default))
            except (TypeError, ValueError):
                return default

        elapsed = number("kMRMediaRemoteNowPlayingInfoElapsedTime")
        rate = number("kMRMediaRemoteNowPlayingInfoPlaybackRate")
        timestamp = number("kMRMediaRemoteNowPlayingInfoTimestamp")
        now = time.time()
        if rate > 0.01 and -5 < now - timestamp < 21_600:
            elapsed += (now - timestamp) * rate
        duration = number("kMRMediaRemoteNowPlayingInfoDuration")
        if duration > 0:
            elapsed = min(elapsed, duration)
        bundle_id = payload.get("bundleID")
        source = "MediaRemote/JXA"
        disagreement = False
        media_remote_elapsed = elapsed
        apple_script_elapsed: float | None = None
        if bundle_id == "com.apple.TV":
            try:
                apple_script_elapsed, apple_script_state = self._tv_player_ground_truth()
            except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError):
                pass
            else:
                disagreement = should_use_applescript_position(elapsed, apple_script_elapsed)
                if disagreement:
                    elapsed = apple_script_elapsed
                    rate = 1.0 if apple_script_state == "playing" else 0.0
                    source = "AppleScript/TV (MediaRemote disagreement)"
        return {
            "title": payload.get("kMRMediaRemoteNowPlayingInfoTitle"),
            "store_id": str(
                payload.get("kMRMediaRemoteNowPlayingInfoiTunesStoreIdentifier")
                or payload.get("kMRMediaRemoteNowPlayingInfoContentItemIdentifier")
                or ""
            ),
            "location_ms": round(max(0, elapsed) * 1000),
            "duration_ms": round(duration * 1000),
            "is_playing": rate > 0.01,
            "playback_rate": rate,
            "app": payload.get("app"),
            "bundle_id": bundle_id,
            "is_tv_app": bundle_id == "com.apple.TV",
            "source": source,
            "sync_disagreement": disagreement,
            "media_remote_location_ms": round(max(0, media_remote_elapsed) * 1000),
            "apple_script_location_ms": (
                round(max(0, apple_script_elapsed) * 1000) if apple_script_elapsed is not None else None
            ),
        }

    def _tv_player_ground_truth(self) -> tuple[float, str]:
        completed = subprocess.run(
            [
                "/usr/bin/osascript",
                "-e", "with timeout of 1 second",
                "-e", 'tell application "TV"',
                "-e", "set p to player position",
                "-e", "set s to player state as text",
                "-e", "return (p as text) & tab & s",
                "-e", "end tell",
                "-e", "end timeout",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("TV.app AppleScript 조회 실패")
        fields = completed.stdout.strip().split("\t", 1)
        if len(fields) != 2 or fields[1] not in {"playing", "paused", "stopped"}:
            raise ValueError("TV.app AppleScript 응답 형식 오류")
        return max(0.0, float(fields[0])), fields[1]

    def _resolve_subtitle(self, value: str) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.subtitles_dir / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(self.subtitles_dir.resolve())
        except ValueError as exc:
            raise ValueError("자막은 tvsub subtitles/ 라이브러리 안에 있어야 합니다.") from exc
        if not candidate.is_file():
            raise FileNotFoundError(f"자막 파일이 없습니다: {self._relative_path(candidate)}")
        return candidate

    def list_subtitles(self) -> dict:
        items = []
        for path in sorted(self.subtitles_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in {".srt", ".smi", ".sami", ".vtt"}:
                try:
                    document = load_subtitle(path)
                    items.append(document.info(self._relative_path(document.path)))
                except Exception as exc:
                    items.append({"path": self._relative_path(path), "filename": path.name,
                                  "error": self._public_error(exc)})
        return {"library": self._relative_path(self.subtitles_dir), "count": len(items), "subtitles": items}

    def _movie_id(self, store_id: str = "") -> str:
        value = store_id.strip() or str(self.now_playing().get("store_id") or "")
        if not value:
            raise ValueError("콘텐츠 ID를 확인할 수 없습니다. store_id를 지정하세요.")
        if "/" in value or "\\" in value or value in {".", ".."}:
            raise ValueError("안전하지 않은 store_id입니다.")
        return value

    def _movie_path(self, store_id: str) -> Path:
        return self.config_dir / f"movie-{store_id}.json"

    def _movie_config(self, store_id: str) -> dict:
        path = self._movie_path(store_id)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {
            "storeId": store_id,
            "title": None,
            "subtitle": None,
            "subtitle2": None,
            "cd2Offset": 0,
            "smiClass": None,
            "primaryLanguage": None,
            "secondarySubtitle": None,
            "secondarySmiClass": None,
            "secondaryLanguage": None,
            "secondaryScale": 1.0,
            "secondaryOffset": 0.0,
            "secondaryAnchors": [],
            "scale": 1.0,
            "offset": 0.0,
            "anchors": [],
        }

    def load_subtitle(self, subtitle: str, store_id: str = "", reset_anchors: bool = False) -> dict:
        path = self._resolve_subtitle(subtitle)
        try:
            document = load_subtitle(path)
        except Exception as exc:
            raise ValueError(self._public_error(exc)) from exc
        movie_id = self._movie_id(store_id)
        config = self._movie_config(movie_id)
        config["storeId"] = movie_id
        if not config.get("title"):
            # 제목은 **지금 재생 중인 항목이 이 영화일 때만** 기록한다.
            # store_id 를 명시로 넘겨 다른 영화를 설정하는 동안 TV.app 이 딴 걸 틀고 있으면,
            # 그 제목이 이 영화 config 에 박힌다 (tvsub 코어에서 e8dfbd8 로 고친 것과 같은 버그).
            try:
                playback = self.now_playing()
                if str(playback.get("store_id") or "") == movie_id:
                    config["title"] = playback.get("title")
            except Exception:
                pass
        # Swift config 계약: 이식 가능한 tvsub 루트 상대경로만 저장한다.
        config["subtitle"] = path.relative_to(self.root).as_posix()
        if reset_anchors:
            config["anchors"] = []
            config["scale"] = 1.0
            config["offset"] = 0.0
        _atomic_json(self._movie_path(movie_id), config)
        return {"loaded": True, "store_id": movie_id,
                "subtitle": document.info(self._relative_path(document.path)),
                "calibration_preserved": not reset_anchors}

    def translate_subtitle(
        self,
        subtitle: str,
        target_language: str = "ko",
        dry_run: bool = False,
        force: bool = False,
        batch_size: int = 40,
        glossary: str = "",
        line_start: int = 0,
        line_end: int = 0,
        time_start: float = -1,
        time_end: float = -1,
        backend: str | None = None,
    ) -> dict:
        source = self._resolve_subtitle(subtitle)
        try:
            result = translate(
                source,
                target_language,
                dry_run=dry_run,
                force=force,
                batch_size=batch_size,
                glossary=glossary,
                line_range=(line_start, line_end) if line_start or line_end else None,
                time_range=(time_start, time_end) if time_start >= 0 or time_end >= 0 else None,
                backend=backend,
            )
        except Exception as exc:
            raise RuntimeError(self._public_error(exc)) from exc
        return self._public_payload(result)

    def set_glossary(self, subtitle: str, glossary: dict) -> dict:
        source = self._resolve_subtitle(subtitle)
        payload = validate_glossary(glossary)
        path = glossary_path(source)
        _atomic_json(path, payload)
        return {"updated": True, "glossary": self._relative_path(path), "entries": {
            "names": len(payload["names"]), "relationships": len(payload["relationships"]),
            "terms": len(payload["terms"]),
            "forbidden_translations": len(payload["forbidden_translations"]),
        }}

    def mark_reviewed(self, subtitle: str) -> dict:
        output = self._resolve_subtitle(subtitle)
        path = provenance_path(output)
        if not path.exists():
            raise FileNotFoundError(f"provenance 파일이 없습니다: {self._relative_path(path)}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["status"] = "user_reviewed"
        payload["reviewed_at"] = datetime.now(UTC).isoformat()
        _atomic_json(path, payload)
        # v0.2 metadata와 provenance가 같은 출력에 공존하면 상태도 함께 맞춘다.
        legacy = output.with_suffix(output.suffix + ".translation.json")
        if legacy.exists():
            _atomic_json(legacy, payload)
        return {"updated": True, "subtitle": self._relative_path(output),
                "provenance": self._relative_path(path), "status": "user_reviewed"}

    def _provenance_for(self, subtitle_value: str | None) -> dict | None:
        if not subtitle_value:
            return None
        candidate = Path(subtitle_value)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        path = provenance_path(candidate)
        if not path.exists():
            return {"status": "untracked", "path": self._relative_path(path)}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return {"status": payload.get("status", "untracked"), "model": payload.get("model"),
                    "path": self._relative_path(path), "source_sha256": payload.get("source_sha256"),
                    "glossary_sha256": payload.get("glossary_sha256")}
        except (OSError, ValueError):
            return {"status": "invalid", "path": self._relative_path(path)}

    def _settings(self) -> dict:
        if self.settings_path.exists():
            current = json.loads(self.settings_path.read_text(encoding="utf-8"))
            return {**DEFAULT_SETTINGS, **current}
        return dict(DEFAULT_SETTINGS)

    def list_fonts(self, language: str = "ko") -> dict:
        tag = language.lower().replace("_", "-")
        sample = LANGUAGE_SAMPLES.get(tag, LANGUAGE_SAMPLES.get(tag.split("-")[0], "Subtitle 123"))
        if self.mock:
            return {
                "source": "mock",
                "language": tag,
                "sample": sample,
                "fonts": [
                    {"family": "Apple SD Gothic Neo", "supports_sample": True},
                    {"family": "Helvetica Neue", "supports_sample": tag not in {"ko", "ja", "zh"}},
                    {"family": "Noto Sans CJK KR", "supports_sample": True},
                ],
            }
        if platform.system() != "Darwin":
            raise RuntimeError("폰트 열거는 macOS에서만 가능합니다. Linux에서는 --mock을 사용하세요.")
        helper = self.server_dir / "helpers" / "list-fonts.swift"
        completed = subprocess.run(
            ["/usr/bin/xcrun", "swift", str(helper), "--language", tag, "--sample", sample],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"CoreText 폰트 헬퍼 실패: {self._public_error(completed.stderr.strip())}")
        return json.loads(completed.stdout)

    def set_style(
        self,
        *,
        font_family: str = "",
        font_size: float = 0,
        color: str = "",
        outline_color: str = "",
        outline_width: float = -1,
        bottom_margin: float = -1,
        background_alpha: float = -1,
        max_lines: int = 0,
        language: str = "ko",
    ) -> dict:
        settings = self._settings()
        warnings: list[str] = []
        if font_family:
            catalog = self.list_fonts(language)
            matching = [item for item in catalog["fonts"] if item["family"].casefold() == font_family.casefold()]
            if not matching:
                warnings.append(f"'{font_family}' 폰트를 설치 목록에서 찾지 못했습니다. macOS가 시스템 폰트로 폴백합니다.")
            elif not matching[0]["supports_sample"]:
                warnings.append(f"'{font_family}'에 {language} 샘플 글리프가 없어 macOS 폴백이 섞일 수 있습니다.")
            settings["fontFamily"] = font_family
        if font_size:
            if not 12 <= font_size <= 160:
                raise ValueError("font_size는 12~160pt여야 합니다.")
            settings["fontSize"] = font_size
        for argument, key in ((color, "fontColor"), (outline_color, "outlineColor")):
            if argument:
                if not HEX_COLOR.fullmatch(argument):
                    raise ValueError(f"{key}는 #RRGGBB 또는 #RRGGBBAA 형식이어야 합니다.")
                settings[key] = argument.upper()
        if outline_width >= 0:
            if outline_width > 20:
                raise ValueError("outline_width는 0~20이어야 합니다.")
            settings["outlineWidth"] = outline_width
        if bottom_margin >= 0:
            if not 0 <= bottom_margin <= 0.30:
                raise ValueError("bottom_margin은 TV 영상 창 높이 비율 0~0.30이어야 합니다.")
            settings["bottomMargin"] = bottom_margin
        if background_alpha >= 0:
            if not 0 <= background_alpha <= 1:
                raise ValueError("background_alpha는 0~1이어야 합니다.")
            settings["backgroundAlpha"] = background_alpha
        if max_lines:
            if not 1 <= max_lines <= 6:
                raise ValueError("max_lines는 1~6이어야 합니다.")
            settings["maxLines"] = max_lines
        _atomic_json(self.settings_path, settings)
        return {
            "updated": True,
            "control_channel": "atomic settings.json write + tvsub mtime watch",
            "applies_live": self.overlay_status()["running"],
            "settings": settings,
            "positioning": self._positioning_metadata(settings),
            "warnings": warnings,
        }

    @staticmethod
    def _positioning_metadata(settings: dict) -> dict:
        return {
            "reference": "tv-video-window",
            "bottom_margin_ratio": settings.get("bottomMargin", DEFAULT_SETTINGS["bottomMargin"]),
            "tracking_interval_seconds": 0.5,
            "fallback": "configured-screen",
            "screen_index_role": "fallback-only",
        }

    def _is_live_tvsub(self, pid: int) -> bool:
        """PID 가 **지금도 우리 tvsub** 인지 확인한다.

        `os.kill(pid, 0)` 만으로는 두 가지를 못 거른다.
        1. 좀비 — 자식이 죽어도 reap 전까지 신호가 통해 계속 'running' 으로 보인다.
        2. PID 재사용 — 서버를 재시작한 뒤 낡은 상태 파일의 PID 를 OS 가 남에게 물려주면,
           stop_overlay 가 **무관한 사용자 프로세스에 SIGTERM 을 쏜다** (2026-08-15 실증).
        그래서 상태(Z 제외)와 실행 명령줄(tvsub 바이너리 경로)까지 같이 본다.
        """
        if pid <= 0:
            return False
        try:
            completed = subprocess.run(
                ["/bin/ps", "-p", str(pid), "-o", "state=,command="],
                capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        line = completed.stdout.strip()
        if completed.returncode != 0 or not line:
            return False
        state, _, command = line.partition(" ")
        if state.startswith("Z"):        # 좀비 = 이미 끝난 프로세스
            return False
        return str(self.binary) in command

    def overlay_status(self) -> dict:
        if not self.overlay_state_path.exists():
            return {"running": False, "pid": None}
        try:
            state = json.loads(self.overlay_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"running": False, "pid": None, "warning": "상태 파일 손상"}
        if state.get("mock"):
            return self._public_payload({**state, "running": bool(state.get("running"))})
        pid = int(state.get("pid") or 0)
        return self._public_payload({**state, "running": self._is_live_tvsub(pid)})

    def start_overlay(
        self,
        subtitle: str = "",
        store_id: str = "",
        font_family: str = "",
        font_size: float = 0,
        color: str = "",
        outline_width: float = -1,
        bottom_margin: float = -1,
    ) -> dict:
        current = self.overlay_status()
        if current["running"]:
            return {**current, "started": False, "message": "오버레이가 이미 실행 중입니다."}
        playback = self.now_playing()
        if not playback.get("is_tv_app"):
            bundle_id = playback.get("bundle_id") or "미확인"
            raise RuntimeError(
                f"TV.app 재생 소스를 확인할 수 없습니다(bundle ID: {bundle_id}). "
                "store_id를 지정해도 소스 검사는 우회하지 않습니다."
            )
        movie_id = self._movie_id(store_id or str(playback.get("store_id") or ""))
        if subtitle:
            self.load_subtitle(subtitle, movie_id)
        if any([font_family, font_size, color, outline_width >= 0, bottom_margin >= 0]):
            self.set_style(font_family=font_family, font_size=font_size, color=color,
                           outline_width=outline_width, bottom_margin=bottom_margin)
        config = self._movie_config(movie_id)
        if not config.get("subtitle"):
            raise ValueError("선택된 자막이 없습니다. load_subtitle을 먼저 호출하세요.")
        if self.mock:
            state = {"running": True, "mock": True, "pid": 4242, "store_id": movie_id,
                     "subtitle": config["subtitle"], "started_at": datetime.now(UTC).isoformat()}
            _atomic_json(self.overlay_state_path, state)
            return self._public_payload({**state, "started": True})
        if platform.system() != "Darwin":
            raise RuntimeError("NSPanel 오버레이는 macOS에서만 실행할 수 있습니다.")
        if not self.binary.is_file():
            raise FileNotFoundError("tvsub 바이너리가 없습니다. Mac에서 build.sh를 먼저 실행하세요: build/tvsub")
        log_path = self.root / "logs" / "tvsub-mcp-overlay.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as log_handle:
            process = subprocess.Popen(
                [str(self.binary), "run", "--movie", movie_id, "--sub", str(config["subtitle"])],
                cwd=self.root,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
            )
        time.sleep(0.25)
        if process.poll() is not None:
            raise RuntimeError(
                f"tvsub가 즉시 종료했습니다(exit {process.returncode}). "
                f"로그: {self._relative_path(log_path)}"
            )
        state = {"running": True, "mock": False, "pid": process.pid, "store_id": movie_id,
                 "subtitle": config["subtitle"], "log": str(log_path),
                 "started_at": datetime.now(UTC).isoformat()}
        _atomic_json(self.overlay_state_path, state)
        return self._public_payload({**state, "started": True})

    def stop_overlay(self) -> dict:
        state = self.overlay_status()
        if not state["running"]:
            return self._public_payload(
                {**state, "stopped": False, "message": "실행 중인 오버레이가 없습니다."}
            )
        escalated = False
        if not state.get("mock"):
            pid = int(state["pid"])
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            # SIGTERM 이 먹었는지 확인한다. 안 죽었는데 'stopped' 라고 답하면 상태 파일과
            # 실제가 어긋나고, 다음 start_overlay 가 조용히 오버레이를 2개 띄운다.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and self._is_live_tvsub(pid):
                time.sleep(0.1)
            if self._is_live_tvsub(pid):
                escalated = True
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and self._is_live_tvsub(pid):
                    time.sleep(0.1)
            if self._is_live_tvsub(pid):
                return self._public_payload({**state, "stopped": False,
                        "message": f"PID {pid}가 SIGTERM·SIGKILL 후에도 살아 있습니다. 직접 확인하세요."})
        state["running"] = False
        state["stopped_at"] = datetime.now(UTC).isoformat()
        _atomic_json(self.overlay_state_path, state)
        result = {**state, "stopped": True}
        if escalated:
            result["message"] = "SIGTERM 에 반응하지 않아 SIGKILL 로 종료했습니다."
        return self._public_payload(result)

    def calibrate_sync(
        self,
        spoken_text: str,
        subtitle_time: float = -1,
        store_id: str = "",
        actual_time: float = -1,
    ) -> dict:
        movie_id = self._movie_id(store_id)
        config = self._movie_config(movie_id)
        if not config.get("subtitle"):
            raise ValueError("선택된 자막이 없습니다.")
        document = load_subtitle(self.root / config["subtitle"], config.get("smiClass"))
        cue: Cue | None = None
        if subtitle_time >= 0:
            cue = min(document.cues, key=lambda item: abs(item.start - subtitle_time))
            if abs(cue.start - subtitle_time) > 2:
                raise ValueError("지정한 subtitle_time ±2초에 큐가 없습니다.")
        else:
            query = spoken_text.strip().casefold()
            if not query:
                raise ValueError("spoken_text 또는 subtitle_time이 필요합니다.")
            matches = [item for item in document.cues if query in item.flat_text.casefold()]
            if not matches:
                return {"anchor_recorded": False, "needs_selection": True, "reason": "matching-cue-not-found",
                        "query": spoken_text, "suggestion": "더 짧고 정확한 대사 일부나 subtitle_time을 지정하세요."}
            if len(matches) > 1:
                return {"anchor_recorded": False, "needs_selection": True, "reason": "multiple-cues",
                        "candidates": [{"subtitle_time": item.start, "text": item.flat_text} for item in matches[:20]]}
            cue = matches[0]
        playback = self.now_playing()
        if playback.get("sync_disagreement"):
            return {
                "anchor_recorded": False,
                "reason": "mediaremote-applescript-disagreement",
                "media_remote_location_ms": playback.get("media_remote_location_ms"),
                "apple_script_location_ms": playback.get("apple_script_location_ms"),
                "suggestion": (
                    "MediaRemote와 TV.app 위치가 일치하지 않아 앵커를 저장하지 않았습니다. "
                    "TV.app에서 pause→play로 재동기화한 뒤 다시 일시정지하고 재시도하세요."
                ),
            }
        if actual_time < 0:
            if playback["is_playing"]:
                raise ValueError("정확한 앵커를 위해 TV.app을 일시정지한 뒤 다시 호출하세요.")
            actual_time = float(playback["location_ms"]) / 1000
        anchors = list(config.get("anchors") or [])
        anchors = [item for item in anchors if abs(float(item["subTime"]) - cue.start) >= 20]
        anchors.append({
            "subTime": cue.start,
            "actualTime": actual_time,
            "note": spoken_text or cue.flat_text,
            "at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        })
        anchors.sort(key=lambda item: float(item["subTime"]))
        calibration = calculate(anchors)
        config.update({"storeId": movie_id, "anchors": anchors,
                       "scale": calibration.scale, "offset": calibration.offset})
        _atomic_json(self._movie_path(movie_id), config)
        return self._public_payload({
            "anchor_recorded": True,
            "store_id": movie_id,
            "matched_cue": {"subtitle_time": cue.start, "text": cue.flat_text},
            "actual_time": actual_time,
            "anchor_count": len(anchors),
            "scale": calibration.scale,
            "offset": calibration.offset,
            "warning": calibration.warning,
            "next_step": "영화 후반부에서 두 번째 앵커를 잡으세요." if len(anchors) == 1 else None,
        })

    def status(self) -> dict:
        try:
            playback: dict[str, Any] = self.now_playing()
            movie_id = str(playback.get("store_id") or "")
        except Exception as exc:
            playback = {"available": False, "error": self._public_error(exc)}
            movie_id = ""
        config = self._movie_config(movie_id) if movie_id else None
        settings = self._settings()
        return self._public_payload({
            "overlay": self.overlay_status(),
            "now_playing": playback,
            "subtitle": config.get("subtitle") if config else None,
            "provenance": self._provenance_for(config.get("subtitle") if config else None),
            "calibration": ({"scale": config.get("scale", 1.0), "offset": config.get("offset", 0.0),
                             "anchor_count": len(config.get("anchors") or [])} if config else None),
            "style": settings,
            "positioning": self._positioning_metadata(settings),
            "mock": self.mock,
        })
