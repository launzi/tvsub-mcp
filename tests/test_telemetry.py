from __future__ import annotations

import asyncio
import inspect
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from tvsub_mcp import __version__, telemetry
from tvsub_mcp.server import build_server, parse_args


REAL_THREAD = threading.Thread
TODAY = date(2026, 10, 22)
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()
FORBIDDEN = {
    "subtitle": "PRIVATE_SUBTITLE_TEXT", "title": "PRIVATE_MOVIE_TITLE",
    "content_id": "PRIVATE_CONTENT_ID", "filename": "private-subtitle.srt",
    "path": "/private/subtitles/private-subtitle.srt",
    "glossary": "PRIVATE_GLOSSARY", "api_key": "PRIVATE_API_KEY",
    "error": "PRIVATE_RAW_ERROR", "ip": "PRIVATE_IP_ADDRESS",
}


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "tvsub" / "telemetry.json"
        # Unit simulations explicitly mock the runtime guard. No production
        # switch can enable telemetry inside pytest/unittest or mock mode.
        self.guard = patch.object(telemetry, "_disabled", return_value=False)
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        opener = patch.object(telemetry.request, "build_opener")
        self.opener = opener.start().return_value
        self.addCleanup(opener.stop)
        self.opener.open.return_value.__enter__.return_value.status = 204
        self.threads = []

        def thread(*args, **kwargs):
            worker = REAL_THREAD(*args, **kwargs)
            self.threads.append(worker)
            return worker

        thread_patch = patch.object(telemetry.threading, "Thread", side_effect=thread)
        thread_patch.start()
        self.addCleanup(thread_patch.stop)
        self.stderr = io.StringIO()
        stderr_patch = patch.object(sys, "stderr", self.stderr)
        stderr_patch.start()
        self.addCleanup(stderr_patch.stop)
        self.addCleanup(self.drain)
        self.today = TODAY
        self.client = self.new_client()

    def new_client(self):
        return telemetry.Telemetry(self.path, today=lambda: self.today)

    def drain(self):
        for worker in self.threads:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive(), "telemetry worker did not finish")

    def state(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def seed(self, days=None, **extra):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "install_id": str(uuid.uuid4()), "notice_shown": True,
            "days": days if days is not None else {
                YESTERDAY: {"tools": {"status": 2}, "backends": {}},
            }, "last_sent_day": None, "last_attempt_day": None,
        }
        state.update(extra)
        self.path.write_text(json.dumps(state), encoding="utf-8")
        return state

    def sent(self):
        req = self.opener.open.call_args.args[0]
        return json.loads(req.data)

    def test_install_id_persists_and_first_notice_is_stderr_once(self):
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.client.start()
            first = self.state()["install_id"]
            self.new_client().start()
            self.client.record("status")
        self.assertEqual(uuid.UUID(first).version, 4)
        self.assertEqual(self.state()["install_id"], first)
        self.assertEqual(self.stderr.getvalue().count("Anonymous usage statistics"), 1)
        self.assertIn("TVSUB_TELEMETRY=0", self.stderr.getvalue())
        self.assertEqual(stdout.getvalue(), "")
        self.opener.open.assert_not_called()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_all_tools_and_only_known_backend_names_are_counted(self):
        for name in telemetry.TOOLS:
            self.client.record(name)
        for backend in sorted(telemetry.BACKENDS):
            self.client.record("translate_subtitle", backend)
        self.client.record("translate_subtitle", FORBIDDEN["api_key"])
        self.client.record(FORBIDDEN["subtitle"])
        entry = self.state()["days"][TODAY.isoformat()]
        self.assertEqual(set(entry["tools"]), telemetry.TOOLS)
        self.assertEqual(entry["tools"]["translate_subtitle"], 7)
        self.assertEqual(entry["backends"], {
            "auto": 2, "api": 1, "claude": 1, "codex": 1, "pro": 1,
        })
        self.opener.open.assert_not_called()
        for value in FORBIDDEN.values():
            self.assertNotIn(value, self.path.read_text())

    def test_startup_sends_only_oldest_completed_day_and_preserves_today(self):
        earlier = (TODAY - timedelta(days=2)).isoformat()
        self.seed(days={
            earlier: {"tools": {"status": 1}, "backends": {}},
            YESTERDAY: {"tools": {"status": 2}, "backends": {}},
            TODAY.isoformat(): {"tools": {"status": 3}, "backends": {}},
        })
        self.client.start()
        self.drain()
        self.assertEqual(self.sent()["day"], earlier)
        self.assertEqual(set(self.state()["days"]), {YESTERDAY, TODAY.isoformat()})
        self.assertEqual(self.state()["last_sent_day"], TODAY.isoformat())
        self.client.record("status")
        self.new_client().start()
        self.drain()
        self.opener.open.assert_called_once()
        self.today += timedelta(days=1)
        self.new_client().start()
        self.drain()
        self.assertEqual(self.opener.open.call_count, 2)
        self.assertEqual(self.sent()["day"], YESTERDAY)

    def test_tool_call_after_midnight_triggers_background_send(self):
        self.client.record("list_fonts")
        self.today += timedelta(days=1)
        self.client.record("status")
        self.drain()
        self.assertEqual(self.sent()["tools"], {"list_fonts": 1})
        self.assertEqual(self.state()["days"][self.today.isoformat()]["tools"], {"status": 1})

    def test_send_whitelist_even_with_tainted_disk_and_environment(self):
        self.seed(days={
            YESTERDAY: {
                "tools": {"translate_subtitle": 4, FORBIDDEN["title"]: 7,
                          "status": "PRIVATE_RAW_ERROR", "load_subtitle": True},
                "backends": {"auto": 2, FORBIDDEN["api_key"]: 1}, **FORBIDDEN,
            }, FORBIDDEN["path"]: {"tools": {"status": 1}},
        }, **FORBIDDEN)
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": FORBIDDEN["api_key"]}), \
                patch.object(telemetry.platform, "mac_ver", return_value=("15.6.1", (), "")):
            self.client.start()
            self.drain()
        payload = self.sent()
        self.assertEqual(set(payload), set(telemetry.FIELDS))
        self.assertEqual(payload["tools"], {"translate_subtitle": 4})
        self.assertEqual(payload["backends"], {"auto": 2})
        self.assertEqual(payload["tvsub_mcp_version"], __version__)
        self.assertEqual(payload["macos_version"], "15.6")
        self.assertEqual(payload["python_minor"], f"{sys.version_info.major}.{sys.version_info.minor}")
        req = self.opener.open.call_args.args[0]
        self.assertEqual(req.full_url, telemetry.DEFAULT_URL)
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(self.opener.open.call_args.kwargs, {"timeout": 3})
        self.assertLessEqual(len(req.data), 2048)
        for key, value in FORBIDDEN.items():
            self.assertNotIn(key, payload)
            self.assertNotIn(value, req.data.decode())
            self.assertNotIn(value, self.path.read_text())
            self.assertNotIn(value, self.stderr.getvalue())

    def test_endpoint_override(self):
        self.seed()
        with patch.dict(os.environ, {"TVSUB_TELEMETRY_URL": "https://collector.invalid/counts"}):
            self.client.start()
            self.drain()
        self.assertEqual(self.opener.open.call_args.args[0].full_url, "https://collector.invalid/counts")

    def test_failures_are_silent_retain_counters_and_retry_next_day(self):
        for error in (TimeoutError(FORBIDDEN["error"]), URLError(FORBIDDEN["path"]),
                      HTTPError(telemetry.DEFAULT_URL, 500, FORBIDDEN["error"], {}, None)):
            with self.subTest(error=type(error).__name__):
                self.seed()
                self.opener.open.reset_mock()
                self.opener.open.side_effect = error
                self.client.start()
                self.drain()
                self.new_client().start()
                self.client.record("status")
                self.drain()
                self.opener.open.assert_called_once()
                self.assertIn(YESTERDAY, self.state()["days"])
                self.assertIsNone(self.state()["last_sent_day"])
                self.assertEqual(self.stderr.getvalue(), "")
                self.today += timedelta(days=1)
                self.opener.open.side_effect = None
                self.new_client().start()
                self.drain()
                self.assertEqual(self.opener.open.call_count, 2)
                self.assertNotIn(YESTERDAY, self.state()["days"])

    def test_non_success_response_does_not_acknowledge_counters(self):
        self.seed()
        self.opener.open.return_value.__enter__.return_value.status = 503
        self.client.start()
        self.drain()
        self.assertIn(YESTERDAY, self.state()["days"])
        self.assertIsNone(self.state()["last_sent_day"])

    def test_redirect_is_not_followed(self):
        handler = telemetry._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://other.invalid"))

    def test_slow_network_does_not_block_calls_or_counter_writes(self):
        self.seed()
        entered, release = threading.Event(), threading.Event()

        def slow(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test release missing")
            return Mock(__enter__=Mock(return_value=Mock(status=204)), __exit__=Mock())

        self.opener.open.side_effect = slow
        try:
            self.client.start()
            self.assertTrue(entered.wait(2))
            self.client.record("status")
            self.assertEqual(self.state()["days"][TODAY.isoformat()]["tools"], {"status": 1})
            self.assertTrue(all(worker.daemon for worker in self.threads))
        finally:
            release.set()
            self.drain()

    def test_concurrent_instances_preserve_counts_and_claim_once(self):
        self.seed()
        clients = [self.new_client() for _ in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: clients[i % 4].record("status"), range(80)))
        self.drain()
        self.assertEqual(self.state()["days"][TODAY.isoformat()]["tools"], {"status": 80})
        self.opener.open.assert_called_once()

    def test_corrupt_file_recovers_without_forwarding_raw_data(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(FORBIDDEN["error"])
        self.client.record("status")
        self.assertEqual(uuid.UUID(self.state()["install_id"]).version, 4)
        self.assertEqual(self.state()["days"][TODAY.isoformat()]["tools"], {"status": 1})
        self.opener.open.assert_not_called()

    def test_storage_and_thread_failures_are_harmless(self):
        with patch.object(Path, "mkdir", side_effect=PermissionError(FORBIDDEN["path"])):
            self.client.start()
            self.client.record("status")
        self.assertFalse(self.path.exists())
        self.seed()
        with patch.object(telemetry.threading, "Thread", side_effect=RuntimeError(FORBIDDEN["error"])):
            self.client.start()
        self.assertIn(YESTERDAY, self.state()["days"])
        self.assertEqual(self.stderr.getvalue(), "")

    def test_atomic_save_failure_leaves_previous_state_readable(self):
        before = self.seed()
        with patch.object(Path, "replace", side_effect=OSError(FORBIDDEN["error"])):
            self.client.record("status")
        self.assertEqual(self.state(), before)
        self.assertEqual(list(self.path.parent.glob(".telemetry-*.tmp")), [])
        self.opener.open.assert_not_called()

    def test_mcp_all_calls_count_preserve_metadata_and_keep_private_values_out(self):
        service = Mock(mock=False)
        for name in telemetry.TOOLS:
            getattr(service, name).return_value = dict(FORBIDDEN)
        server = build_server(service, telemetry=self.client)
        arguments = {
            "load_subtitle": {"subtitle": FORBIDDEN["path"], "store_id": FORBIDDEN["content_id"]},
            "translate_subtitle": {"subtitle": FORBIDDEN["path"], "glossary": FORBIDDEN["glossary"], "backend": "codex"},
            "set_glossary": {"subtitle": FORBIDDEN["path"], "glossary": dict(FORBIDDEN)},
            "mark_reviewed": {"subtitle": FORBIDDEN["path"]},
            "calibrate_sync": {"spoken_text": FORBIDDEN["subtitle"]},
        }
        for name in telemetry.TOOLS:
            asyncio.run(server.call_tool(name, arguments.get(name, {})))
            function = server._tool_manager.get_tool(name).fn
            self.assertEqual(inspect.signature(function), inspect.signature(function.__wrapped__))
            self.assertEqual(function.__annotations__, function.__wrapped__.__annotations__)
        entry = self.state()["days"][TODAY.isoformat()]
        self.assertEqual(entry["tools"], dict.fromkeys(telemetry.TOOLS, 1))
        self.assertEqual(entry["backends"], {"codex": 1})
        status = server._tool_manager.get_tool("status").fn()
        self.assertEqual(status["telemetry"], self.client.status())
        self.assertEqual({k: status[k] for k in FORBIDDEN}, FORBIDDEN)
        service.translate_subtitle.side_effect = ValueError(FORBIDDEN["error"])
        translate = server._tool_manager.get_tool("translate_subtitle").fn
        with self.assertRaisesRegex(ValueError, FORBIDDEN["error"]):
            translate(FORBIDDEN["path"])
        self.assertEqual(self.state()["days"][TODAY.isoformat()]["backends"], {"codex": 1, "auto": 1})
        self.today += timedelta(days=1)
        self.client.start()
        self.drain()
        for value in FORBIDDEN.values():
            self.assertNotIn(value, json.dumps(self.sent()))
            self.assertNotIn(value, self.path.read_text())


class DisableTests(unittest.TestCase):
    def test_disable_switches_never_touch_disk_or_network_even_with_pending_data(self):
        cases = [
            *[({"TVSUB_TELEMETRY": value}, False, []) for value in ("0", "false", "off", "no", "FALSE", " OFF ")],
            ({"DO_NOT_TRACK": "1"}, False, []),
            ({"TVSUB_MCP_MOCK": "1"}, False, []),
            ({}, True, []), ({}, False, ["tvsub-mcp", "--mock"]),
            ({"PYTEST_CURRENT_TEST": "test_telemetry"}, False, []),
        ]
        # Hide the runner modules to prove each other switch works independently.
        with patch.dict(sys.modules):
            sys.modules.pop("pytest", None)
            sys.modules.pop("unittest", None)
            for env, mock, argv in cases:
                with self.subTest(env=env, mock=mock, argv=argv), \
                        patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", argv), \
                        patch.object(telemetry.Telemetry, "_state") as state, \
                        patch.object(telemetry.request, "build_opener") as opener, \
                        patch.object(telemetry.threading, "Thread") as thread:
                    client = telemetry.Telemetry(Path("unused-telemetry.json"), mock=mock)
                    client.start()
                    client.record("status")
                    client._send({})
                    self.assertFalse(client.status()["enabled"])
                    state.assert_not_called()
                    opener.assert_not_called()
                    thread.assert_not_called()

    def test_pytest_and_unittest_disable_even_during_collection(self):
        for runner in ("pytest", "unittest"):
            with self.subTest(runner=runner), patch.dict(os.environ, {}, clear=True), \
                    patch.object(sys, "argv", []), patch.dict(sys.modules):
                sys.modules.pop("pytest", None)
                sys.modules.pop("unittest", None)
                sys.modules[runner] = Mock()
                self.assertFalse(telemetry.Telemetry().enabled)

    def test_default_enabled_and_mock_cli_wiring(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(sys, "argv", ["tvsub-mcp"]), patch.dict(sys.modules):
            sys.modules.pop("pytest", None)
            sys.modules.pop("unittest", None)
            self.assertTrue(telemetry.Telemetry().enabled)
            self.assertTrue(parse_args(["--mock"]).mock)
            with patch.dict(os.environ, {"TVSUB_MCP_MOCK": "1"}):
                self.assertTrue(parse_args([]).mock)
        with patch.object(telemetry.Telemetry, "_state") as state:
            # Status itself stays usable while telemetry is disabled.
            service = Mock(mock=True)
            service.status.return_value = {}
            result = build_server(service)._tool_manager.get_tool("status").fn()
            self.assertFalse(result["telemetry"]["enabled"])
            state.assert_not_called()


if __name__ == "__main__":
    unittest.main()
