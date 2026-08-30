from __future__ import annotations

import os
import unittest
from unittest.mock import Mock, patch

from tvsub_mcp.server import KEYCHAIN_SERVICE, _load_keychain_key


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


if __name__ == "__main__":
    unittest.main()
