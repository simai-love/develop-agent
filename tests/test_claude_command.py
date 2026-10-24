"""Check Claude Code command options, model aliases, and error events."""

import json
import unittest
from pathlib import Path

from app.agent import SKILL_ROOT, claude_command, cli_error
from app.model_catalog import claude_models


SCHEMA = SKILL_ROOT / "references" / "plan-response.schema.json"


class ClaudeCommandTests(unittest.TestCase):
    """Verify Claude Code gets the permissions that match the Codex sandbox."""

    def test_workspace_write_uses_required_sandbox(self):
        """Return after checking sandbox settings, directories, and model flags."""
        command = claude_command("claude", "opus", SCHEMA, [Path("/tasks/a"), SKILL_ROOT], "workspace-write")
        settings = json.loads(command[command.index("--settings") + 1])["sandbox"]
        self.assertTrue(settings["enabled"] and settings["failIfUnavailable"] and settings["autoAllowBashIfSandboxed"])
        self.assertFalse(settings["allowUnsandboxedCommands"])
        self.assertIn("pypi.org", settings["network"]["allowedDomains"])
        self.assertEqual(command[command.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(command[command.index("--add-dir") + 1:command.index("--add-dir") + 3], ["/tasks/a", str(SKILL_ROOT)])
        self.assertEqual(command[command.index("--model") + 1], "opus")
        self.assertNotIn("--dangerously-skip-permissions", command)

    def test_full_access_skips_permission_checks(self):
        """Return after checking full access omits the sandbox and default model."""
        command = claude_command("claude", "", SCHEMA, [SKILL_ROOT], "danger-full-access")
        self.assertIn("--dangerously-skip-permissions", command)
        self.assertNotIn("--settings", command)
        self.assertNotIn("--model", command)
        self.assertEqual(json.loads(command[-1]), json.loads(SCHEMA.read_text(encoding="utf-8")))

    def test_error_result_and_aliases(self):
        """Return after checking Claude error results and the alias catalog."""
        events = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "errors": ["sandbox unavailable"]})
        self.assertEqual(cli_error(events), "sandbox unavailable")
        self.assertIsNone(cli_error(json.dumps({"type": "result", "is_error": True})))
        self.assertIn({"id": "opus", "name": "Opus"}, claude_models("/usr/bin/claude"))
        self.assertEqual(claude_models(None), [])


if __name__ == "__main__":
    unittest.main()
