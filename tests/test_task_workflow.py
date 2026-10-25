"""Verify task-owned branches and conversion of old project records."""

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from app.agent import AgentManager
from app.storage import append_report, read_chat, read_task, report_draft_path, set_implementation, set_readiness, update_task, write_text
from app.task_workflow import TaskWorkflowService
from app.workflow import WorkflowService


def git(*args: str, cwd: Path) -> str:
    """Run Git args in cwd and return stdout."""
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


class TaskWorkflowTest(unittest.TestCase):
    """Check branch creation, completion, and migration using real Git history."""

    def setUp(self):
        """Create an isolated dev repository for each test; return nothing."""
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name) / "repo"
        self.repo.mkdir()
        git("init", "-q", "-b", "dev", cwd=self.repo)
        git("config", "user.name", "Test", cwd=self.repo)
        git("config", "user.email", "test@example.com", cwd=self.repo)
        (self.repo / ".gitignore").write_text("tasks/\nprojects/\n", encoding="utf-8")
        git("add", ".", cwd=self.repo)
        git("commit", "-q", "-m", "初期化", cwd=self.repo)
        self.service = TaskWorkflowService(self.repo, self.repo / "tasks", self.repo / "projects")

    def tearDown(self):
        """Remove the isolated repository after a test; return nothing."""
        self.temporary.cleanup()

    def test_child_and_root_merge_to_their_owners(self):
        """Merge a child into its parent and the parent into dev."""
        root = self.service.create_root_task("親")
        child = self.service.create_child(root["id"], "子")
        self.service.claim_code(child["id"])
        (self.service.code_path(child["id"]) / "feature.txt").write_text("done\n", encoding="utf-8")
        write_text(report_draft_path(self.service.tasks_root, child["id"]), "# 検証\n")
        append_report(self.service.tasks_root, child["id"], "supported", ["feature.txt"], [], "codex", "test", False)
        operation = self.service.begin_agent_commit(child["id"], self.service.completion_preview(child["id"]))
        git("add", "feature.txt", cwd=self.service.code_path(child["id"]))
        git("commit", "-q", "-m", "子を実装", "-m", f"Task-Operation: {operation['id']}", cwd=self.service.code_path(child["id"]))
        self.service.reconcile_agent_commit(operation)
        self.assertEqual(read_task(self.service.tasks_root, child["id"])["status"], "completed")
        self.assertTrue((self.service.code_path(root["id"]) / "feature.txt").exists())
        self.assertFalse((self.repo / "feature.txt").exists())
        write_text(report_draft_path(self.service.tasks_root, root["id"]), "# 総括\n")
        append_report(self.service.tasks_root, root["id"], "supported", [], [], "codex", "test", False, "summary", self.service.summary_basis(root["id"]))
        preview = self.service.completion_preview(root["id"])
        self.assertEqual(preview["target"], "dev")
        self.service.complete_task(root["id"], preview)
        self.assertEqual((self.repo / "feature.txt").read_text(), "done\n")
        self.assertEqual(read_task(self.service.tasks_root, root["id"])["status"], "completed")

    def test_independent_task_agents_run_together(self):
        """Run independent task Agents together and block a related parent."""
        cli = self.repo / "fake-codex"
        cli.write_text(
            f"#!{sys.executable}\n"
            "import json, sys, time\n"
            "sys.stdin.read()\n"
            "print(json.dumps({'type': 'item.started', 'item': {'id': 'wait', 'type': 'command_execution', 'command': 'wait'}}), flush=True)\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        cli.chmod(0o755)
        manager = AgentManager(self.service.tasks_root, self.repo, cli_path=str(cli), workflow=self.service)
        self.service.agent = manager
        parent = self.service.create_root_task("親")
        child = self.service.create_child(parent["id"], "子")
        other = self.service.create_root_task("別の親")
        for task in (child, other):
            update_task(self.service.tasks_root, task["id"], {"plan": "# 計画\n実装"})
            set_readiness(self.service.tasks_root, task["id"], True, "# 計画\n実装", "実装可能")
        first = manager.start_implementation(child["id"])
        second = manager.start_implementation(other["id"])
        self.assertEqual(manager.active_tasks, {child["id"], other["id"]})
        self.assertNotEqual(self.service.code_path(child["id"]), self.service.code_path(other["id"]))
        with self.assertRaisesRegex(ValueError, "親子タスク"):
            manager.start_chat(parent["id"], "plan", "計画を確認")
        with self.assertRaisesRegex(ValueError, "親子タスク"):
            self.service.create_child(parent["id"], "別の子")
        for run in (first, second):
            manager.cancel_run(run["id"])
        deadline = time.monotonic() + 5
        while manager.active_tasks and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(manager.active_tasks)

    def test_commit_agent_starts_while_another_task_runs(self):
        """Allow an approved commit Agent beside an unrelated task Agent."""
        cli = self.repo / "fake-codex"
        cli.write_text(f"#!{sys.executable}\nimport sys, time\nsys.stdin.read()\ntime.sleep(30)\n", encoding="utf-8")
        cli.chmod(0o755)
        manager = AgentManager(self.service.tasks_root, self.repo, cli_path=str(cli), workflow=self.service)
        self.service.agent = manager
        running = self.service.create_root_task("進行中")
        committing = self.service.create_root_task("完了待ち")
        first = manager.start_chat(running["id"], "plan", "計画")
        self.service.claim_code(committing["id"])
        (self.service.code_path(committing["id"]) / "feature.txt").write_text("done\n", encoding="utf-8")
        set_implementation(self.service.tasks_root, committing["id"], "succeeded", ["feature.txt"])
        write_text(report_draft_path(self.service.tasks_root, committing["id"]), "# 検証\n")
        append_report(self.service.tasks_root, committing["id"], "supported", ["feature.txt"], [], "codex", "test", False)
        second = manager.start_commit(committing["id"], self.service.completion_preview(committing["id"]))
        self.assertEqual(manager.active_tasks, {running["id"], committing["id"]})
        for run in (first, second):
            manager.cancel_run(run["id"])
        deadline = time.monotonic() + 5
        while manager.active_tasks and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(manager.active_tasks)

    def run_commit(self, script: str, instruction: str) -> dict:
        """Run a fake commit CLI script with instruction on a new task; return the finished task."""
        cli = self.repo.parent / "fake-commit-codex"
        cli.write_text(f"#!{sys.executable}\n{script}", encoding="utf-8")
        cli.chmod(0o755)
        manager = AgentManager(self.service.tasks_root, self.repo, cli_path=str(cli), workflow=self.service)
        self.service.agent = manager
        task = self.service.create_root_task("完了待ち")
        self.service.claim_code(task["id"])
        (self.service.code_path(task["id"]) / "feature.txt").write_text("done\n", encoding="utf-8")
        set_implementation(self.service.tasks_root, task["id"], "succeeded", ["feature.txt"])
        write_text(report_draft_path(self.service.tasks_root, task["id"]), "# 検証\n")
        append_report(self.service.tasks_root, task["id"], "supported", ["feature.txt"], [], "codex", "test", False)
        manager.start_commit(task["id"], self.service.completion_preview(task["id"]), instruction)
        deadline = time.monotonic() + 10
        while manager.active_tasks and time.monotonic() < deadline:
            time.sleep(0.05)
        return read_task(self.service.tasks_root, task["id"])

    def test_commit_run_history_is_saved_to_result_chat(self):
        """Record the commit instruction, created commits, and Agent reply in the result chat."""
        task = self.run_commit(
            "import json, re, subprocess, sys\n"
            "prompt = sys.stdin.read()\n"
            "operation = re.search(r'Task-Operation: (\\w+)', prompt).group(1)\n"
            "subprocess.run(['git', 'add', 'feature.txt'], check=True)\n"
            "subprocess.run(['git', 'commit', '-q', '-m', '機能を追加', '-m', f'Task-Operation: {operation}'], check=True)\n"
            "path = sys.argv[sys.argv.index('--output-last-message') + 1]\n"
            "open(path, 'w', encoding='utf-8').write(json.dumps({'message': '1件にまとめました'}))\n",
            "1件にまとめる",
        )
        self.assertEqual(task["status"], "completed")
        chat = read_chat(self.service.tasks_root, task["id"], "result")
        self.assertIn("コミット方針: 1件にまとめる", chat[-2]["content"])
        self.assertEqual(chat[-1]["role"], "system")
        self.assertIn(task["commit_hashes"][-1][:10], chat[-1]["content"])
        self.assertIn("1件にまとめました", chat[-1]["content"])
        self.assertFalse((self.service.tasks_root / task["id"] / "diagnostics").exists())

    def test_failed_commit_run_saves_reason_and_diagnostic(self):
        """Record a failed commit run's reason and keep its CLI output for diagnosis."""
        task = self.run_commit(
            "import json, sys\nsys.stdin.read()\n"
            "print(json.dumps({'type': 'error', 'message': '認証に失敗しました'}, ensure_ascii=False))\nsys.exit(1)\n",
            "",
        )
        self.assertNotEqual(task["status"], "completed")
        chat = read_chat(self.service.tasks_root, task["id"], "result")
        self.assertIn("コミット方針: なし", chat[-2]["content"])
        self.assertIn("コミット作成に失敗しました: 認証に失敗しました", chat[-1]["content"])
        self.assertIn("作成したコミット: なし", chat[-1]["content"])
        diagnostics = list((self.service.tasks_root / task["id"] / "diagnostics").glob("commit-*.json"))
        self.assertEqual(len(diagnostics), 1)
        self.assertIn("認証に失敗しました", json.loads(diagnostics[0].read_text(encoding="utf-8"))["stdout_tail"])

    def test_existing_project_records_become_task_owned(self):
        """Keep existing task IDs and reports while assigning each task a branch."""
        legacy = WorkflowService(self.repo, self.repo / "tasks", self.repo / "projects")
        project = legacy.create_project("旧制度", "legacy", "親")
        parent = read_task(self.service.tasks_root, project["first_task_id"])
        child = legacy.create_child(parent["id"], "子")
        before = (self.service.tasks_root / child["id"] / "task.json").read_text(encoding="utf-8")
        self.service.migrate_projects()
        migrated_parent = read_task(self.service.tasks_root, parent["id"])
        migrated_child = read_task(self.service.tasks_root, child["id"])
        self.assertEqual(migrated_parent["root_id"], parent["id"])
        self.assertEqual(migrated_child["root_id"], parent["id"])
        self.assertEqual(migrated_child["parent_id"], parent["id"])
        self.assertNotEqual(migrated_child["branch"], migrated_parent["branch"])
        self.assertEqual(json.loads(before)["id"], migrated_child["id"])
        self.assertTrue((self.repo / "projects" / project["id"] / "project.json").exists())
        self.service.migrate_projects()
        self.assertEqual(read_task(self.service.tasks_root, child["id"])["branch"], migrated_child["branch"])

    def test_migration_preserves_uncommitted_child_work(self):
        """Keep a child's dirty legacy worktree separate from its parent."""
        legacy = WorkflowService(self.repo, self.repo / "tasks", self.repo / "projects")
        project = legacy.create_project("旧制度", "legacy", "親")
        parent_id = project["first_task_id"]
        child = legacy.create_child(parent_id, "子")
        legacy.claim_code(child["id"])
        dirty_branch = legacy.branch_for(child["id"])
        (legacy.code_path(child["id"]) / "unfinished.txt").write_text("途中\n", encoding="utf-8")
        self.service.migrate_projects()
        parent = read_task(self.service.tasks_root, parent_id)
        migrated_child = read_task(self.service.tasks_root, child["id"])
        self.assertEqual(migrated_child["branch"], dirty_branch)
        self.assertNotEqual(parent["branch"], dirty_branch)
        self.assertTrue(migrated_child["code_owner"])
        self.assertEqual((self.service.code_path(child["id"]) / "unfinished.txt").read_text(), "途中\n")
        self.assertFalse((self.service.code_path(parent_id) / "unfinished.txt").exists())


if __name__ == "__main__":
    unittest.main()
