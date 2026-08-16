#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import selectors
import subprocess
import sys
import tempfile
from pathlib import Path


SRT = """1
00:01:05,000 --> 00:01:07,000
First line

2
00:10:00,000 --> 00:10:02,000
Late line

"""


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    python = Path(os.environ.get("TVSUB_TEST_PYTHON", sys.executable))
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "subtitles").mkdir()
        (root / "subtitles" / "sample.en.srt").write_text(SRT, encoding="utf-8")
        process = subprocess.Popen(
            [str(python), "-u", str(project / "run_server.py"), "--mock", "--tvsub-root", str(root)],
            cwd=project,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env={
                **os.environ,
                "TVSUB_MOCK_AS_ELAPSED": "300.1",
                "TVSUB_MOCK_AS_STATE": "paused",
            },
        )
        assert process.stdin and process.stdout and process.stderr
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        request_id = 0

        def send(method: str, params: dict | None = None, *, notification: bool = False) -> dict | None:
            nonlocal request_id
            payload = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                payload["params"] = params
            if not notification:
                request_id += 1
                payload["id"] = request_id
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()
            if notification:
                return None
            while True:
                if not selector.select(timeout=15):
                    raise TimeoutError(f"응답 시간 초과: {method}; stderr={process.stderr.read(1000)}")
                line = process.stdout.readline()
                if not line:
                    raise RuntimeError(f"서버 조기 종료: {process.poll()}; stderr={process.stderr.read()}")
                if str(root) in line:
                    raise AssertionError(f"absolute tvsub root leaked in MCP response: {line}")
                response = json.loads(line)
                if response.get("id") == request_id:
                    if "error" in response:
                        raise RuntimeError(f"{method}: {response['error']}")
                    return response

        init = send("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "tvsub-raw-smoke", "version": "1.0"},
        })
        assert init and init["result"]["serverInfo"]["name"] == "tvsub"
        send("notifications/initialized", notification=True)
        listed = send("tools/list")
        names = [item["name"] for item in listed["result"]["tools"]]
        expected = {
            "now_playing", "list_subtitles", "load_subtitle", "translate_subtitle",
            "set_glossary", "mark_reviewed",
            "start_overlay", "stop_overlay", "list_fonts", "set_style", "calibrate_sync", "status",
        }
        if set(names) != expected:
            raise AssertionError(f"tool catalog mismatch: {names}")
        catalog = {item["name"]: item for item in listed["result"]["tools"]}
        for name in ("set_style", "status"):
            if "창" not in catalog[name].get("description", ""):
                raise AssertionError(f"{name} window-relative description regression: {catalog[name]}")

        calls = [
            ("now_playing", {}),
            ("list_subtitles", {}),
            ("load_subtitle", {"subtitle": "sample.en.srt"}),
            ("translate_subtitle", {"subtitle": "sample.en.srt", "target_language": "ja", "dry_run": True}),
            ("list_fonts", {"language": "ja"}),
            ("set_style", {
                "font_family": "Noto Sans CJK KR", "font_size": 52,
                "bottom_margin": 0.09, "language": "ja",
            }),
            ("start_overlay", {}),
            ("calibrate_sync", {"spoken_text": "First line", "actual_time": 70}),
            ("status", {}),
            ("stop_overlay", {}),
        ]
        outcomes = {}
        for name, arguments in calls:
            response = send("tools/call", {"name": name, "arguments": arguments})
            result = response["result"]
            if result.get("isError"):
                raise RuntimeError(f"tool {name} failed: {result}")
            outcomes[name] = "PASS"
            if name == "now_playing":
                content = json.loads(result["content"][0]["text"])
                if not content.get("sync_disagreement") or content.get("location_ms") != 300_100:
                    raise AssertionError(f"AppleScript correction regression: {content}")
            if name == "calibrate_sync":
                content = json.loads(result["content"][0]["text"])
                if content.get("anchor_recorded") is not False \
                   or content.get("reason") != "mediaremote-applescript-disagreement":
                    raise AssertionError(f"calibrate_sync fail-closed regression: {content}")
            if name in {"set_style", "status"}:
                content = json.loads(result["content"][0]["text"])
                positioning = content.get("positioning") or {}
                if positioning.get("reference") != "tv-video-window" \
                   or positioning.get("bottom_margin_ratio") != 0.09 \
                   or positioning.get("fallback") != "configured-screen":
                    raise AssertionError(f"{name} positioning contract regression: {content}")
        process.stdin.close()
        process.wait(timeout=10)
        stderr = process.stderr.read()
        if str(root) in stderr:
            raise AssertionError(f"absolute tvsub root leaked in stderr: {stderr}")
        print(json.dumps({"initialize": "PASS", "tools_list": names, "tool_calls": outcomes},
                         ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
