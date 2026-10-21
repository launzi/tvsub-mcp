"""Opt-out daily counters. Tool arguments, results and errors never enter this module."""
from __future__ import annotations

import fcntl
import json
import os
import platform
import re
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterator
from urllib import request

from . import __version__

DEFAULT_URL = "https://youngji.kim/api/tvsub/telemetry"
TOOLS = frozenset({
    "now_playing", "list_subtitles", "load_subtitle", "translate_subtitle",
    "set_glossary", "mark_reviewed", "start_overlay", "stop_overlay",
    "list_fonts", "set_style", "calibrate_sync", "status",
})
BACKENDS = frozenset({"api", "claude", "codex", "pro", "auto"})
FIELDS = (
    "install_id", "day", "tvsub_mcp_version", "macos_version", "python_minor",
    "tools", "backends",
)
HOW_TO_DISABLE = "Set TVSUB_TELEMETRY=0 (or false/off/no), or DO_NOT_TRACK=1."
MAX_COUNT = 2_147_483_647


def _disabled(mock: bool) -> bool:
    return (
        mock
        or os.getenv("TVSUB_TELEMETRY", "").strip().lower() in {"0", "false", "off", "no"}
        or os.getenv("DO_NOT_TRACK") == "1"
        or os.getenv("TVSUB_MCP_MOCK") == "1"
        or "--mock" in sys.argv
        or "PYTEST_CURRENT_TEST" in os.environ
        or "pytest" in sys.modules
        or "unittest" in sys.modules
    )


def _day(value: object) -> str | None:
    if isinstance(value, str):
        try:
            if date.fromisoformat(value).isoformat() == value:
                return value
        except ValueError:
            pass
    return None


def _counts(value: object, allowed: frozenset[str]) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {name: min(count, MAX_COUNT) for name, count in value.items()
            if name in allowed and type(count) is int and count > 0}


def _clean_state(raw: object) -> dict:
    """Rebuild from an allowlist, including when the local file is malformed."""
    raw = raw if isinstance(raw, dict) else {}
    try:
        install_id = uuid.UUID(raw.get("install_id", ""))
        if install_id.version != 4:
            raise ValueError("not a random installation ID")
    except (ValueError, TypeError, AttributeError):
        # Do not attach untrusted counters to a new identity.
        raw = {}
        install_id = uuid.uuid4()
    days = {}
    counters = raw.get("days", {})
    if isinstance(counters, dict):
        for day, entry in counters.items():
            if _day(day) and isinstance(entry, dict):
                tools = _counts(entry.get("tools"), TOOLS)
                if tools:
                    days[day] = {"tools": tools, "backends": _counts(entry.get("backends"), BACKENDS)}
    return {
        "install_id": str(install_id), "days": days,
        "last_sent_day": _day(raw.get("last_sent_day")),
        "last_attempt_day": _day(raw.get("last_attempt_day")),
        "notice_shown": raw.get("notice_shown") is True,
    }


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # One request to the configured collector; never forward to another URL.
        return None


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


