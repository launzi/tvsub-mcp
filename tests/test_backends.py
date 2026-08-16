from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from tvsub_mcp.backends import (
    SelectedBackend, clear_probe_cache, extract_json, probe, run_cli_batch, select_backend,
)


def completed(args, stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


class BackendTests(unittest.TestCase):
    def setUp(self):
        clear_probe_cache()

    def test_extract_json_strips_fence_and_preamble(self):
        text = 'codex startup\n```json\n{"translations":[{"id":1,"text":"ok"}]}\n```\ntokens used'
        self.assertEqual(extract_json(text)["translations"][0]["text"], "ok")

    def test_api_wins_auto_without_cli_probe(self):
        run = unittest.mock.Mock()
        self.assertEqual(select_backend("auto", api_key="key", run=run).name, "api")
        run.assert_not_called()

    @patch("tvsub_mcp.backends.executable_for")
    def test_auto_skips_unauthenticated_claude_and_uses_codex(self, executable):
        executable.side_effect = lambda name: f"/bin/{name}"
        def run(command, **kwargs):
            if command[0] == "/bin/claude":
                return completed(command, stderr="not logged in", returncode=1)
            return completed(command, stdout="Logged in")
        self.assertEqual(select_backend("auto", api_key=None, run=run).name, "codex")

    def test_claude_fast_auth_status_avoids_model_round_trip(self):
        run = unittest.mock.Mock(return_value=completed([], stdout="Logged in"))
        self.assertTrue(probe("claude", "/bin/claude", run=run))
        self.assertEqual(run.call_args.args[0], ["/bin/claude", "auth", "status"])
        self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_claude_unsupported_status_falls_back_to_25_second_model_probe(self):
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            if command[1:3] == ["auth", "status"]:
                return completed(command, stderr="unknown command", returncode=1)
            return completed(command, stdout="OK")
        self.assertTrue(probe("claude", "/bin/claude", run=run, credentials_present=lambda: False))
        self.assertEqual(calls[-1][0], ["/bin/claude", "-p", "--output-format", "text"])
        self.assertEqual(calls[-1][1]["timeout"], 25)

    def test_claude_credential_signal_avoids_model_round_trip(self):
        run = unittest.mock.Mock(return_value=completed([], stderr="unknown command", returncode=1))
        self.assertTrue(probe(
            "claude", "/bin/claude", run=run, credentials_present=lambda: True,
        ))
        run.assert_called_once()

    def test_probe_result_is_cached_for_process_lifetime(self):
        run = unittest.mock.Mock(return_value=completed([], stdout="Logged in"))
        self.assertTrue(probe("claude", "/bin/claude", run=run))
        self.assertTrue(probe("claude", "/bin/claude", run=run))
        run.assert_called_once()

    @patch("tvsub_mcp.backends.executable_for", return_value="/bin/codex")
    def test_explicit_backend_skips_probe(self, _):
        run = unittest.mock.Mock()
        selected = select_backend("codex", api_key=None, run=run)
        self.assertEqual(selected.executable, "/bin/codex")
        run.assert_not_called()

    def test_cli_json_parse_retries_once(self):
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs["input"]))
            if len(calls) == 1:
                return completed(command, stdout="not json")
            return completed(command, stdout='preamble\n{"translations":[]}')
        payload, _ = run_cli_batch(SelectedBackend("codex", "/bin/codex"), "prompt", run=run)
        self.assertEqual(payload, {"translations": []})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], ["/bin/codex", "exec", "--skip-git-repo-check", "-"])
        self.assertEqual(calls[0][1], "prompt")
        self.assertIn("not valid JSON", calls[1][1])


if __name__ == "__main__":
    unittest.main()
