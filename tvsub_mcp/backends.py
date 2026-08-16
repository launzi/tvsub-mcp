from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

BACKENDS = ("auto", "api", "claude", "codex")
Run = Callable[..., subprocess.CompletedProcess[str]]
_PROBE_CACHE: dict[tuple[str, str], bool] = {}


@dataclass(frozen=True)
class SelectedBackend:
    name: str
    executable: str | None = None
    model: str | None = None


def _executable(name: str, fallbacks: tuple[str, ...]) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for raw in fallbacks:
        path = Path(raw).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def executable_for(name: str) -> str | None:
    if name == "claude":
        return _executable("claude", ("~/.local/bin/claude", "/usr/local/bin/claude"))
    if name == "codex":
        return _executable("codex", ("/opt/homebrew/bin/codex", "~/.npm-global/bin/codex"))
    return None


def _claude_credentials_present(*, run: Run = subprocess.run) -> bool:
    if sys.platform == "darwin":
        try:
            result = run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if result.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        payload = json.loads(Path("~/.claude.json").expanduser().read_text(encoding="utf-8"))
        return bool(payload.get("oauthAccount"))
    except (OSError, ValueError, TypeError):
        return False


def _unsupported_auth_status(result: subprocess.CompletedProcess[str]) -> bool:
    output = f"{result.stdout}\n{result.stderr}".lower()
    return any(marker in output for marker in (
        "unknown command", "unrecognized command", "invalid command", "no such command",
    ))


def clear_probe_cache() -> None:
    _PROBE_CACHE.clear()


def probe(name: str, executable: str, *, run: Run = subprocess.run,
          credentials_present: Callable[[], bool] | None = None) -> bool:
    cache_key = (name, executable)
    if cache_key in _PROBE_CACHE:
        return _PROBE_CACHE[cache_key]
    try:
        if name == "codex":
            result = run([executable, "login", "status"], capture_output=True, text=True,
                         timeout=5, check=False)
            available = result.returncode == 0
        else:
            try:
                status = run([executable, "auth", "status"], capture_output=True, text=True,
                             timeout=5, check=False)
            except (OSError, subprocess.SubprocessError):
                status = None
            if status is not None and status.returncode == 0:
                available = True
            elif status is not None and not _unsupported_auth_status(status):
                available = False
            elif (credentials_present or (lambda: _claude_credentials_present(run=run)))():
                available = True
            else:
                result = run([executable, "-p", "--output-format", "text"], input="Reply only: OK",
                             capture_output=True, text=True, timeout=25, check=False)
                available = result.returncode == 0 and "OK" in result.stdout
    except (OSError, subprocess.SubprocessError):
        available = False
    _PROBE_CACHE[cache_key] = available
    return available


def select_backend(requested: str | None = None, *, api_key: str | None = None,
                   run: Run = subprocess.run) -> SelectedBackend:
    value = (requested or os.getenv("TVSUB_TRANSLATE_BACKEND", "auto")).strip().lower()
    if value not in BACKENDS:
        raise ValueError("backend는 auto, api, claude, codex 중 하나여야 합니다.")
    if value == "api":
        if not api_key:
            raise RuntimeError("api 백엔드에는 ANTHROPIC_API_KEY가 필요합니다.")
        return SelectedBackend("api", model="claude-sonnet-5")
    if value in {"claude", "codex"}:
        executable = executable_for(value)
        if not executable:
            raise RuntimeError(f"{value} CLI를 찾지 못했습니다. PATH와 표준 설치 경로를 확인하세요.")
        # Explicit selection intentionally skips authentication probing.
        return SelectedBackend(value, executable=executable)
    if api_key:
        return SelectedBackend("api", model="claude-sonnet-5")
    for name in ("claude", "codex"):
        executable = executable_for(name)
        if executable and probe(name, executable, run=run):
            return SelectedBackend(name, executable=executable)
    raise RuntimeError(
        "사용 가능한 번역 백엔드가 없습니다. ANTHROPIC_API_KEY를 설정하거나, "
        "Claude 구독용 claude CLI 또는 ChatGPT 구독용 codex CLI를 설치하고 로그인하세요."
    )


def extract_json(text: str) -> dict:
    """Extract the first valid JSON object from fences or CLI log preambles."""
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("CLI 출력에서 JSON 객체를 찾지 못했습니다.")


def cli_prompt(system: str, message: str) -> str:
    return f"""{system}

Return JSON only, with no Markdown code fence and no text before or after it.
Do not create, modify, or read any files. Work only from the subtitle data below.
Required shape: {{"translations":[{{"id":1,"text":"translated text"}}]}}

{message}
"""


def run_cli_batch(backend: SelectedBackend, prompt: str, *, run: Run = subprocess.run) -> tuple[dict, str | None]:
    if backend.name == "claude":
        command = [backend.executable or "claude", "-p", "--output-format", "text"]
    elif backend.name == "codex":
        command = [backend.executable or "codex", "exec", "--skip-git-repo-check", "-"]
    else:
        raise ValueError(f"CLI 백엔드가 아닙니다: {backend.name}")
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            result = run(command, input=prompt, capture_output=True, text=True, timeout=240, check=False)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()[-1200:]
                raise RuntimeError(f"종료 코드 {result.returncode}: {detail}")
            return extract_json(result.stdout), None
        except (ValueError, OSError, subprocess.SubprocessError, RuntimeError) as exc:
            last_error = exc
            if attempt == 0:
                prompt += "\nPrevious response was not valid JSON. Return the required JSON object only."
    raise RuntimeError(f"{backend.name} CLI 배치가 2회 실패했습니다: {last_error}") from last_error
