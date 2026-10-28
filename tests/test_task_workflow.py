"""Verify task-owned branches and conversion of old project records."""

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from app.agent import AgentManager
from app.storage import append_report, read_chat, read_task, report_draft_path, set_implementation, set_readiness, task_dir, update_task, write_text
from app.task_workflow import TaskWorkflowService
from app.workflow import WorkflowService


def git(*args: str, cwd: Path) -> str:
    """Run Git args in cwd and return stdout."""
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


FAKE_COMPLETION = (
    "import json, re, shlex, subprocess, sys\n"
    "prompt = sys.stdin.read()\n"
    "def complete(names, merge=True):\n"
    "    operations = re.findall(r'Task-Operation: (\\w+)', prompt)\n"
    "    source = re.search(r'作業場所 (\\S+) で、現在', prompt).group(1)\n"
    "    for name in names:\n"
    "        subprocess.run(['git', '-C', source, 'add', name], check=True)\n"
    "        subprocess.run(['git', '-C', source, 'commit', '-q', '-m', name, '-m', 'Task-Operation: ' + operations[0]], check=True)\n"
    "    if merge:\n"
    "        subprocess.run(shlex.split(next(line for line in prompt.splitlines() if line.startswith('git merge'))), check=True, capture_output=True)\n"
    "        for command in re.findall(r'`(git (?:worktree remove|branch -d) [^`]+)`', prompt):\n"
    "            subprocess.run(shlex.split(command), check=True, capture_output=True)\n"
    "    path = sys.argv[sys.argv.index('--output-last-message') + 1]\n"
    "    open(path, 'w', encoding='utf-8').write(json.dumps({'message': '完了しました'}, ensure_ascii=False))\n"
)


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

    def agent_merge(self, operation: dict, remove_branch: bool = True) -> None:
        """Simulate an Agent merge for operation, optionally deleting the branch; return nothing."""
        target_path = self.service.git.ensure(operation["destination"])
        git("merge", "--no-ff", "-q", "-m", operation["message"], "-m", f"Task-Operation: {operation['id']}", operation["branch"], cwd=target_path)
        if remove_branch:
            git("worktree", "remove", str(self.service.git.path(operation["branch"])), cwd=target_path)
            git("branch", "-d", operation["branch"], cwd=target_path)

    def test_child_and_root_merge_to_their_owners(self):
        """Accept Agent merges of a child into its parent and the parent into dev."""
        root = self.service.create_root_task("親")
        child = self.service.create_child(root["id"], "子")
        self.service.claim_code(child["id"])
        (self.service.code_path(child["id"]) / "feature.txt").write_text("done\n", encoding="utf-8")
        write_text(report_draft_path(self.service.tasks_root, child["id"]), "# 検証\n")
        append_report(self.service.tasks_root, child["id"], "supported", ["feature.txt"], [], "codex", "test", False)
        operations = self.service.begin_agent_completion(child["id"], self.service.completion_preview(child["id"]))
        git("add", "feature.txt", cwd=self.service.code_path(child["id"]))
        git("commit", "-q", "-m", "子を実装", "-m", f"Task-Operation: {operations['commit']['id']}", cwd=self.service.code_path(child["id"]))
        self.agent_merge(operations["merge"])
        self.service.finish_agent_completion(operations)
        self.assertEqual(read_task(self.service.tasks_root, child["id"])["status"], "completed")
        self.assertNotIn("cleanup", operations["merge"])
        self.assertTrue((self.service.code_path(root["id"]) / "feature.txt").exists())
        self.assertFalse((self.repo / "feature.txt").exists())
        write_text(report_draft_path(self.service.tasks_root, root["id"]), "# 総括\n")
        append_report(self.service.tasks_root, root["id"], "supported", [], [], "codex", "test", False, "summary", self.service.summary_basis(root["id"]))
        preview = self.service.completion_preview(root["id"])
        self.assertEqual(preview["target"], "dev")
        operations = self.service.begin_agent_completion(root["id"], preview)
        self.assertIsNone(operations["commit"])
        self.agent_merge(operations["merge"], remove_branch=False)
        self.service.finish_agent_completion(operations)
        self.assertEqual(operations["merge"]["cleanup"], "gui")
        self.assertFalse(self.service.git.has_branch(root["branch"]))
        self.assertEqual((self.repo / "feature.txt").read_text(), "done\n")
        self.assertEqual(read_task(self.service.tasks_root, root["id"])["status"], "completed")

    def test_merge_with_unrelated_change_is_rejected(self):
        """Reject an Agent merge whose tree differs from Git's conflict-free result."""
        task = self.ready_task("葉", {"feature.txt": "done\n"})
        operations = self.service.begin_agent_completion(task["id"], self.service.completion_preview(task["id"]))
        git("add", "feature.txt", cwd=self.service.code_path(task["id"]))
        git("commit", "-q", "-m", "実装", "-m", f"Task-Operation: {operations['commit']['id']}", cwd=self.service.code_path(task["id"]))
        git("merge", "--no-ff", "--no-commit", operations["merge"]["branch"], cwd=self.repo)
        (self.repo / "extra.txt").write_text("extra\n", encoding="utf-8")
        git("add", "extra.txt", cwd=self.repo)
        git("commit", "-q", "-m", "統合", "-m", f"Task-Operation: {operations['merge']['id']}", cwd=self.repo)
        with self.assertRaisesRegex(ValueError, "自動マージと一致しません"):
            self.service.finish_agent_completion(operations)
        self.assertNotEqual(read_task(self.service.tasks_root, task["id"])["status"], "completed")
        self.assertTrue(read_task(self.service.tasks_root, task["id"])["commit_hash"])

    def test_resolved_conflict_is_accepted_and_unresolved_merge_is_aborted(self):
        """Abort an unfinished conflicting merge, then accept a resolution limited to task files."""
        first = self.ready_task("先", {"shared.txt": "first\n"})
        second = self.ready_task("後", {"shared.txt": "second\n"})
        operations = self.service.begin_agent_completion(first["id"], self.service.completion_preview(first["id"]))
        git("add", "shared.txt", cwd=self.service.code_path(first["id"]))
        git("commit", "-q", "-m", "先", "-m", f"Task-Operation: {operations['commit']['id']}", cwd=self.service.code_path(first["id"]))
        self.agent_merge(operations["merge"])
        self.service.finish_agent_completion(operations)
        operations = self.service.begin_agent_completion(second["id"], self.service.completion_preview(second["id"]))
        git("add", "shared.txt", cwd=self.service.code_path(second["id"]))
        git("commit", "-q", "-m", "後", "-m", f"Task-Operation: {operations['commit']['id']}", cwd=self.service.code_path(second["id"]))
        subprocess.run(["git", "merge", "--no-ff", "-m", "統合", operations["merge"]["branch"]], cwd=self.repo, capture_output=True)
        with self.assertRaisesRegex(ValueError, "マージを作成しませんでした"):
            self.service.finish_agent_completion(operations)
        self.assertTrue(self.service.abandon_agent_merge(operations["merge"]))
        self.assertTrue(self.service.git.clean(self.repo))
        self.assertEqual(operations["merge"]["state"], "failed")
        preview = self.service.completion_preview(second["id"])
        self.assertEqual(preview["kind"], "merge")
        operations = self.service.begin_agent_completion(second["id"], preview)
        subprocess.run(["git", "merge", "--no-ff", "-m", operations["merge"]["message"], "-m", f"Task-Operation: {operations['merge']['id']}", operations["merge"]["branch"]], cwd=self.repo, capture_output=True)
        (self.repo / "shared.txt").write_text("first\nsecond\n", encoding="utf-8")
        git("add", "shared.txt", cwd=self.repo)
        git("commit", "-q", "--no-edit", cwd=self.repo)
        self.service.finish_agent_completion(operations)
        self.assertEqual(read_task(self.service.tasks_root, second["id"])["status"], "completed")
        self.assertEqual((self.repo / "shared.txt").read_text(), "first\nsecond\n")

    def test_committed_task_can_be_edited_after_failed_merge(self):
        """Reopen a committed, unmerged task for fixes and commit the new changes before merging."""
        task = self.ready_task("再修正", {"feature.txt": "done\n"})
        operations = self.service.begin_agent_completion(task["id"], self.service.completion_preview(task["id"]))
        git("add", "feature.txt", cwd=self.service.code_path(task["id"]))
        git("commit", "-q", "-m", "実装", "-m", f"Task-Operation: {operations['commit']['id']}", cwd=self.service.code_path(task["id"]))
        with self.assertRaisesRegex(ValueError, "マージを作成しませんでした"):
            self.service.finish_agent_completion(operations)
        self.service.abandon_agent_merge(operations["merge"])
        self.service.claim_code(task["id"])
        (self.service.code_path(task["id"]) / "fix.txt").write_text("fix\n", encoding="utf-8")
        preview = self.service.completion_preview(task["id"])
        self.assertEqual((preview["kind"], preview["paths"]), ("commit", ["fix.txt"]))
        operations = self.service.begin_agent_completion(task["id"], preview)
        git("add", "fix.txt", cwd=self.service.code_path(task["id"]))
        git("commit", "-q", "-m", "修正", "-m", f"Task-Operation: {operations['commit']['id']}", cwd=self.service.code_path(task["id"]))
        self.agent_merge(operations["merge"])
        task = self.service.finish_agent_completion(operations)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(len(task["commit_hashes"]), 2)

    def test_restart_finishes_agent_commit_and_merge(self):
        """Finish commits and an Agent merge recorded in Git before a server restart."""
        task = self.ready_task("再起動", {"feature.txt": "done\n"})
        operations = self.service.begin_agent_completion(task["id"], self.service.completion_preview(task["id"]))
        git("add", "feature.txt", cwd=self.service.code_path(task["id"]))
        git("commit", "-q", "-m", "実装", "-m", f"Task-Operation: {operations['commit']['id']}", cwd=self.service.code_path(task["id"]))
        self.agent_merge(operations["merge"])
        TaskWorkflowService(self.repo, self.repo / "tasks", self.repo / "projects").recover_operations()
        task = read_task(self.service.tasks_root, task["id"])
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["merge_hash"], git("rev-parse", "dev", cwd=self.repo))

    def ready_task(self, title: str, files: dict[str, str]) -> dict:
        """Create a root task with uncommitted files and a validation report; return the task."""
        task = self.service.create_root_task(title)
        self.service.claim_code(task["id"])
        for name, content in files.items():
            (self.service.code_path(task["id"]) / name).write_text(content, encoding="utf-8")
        set_implementation(self.service.tasks_root, task["id"], "succeeded", list(files))
        write_text(report_draft_path(self.service.tasks_root, task["id"]), "# 検証\n")
        append_report(self.service.tasks_root, task["id"], "supported", list(files), [], "codex", "test", False)
        return read_task(self.service.tasks_root, task["id"])

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
        """Allow an approved commit Agent beside an unrelated task Agent, but not beside another merge into dev."""
        cli = self.repo.parent / "fake-codex"
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
        second = manager.start_completion(committing["id"], self.service.completion_preview(committing["id"]))
        self.assertEqual(manager.active_tasks, {running["id"], committing["id"]})
        waiting = self.ready_task("次の完了", {"other.txt": "other\n"})
        with self.assertRaisesRegex(ValueError, "dev への統合を別のタスクで実行中"):
            manager.start_completion(waiting["id"], self.service.completion_preview(waiting["id"]))
        for run in (first, second):
            manager.cancel_run(run["id"])
        deadline = time.monotonic() + 5
        while manager.active_tasks and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(manager.active_tasks)

    def run_completion(self, script: str, instruction: str, task: dict | None = None) -> dict:
        """Run a fake completion CLI script with instruction on a ready task; return the task afterwards."""
        cli = self.repo.parent / "fake-commit-codex"
        cli.write_text(f"#!{sys.executable}\n{script}", encoding="utf-8")
        cli.chmod(0o755)
        manager = AgentManager(self.service.tasks_root, self.repo, cli_path=str(cli), workflow=self.service)
        self.service.agent = manager
        task = task or self.ready_task("完了待ち", {"api.txt": "api\n", "ui.txt": "ui\n"})
        manager.start_completion(task["id"], self.service.completion_preview(task["id"]), instruction)
        deadline = time.monotonic() + 10
        while manager.active_tasks and time.monotonic() < deadline:
            time.sleep(0.05)
        return read_task(self.service.tasks_root, task["id"])

    def test_agent_commits_merges_and_deletes_branch(self):
        """Record the instruction, two commits, merge, and Agent reply after a full completion run."""
        task = self.run_completion(FAKE_COMPLETION + "complete(['api.txt', 'ui.txt'])\n", "APIと画面を分ける")
        self.assertEqual(task["status"], "completed")
        self.assertEqual(len(task["commit_hashes"]), 2)
        self.assertFalse(self.service.git.has_branch(task["branch"]))
        self.assertEqual((self.repo / "ui.txt").read_text(), "ui\n")
        chat = read_chat(self.service.tasks_root, task["id"], "result")
        self.assertIn("方針: APIと画面を分ける", chat[-2]["content"])
        self.assertEqual(chat[-1]["role"], "system")
        self.assertIn(task["commit_hashes"][-1][:10], chat[-1]["content"])
        self.assertIn(f"→ dev ({task['merge_hash'][:10]})", chat[-1]["content"])
        self.assertNotIn("GUIが削除", chat[-1]["content"])
        self.assertIn("完了しました", chat[-1]["content"])
        self.assertFalse((task_dir(self.service.tasks_root, task["id"]) / "diagnostics").exists())

    def test_partial_commit_run_can_be_retried(self):
        """Keep a verified partial commit, then finish the remaining commit and merge on retry."""
        task = self.run_completion(FAKE_COMPLETION + "complete(['api.txt'], merge=False)\n", "")
        self.assertEqual(len(task["commit_hashes"]), 1)
        self.assertNotEqual(task["status"], "completed")
        self.assertEqual(self.service.completion_preview(task["id"])["paths"], ["ui.txt"])
        task = self.run_completion(FAKE_COMPLETION + "complete(['ui.txt'])\n", "", task)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(len(task["commit_hashes"]), 2)

    def test_failed_completion_run_saves_reason_and_diagnostic(self):
        """Record a failed completion run's reason and keep its CLI output for diagnosis."""
        task = self.run_completion(
            "import json, sys\nsys.stdin.read()\n"
            "print(json.dumps({'type': 'error', 'message': '認証に失敗しました'}, ensure_ascii=False))\nsys.exit(1)\n",
            "",
        )
        self.assertNotEqual(task["status"], "completed")
        chat = read_chat(self.service.tasks_root, task["id"], "result")
        self.assertIn("方針: なし", chat[-2]["content"])
        self.assertIn("コミット・統合に失敗しました: 認証に失敗しました", chat[-1]["content"])
        self.assertIn("作成したコミット: なし", chat[-1]["content"])
        diagnostics = list((task_dir(self.service.tasks_root, task["id"]) / "diagnostics").glob("completion-*.json"))
        self.assertEqual(len(diagnostics), 1)
        self.assertIn("認証に失敗しました", json.loads(diagnostics[0].read_text(encoding="utf-8"))["stdout_tail"])

    def test_unexpected_worker_error_releases_task(self):
        """Mark a run failed and release its task when the worker raises an unexpected error."""
        manager = AgentManager(self.service.tasks_root, self.repo, cli_path=sys.executable, workflow=self.service)
        self.service.agent = manager
        task = self.service.create_root_task("計画")

        def broken_prompt(task_id: str, phase: str) -> str:
            """Raise an error that the worker does not expect; return nothing."""
            raise TypeError("壊れた応答")

        manager._chat_prompt = broken_prompt
        run = manager.start_chat(task["id"], "plan", "計画")
        deadline = time.monotonic() + 5
        while manager.active_tasks and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(manager.active_tasks)
        self.assertEqual(manager.get_run(run["id"])["state"], "failed")
        self.assertIn("壊れた応答", manager.get_run(run["id"])["message"])

    def test_existing_project_records_become_task_owned(self):
        """Keep existing task IDs and reports while assigning each task a branch."""
        legacy = WorkflowService(self.repo, self.repo / "tasks", self.repo / "projects")
        project = legacy.create_project("旧制度", "legacy", "親")
        parent = read_task(self.service.tasks_root, project["first_task_id"])
        child = legacy.create_child(parent["id"], "子")
        before = (task_dir(self.service.tasks_root, child["id"]) / "task.json").read_text(encoding="utf-8")
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
