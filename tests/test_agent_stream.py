"""Check Agent CLI event streaming and visible tool activity."""

import sys
import tempfile
import threading
import unittest
from pathlib import Path

from app.agent_stream import run_streamed, tool_activity


class AgentStreamTests(unittest.TestCase):
    """Verify tool events become command progress while the CLI still runs."""

    def test_codex_command_is_visible_before_completion(self):
        """Start a mock CLI and return after its command appears mid-run."""
        script = (
            "import json, sys, time\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'type':'item.started','item':{'id':'cmd-1','type':'command_execution','command':'uv run pytest'}}), flush=True)\n"
            "time.sleep(0.3)\n"
            "print(json.dumps({'type':'item.completed','item':{'id':'cmd-1','type':'command_execution','command':'uv run pytest','exit_code':0}}), flush=True)\n"
        )
        seen = []
        results = []
        started = threading.Event()

        def collect(activity: dict) -> None:
            """Collect one activity event and signal a running command; return nothing."""
            seen.append(activity)
            if activity["state"] == "running":
                started.set()

        def run_mock(cwd: Path) -> None:
            """Run the mock CLI in cwd and store its result; return nothing."""
            results.append(run_streamed([sys.executable, "-u", "-c", script], "prompt", cwd, "codex", collect))

        with tempfile.TemporaryDirectory() as temporary:
            thread = threading.Thread(target=run_mock, args=(Path(temporary),))
            thread.start()
            self.assertTrue(started.wait(2), "Command activity was not streamed")
            self.assertTrue(thread.is_alive(), "The command only appeared after CLI completion")
            thread.join(2)
        self.assertEqual([item["state"] for item in seen], ["running", "done"])
        self.assertEqual(seen[0]["label"], "uv run pytest")
        self.assertEqual(results[0].returncode, 0)

    def test_claude_tool_start_and_result(self):
        """Convert Claude tool blocks into a running and completed command."""
        assistant = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "git status --short"}}]}}
        result = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": ""}]}}
        self.assertEqual(tool_activity(assistant, "claude"), [{"id": "tool-1", "label": "git status --short", "state": "running"}])
        self.assertEqual(tool_activity(result, "claude"), [{"id": "tool-1", "label": "", "state": "done"}])


if __name__ == "__main__":
    unittest.main()
