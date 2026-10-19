"""Verify task-owned branches and conversion of old project records."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.storage import append_report, read_task, report_draft_path, write_text
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
