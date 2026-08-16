from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tvsub_mcp.server import KEYCHAIN_SERVICE, _load_env, _load_keychain_key


class ServerEnvironmentTests(unittest.TestCase):
    def test_keychain_fallback_uses_swift_service_name(self) -> None:
        completed = Mock(returncode=0, stdout="sk-ant-fixture\n")
        with patch.dict(os.environ, {}, clear=True), \
             patch("tvsub_mcp.server.sys.platform", "darwin"), \
             patch("tvsub_mcp.server.subprocess.run", return_value=completed) as run:
            _load_keychain_key()
            self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "sk-ant-fixture")
            self.assertEqual(run.call_args.args[0], [
                "security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w",
            ])

    def test_keychain_failure_leaves_environment_unset(self) -> None:
        completed = Mock(returncode=44, stdout="")
        with patch.dict(os.environ, {}, clear=True), \
             patch("tvsub_mcp.server.sys.platform", "darwin"), \
             patch("tvsub_mcp.server.subprocess.run", return_value=completed):
            _load_keychain_key()
            self.assertNotIn("ANTHROPIC_API_KEY", os.environ)

    def test_load_env_reads_only_explicit_file_and_allowed_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            explicit = root / "project" / ".env"
            explicit.parent.mkdir()
            explicit.write_text(
                "ANTHROPIC_API_KEY=fixture-key\nUNRELATED_SECRET=must-not-load\n",
                encoding="utf-8",
            )
            (root / ".env").write_text("ANTHROPIC_API_KEY=parent-key\n", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                _load_env(explicit)
                self.assertEqual(os.environ.get("ANTHROPIC_API_KEY"), "fixture-key")
                self.assertNotIn("UNRELATED_SECRET", os.environ)

    def test_load_env_does_not_search_parent_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text("ANTHROPIC_API_KEY=parent-key\n", encoding="utf-8")
            missing_explicit = root / "project" / ".env"
            with patch.dict(os.environ, {}, clear=True):
                _load_env(missing_explicit)
                self.assertNotIn("ANTHROPIC_API_KEY", os.environ)


if __name__ == "__main__":
    unittest.main()
