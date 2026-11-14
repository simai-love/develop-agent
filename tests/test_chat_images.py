"""Verify chat image and file attachment validation, persistence, and Agent CLI handoff."""

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent import AgentManager
from app.attachments import attachment_path, save_attachment
from app.evidence import save_evidence
from app.storage import create_task, read_chat, task_dir, update_task


class ChatImageTests(unittest.TestCase):
    """Check image-only chat requests for both configured CLI providers."""

    def setUp(self):
        """Create a Git repository and task storage; return nothing."""
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q", "-b", "dev"], cwd=self.repo, check=True)
        (self.repo / ".gitignore").write_text("tasks/\n", encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-q", "-m", "初期化"], cwd=self.repo, check=True)
        self.tasks = self.repo / "tasks"
        self.task = create_task(self.tasks, "画像を確認")
        self.image = save_evidence(self.tasks, self.task["id"], "sample.png", b"\x89PNG\r\n\x1a\ncontent")
        self.manager = AgentManager(self.tasks, self.repo, cli_path="/bin/true")
        self.manager.cli_paths["claude"] = "/bin/true"

    def tearDown(self):
        """Remove temporary repository files; return nothing."""
        self.temporary.cleanup()

    def run_image_chat(self, provider: str) -> tuple[list[str], str]:
        """Run an image-only plan chat through provider and return CLI input."""
        if provider == "claude":
            update_task(self.tasks, self.task["id"], {"provider": "claude"})
        captured = {}
        answer = {"message": "確認しました", "implementation_ready": False, "reason": "検討中", "questions": [], "child_task_proposals": []}

        def fake_stream(command, prompt, *args):
            """Record CLI input and return one valid plan response."""
            captured["command"] = command
            captured["prompt"] = prompt
            if provider == "codex":
                Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(answer), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.CompletedProcess(command, 0, json.dumps({"type": "result", "structured_output": answer}), "")

        with patch("app.agent.run_streamed", side_effect=fake_stream):
            run = self.manager.start_chat(self.task["id"], "plan", "", [self.image["name"]])
            for _ in range(200):
                if self.manager.get_run(run["id"])["state"] != "running":
                    break
                time.sleep(0.01)
        self.assertEqual(self.manager.get_run(run["id"])["state"], "succeeded")
        self.assertEqual(read_chat(self.tasks, self.task["id"], "plan")[0]["images"], [self.image["name"]])
        return captured["command"], captured["prompt"]

    def test_codex_receives_image_file(self):
        """Pass the selected image to Codex and retain its message attachment."""
        command, prompt = self.run_image_chat("codex")
        self.assertIn("--image", command)
        self.assertIn(str(task_dir(self.tasks, self.task["id"]) / "evidence" / self.image["name"]), command)
        self.assertIn(self.image["name"], prompt)

    def test_claude_receives_image_path(self):
        """Tell Claude Code to read the selected image from task storage."""
        command, prompt = self.run_image_chat("claude")
        self.assertIn("--print", command)
        self.assertIn(str(task_dir(self.tasks, self.task["id"]) / "evidence" / self.image["name"]), prompt)
        self.assertIn("Readツール", prompt)

    def test_chat_rejects_other_files_and_missing_images(self):
        """Reject attachment names that are not saved images for this task."""
        with self.assertRaises(ValueError):
            self.manager.start_chat(self.task["id"], "plan", "", ["missing.png"])
        data = save_evidence(self.tasks, self.task["id"], "notes.pdf", b"document")
        with self.assertRaises(ValueError):
            self.manager.start_chat(self.task["id"], "plan", "", [data["name"]])

    def test_any_file_type_is_saved_and_passed_by_path(self):
        """Save a non-image file under a safe name and name its path in the Agent prompt."""
        saved = save_attachment(self.tasks, self.task["id"], "../実験 結果.xlsx", b"sheet")
        self.assertRegex(saved["name"], r"^[0-9a-f]{8}-[a-zA-Z0-9_-]+\.xlsx$")
        self.assertEqual(saved["original_name"], "実験 結果.xlsx")
        path = attachment_path(self.tasks, self.task["id"], saved["name"])
        self.assertEqual(path.read_bytes(), b"sheet")
        captured = {}
        answer = {"message": "確認しました", "implementation_ready": False, "reason": "検討中", "questions": [], "child_task_proposals": []}

        def fake_stream(command, prompt, *args):
            """Record the prompt and return one valid plan response."""
            captured["prompt"] = prompt
            Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(answer), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch("app.agent.run_streamed", side_effect=fake_stream):
            run = self.manager.start_chat(self.task["id"], "plan", "", [], [saved["name"]])
            for _ in range(200):
                if self.manager.get_run(run["id"])["state"] != "running":
                    break
                time.sleep(0.01)
        self.assertEqual(self.manager.get_run(run["id"])["state"], "succeeded")
        self.assertIn(str(path), captured["prompt"])
        self.assertEqual(read_chat(self.tasks, self.task["id"], "plan")[0]["files"], [saved["name"]])

    def test_chat_rejects_unknown_or_unsafe_files(self):
        """Reject missing attachment names and names that leave the attachment folder."""
        with self.assertRaises(ValueError):
            self.manager.start_chat(self.task["id"], "plan", "", [], ["missing.bin"])
        with self.assertRaises(ValueError):
            self.manager.start_chat(self.task["id"], "plan", "", [], ["../task.json"])
        with self.assertRaises(ValueError):
            save_attachment(self.tasks, self.task["id"], "empty.txt", b"")


if __name__ == "__main__":
    unittest.main()