class Telemetry:
    def __init__(
        self, path: Path | None = None, *, mock: bool = False,
        today: Callable[[], date] | None = None,
    ) -> None:
        self.path = path if path is not None else (
            Path.home() / "Library" / "Application Support" / "tvsub" / "telemetry.json"
        )
        self.mock = mock
        # UTC days, matching the collector, which only accepts days already complete in UTC.
        self._today = today or _utc_today
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return not _disabled(self.mock)

    def status(self) -> dict:
        return {"enabled": self.enabled, "how_to_disable": HOW_TO_DISABLE,
                "what_is_sent": list(FIELDS)}

    @contextmanager
    def _state(self) -> Iterator[dict]:
        # Multiple MCP clients may launch separate servers for the same install.
        # Lock a stable sibling file: telemetry.json itself is atomically replaced.
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(fd, "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    try:
                        raw = json.loads(self.path.read_text(encoding="utf-8"))
                    except (FileNotFoundError, ValueError, UnicodeError):
                        raw = {}
                    state = _clean_state(raw)
                    yield state
                    temporary = None
                    try:
                        with tempfile.NamedTemporaryFile(
                            mode="w", encoding="utf-8", dir=self.path.parent,
                            prefix=".telemetry-", suffix=".tmp", delete=False,
                        ) as handle:
                            temporary = Path(handle.name)
                            json.dump(state, handle, separators=(",", ":"))
                            handle.write("\n")
                        temporary.replace(self.path)
                    finally:
                        if temporary is not None:
                            temporary.unlink(missing_ok=True)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def start(self) -> None:
        self._update()

    def record(self, tool: str, backend: str = "") -> None:
        if tool in TOOLS:
            self._update(tool, backend)

    def _update(self, tool: str | None = None, backend: str = "") -> None:
        if not self.enabled:
            return
        try:
            payload = None
            show_notice = False
            today = self._today().isoformat()
            with self._state() as state:
                if not self.enabled:
                    return
                if not state["notice_shown"]:
                    state["notice_shown"] = True
                    show_notice = True
                if tool is not None:
                    entry = state["days"].setdefault(today, {"tools": {}, "backends": {}})
                    entry["tools"][tool] = min(entry["tools"].get(tool, 0) + 1, MAX_COUNT)
                    backend = backend or "auto"
                    if tool == "translate_subtitle" and backend in BACKENDS:
                        entry["backends"][backend] = min(entry["backends"].get(backend, 0) + 1, MAX_COUNT)
                past = sorted(day for day in state["days"] if day < today)
                # Persist the claim before starting the thread. Even failures and
                # process restarts cannot cause more than one POST per local day.
                if past and (state["last_attempt_day"] or "") < today \
                        and (state["last_sent_day"] or "") < today:
                    payload = self._payload(state, past[0])
                    state["last_attempt_day"] = today
            if show_notice:
                print(
                    "[tvsub-mcp] Anonymous usage statistics are enabled: random install ID, "
                    "day, app/macOS/Python versions and tool/backend counts, at most once daily "
                    "to youngji.kim (or TVSUB_TELEMETRY_URL). No subtitles or content details. "
                    + HOW_TO_DISABLE, file=sys.stderr, flush=True,
                )
            if payload is not None:
                threading.Thread(target=self._send, args=(payload,),
                                 name="tvsub-telemetry", daemon=True).start()
        except Exception:
            # Storage, permissions, stderr and thread failures must not affect tools.
            pass

    @staticmethod
    def _payload(state: dict, day: str) -> dict:
        macos = platform.mac_ver()[0]
        macos = ".".join(macos.split(".")[:2]) if re.fullmatch(r"\d+\.\d+(?:\.\d+)*", macos) else "unknown"
        return {
            "install_id": state["install_id"], "day": day,
            "tvsub_mcp_version": __version__, "macos_version": macos,
            "python_minor": f"{sys.version_info.major}.{sys.version_info.minor}",
            "tools": _counts(state["days"][day]["tools"], TOOLS),
            "backends": _counts(state["days"][day]["backends"], BACKENDS),
        }

    def _send(self, payload: dict) -> None:
        try:
            if not self.enabled:
                return
            req = request.Request(
                os.getenv("TVSUB_TELEMETRY_URL", DEFAULT_URL),
                data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with request.build_opener(_NoRedirect()).open(req, timeout=3) as response:
                if not 200 <= response.status < 300:
                    return
            if not self.enabled:
                return
            with self._state() as state:
                if state["install_id"] != payload["install_id"]:
                    return
                state["last_sent_day"] = self._today().isoformat()
                sent = {"tools": payload["tools"], "backends": payload["backends"]}
                if state["days"].get(payload["day"]) == sent:
                    del state["days"][payload["day"]]
        except Exception:
            # Never log network errors: they can contain URLs or response bodies.
            pass
