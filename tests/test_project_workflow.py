"""Verify branch transitions in isolated Git repositories."""

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from app.agent import AgentManager
from app.git.workspace import task_branch_name
from app.projects import list_projects
from app.storage import append_report, create_task, plan_hash, read_task, report_draft_path, save_child_proposals, save_metadata, set_readiness, write_text
from app.workflow import WorkflowService


class ProjectWorkflowTest(unittest.TestCase):
    """Exercise project, child, and parent completion with real Git history."""

    def test_task_branch_name_is_readable_and_safe(self):
        """Keep a Japanese title readable while removing Git ref separators."""
        self.assertEqual(task_branch_name(" 画面/表示の改善！ ", "abc123"), "task/画面-表示の改善-abc123")
        self.assertEqual(task_branch_name("...", "abc123"), "task/untitled-abc123")

    def test_parallel_children_commit_separately_and_merge_after_review(self):
        """Keep unapproved sibling edits isolated and merge each reviewed commit."""
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "dev"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            (repo / "README.md").write_text("# Test\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "初期化"], cwd=repo, check=True)
            service = WorkflowService(repo, repo / "tasks", repo / "projects")
            project = service.create_project("検証", "validation", "親")
            parent_id = project["first_task_id"]
            children = [service.create_child(parent_id, title) for title in ("子A", "子B")]
            for child in children:
                write_text(service.tasks_root / child["id"] / "plan.md", "# 計画\n")
                set_readiness(service.tasks_root, child["id"], True, "# 計画\n", "実装可能")
            service.prepare_parallel_children(parent_id, [child["id"] for child in children])
            self.assertNotEqual(service.code_path(children[0]["id"]), service.code_path(children[1]["id"]))
            parent_path = service.code_path(parent_id)
            for index, child in enumerate(children):
                task_id = child["id"]
                service.claim_code(task_id)
                (service.code_path(task_id) / f"feature{index}.txt").write_text("done\n", encoding="utf-8")
                write_text(report_draft_path(service.tasks_root, task_id), "# 検証\n")
                append_report(service.tasks_root, task_id, "supported", [f"feature{index}.txt"], [], "codex", "test", False)
            self.assertFalse((parent_path / "feature0.txt").exists())
            first_id = children[0]["id"]
            service.complete_task(first_id, service.completion_preview(first_id))
            self.assertEqual(read_task(service.tasks_root, first_id)["status"], "completed")
            self.assertTrue((parent_path / "feature0.txt").exists())
            self.assertFalse((parent_path / "feature1.txt").exists())
            second_id = children[1]["id"]
            service.complete_task(second_id, service.completion_preview(second_id))
            self.assertTrue((parent_path / "feature1.txt").exists())
            self.assertEqual(set(service.summary_basis(parent_id)["children"]), {first_id, second_id})
            conflicting = [service.create_child(parent_id, title) for title in ("子C", "子D")]
            for index, child in enumerate(conflicting):
                task_id = child["id"]
                write_text(service.tasks_root / task_id / "plan.md", "# 計画\n")
                set_readiness(service.tasks_root, task_id, True, "# 計画\n", "実装可能")
            service.prepare_parallel_children(parent_id, [child["id"] for child in conflicting])
            for index, child in enumerate(conflicting):
                task_id = child["id"]
                service.claim_code(task_id)
                (service.code_path(task_id) / "feature0.txt").write_text(f"conflict {index}\n", encoding="utf-8")
                write_text(report_draft_path(service.tasks_root, task_id), "# 検証\n")
                append_report(service.tasks_root, task_id, "supported", ["feature0.txt"], [], "codex", "test", False)
                if index == 0:
                    service.complete_task(task_id, service.completion_preview(task_id))
            blocked_id = conflicting[1]["id"]
            with self.assertRaisesRegex(ValueError, "コミットは保存"):
                service.complete_task(blocked_id, service.completion_preview(blocked_id))
            self.assertEqual((parent_path / "feature0.txt").read_text(), "conflict 0\n")
            self.assertTrue(read_task(service.tasks_root, blocked_id)["commit_hash"])
            self.assertNotEqual(read_task(service.tasks_root, blocked_id)["status"], "completed")
            self.assertTrue(service.git.clean(parent_path))

    def test_parallel_agent_runs_receive_shared_instruction(self):
        """Run two sibling agents concurrently and pass the one-run instruction to both."""
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "dev"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            (repo / ".gitignore").write_text("tasks/\nprojects/\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "初期化"], cwd=repo, check=True)
            service = WorkflowService(repo, repo / "tasks", repo / "projects")
            project = service.create_project("並列", "parallel", "親")
            parent_id = project["first_task_id"]
            children = [service.create_child(parent_id, title) for title in ("子A", "子B")]
            for child in children:
                write_text(service.tasks_root / child["id"] / "plan.md", "# 計画\n")
                set_readiness(service.tasks_root, child["id"], True, "# 計画\n", "実装可能")
            cli = Path(temporary) / "fake-codex"
            cli.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys, time\n"
                "from pathlib import Path\n"
                "prompt = sys.stdin.read()\n"
                "task = Path(sys.argv[sys.argv.index('--add-dir') + 1])\n"
                "(task / 'received-instruction.txt').write_text(prompt, encoding='utf-8')\n"
                "Path('feature.txt').write_text(task.name + '\\n', encoding='utf-8')\n"
                "(task / 'report-draft.md').write_text('# 検証\\n成功\\n', encoding='utf-8')\n"
                "time.sleep(0.2)\n"
                "data = {'message': '完了', 'outcome': 'supported', 'needs_followup': False, "
                "'evidence_files': [], 'child_task_proposals': [], 'commit_message': '並列実装'}\n"
                "Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text(json.dumps(data, ensure_ascii=False))\n",
                encoding="utf-8",
            )
            cli.chmod(0o755)
            manager = AgentManager(service.tasks_root, repo, str(cli), workflow=service)
            service.agent = manager
            runs = manager.start_parallel_children(parent_id, [child["id"] for child in children], "互換性を確認")
            self.assertEqual(len(runs), 2)
            for _ in range(100):
                if all(manager.get_run(run["id"])["state"] != "running" for run in runs):
                    break
                time.sleep(0.02)
            self.assertEqual([manager.get_run(run["id"])["state"] for run in runs], ["succeeded", "succeeded"])
            for child in children:
                task_path = service.tasks_root / child["id"]
                self.assertIn("今回の実行への追加指示（計画は変更しない）:\n互換性を確認", (task_path / "received-instruction.txt").read_text(encoding="utf-8"))
                self.assertEqual(read_task(service.tasks_root, child["id"])["plan"], "# 計画\n")
            self.assertFalse((service.code_path(parent_id) / "feature.txt").exists())

    def test_child_commits_merge_into_parent_then_dev(self):
        """Build a nested project and check commit and merge outputs."""
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "dev"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            (repo / ".gitignore").write_text("tasks/\nprojects/\n", encoding="utf-8")
            (repo / "README.md").write_text("# Test\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "初期化"], cwd=repo, check=True)
            service = WorkflowService(repo, repo / "tasks", repo / "projects")
            project = service.create_project("検証", "validation", "親タスク")
            parent_id = project["first_task_id"]
            proposal = {"title": "子A", "purpose": "範囲A", "scope": "機能A", "verification": "動作確認", "reason": "独立して検証できる"}
            proposed = save_child_proposals(service.tasks_root, parent_id, [proposal], plan_hash(""))["child_proposals"][0]
            first = service.create_child(parent_id, "子A", "# 子A", proposed["id"])
            self.assertEqual(service.create_child(parent_id, "子A", "# 子A", proposed["id"])["id"], first["id"])
            second = service.create_child(parent_id, "子B")
            for index, child in enumerate((first, second), 1):
                service.claim_code(child["id"])
                if index == 1:
                    with self.assertRaises(ValueError):
                        service.claim_code(second["id"])
                code = service.code_path(child["id"]) / f"feature{index}.txt"
                code.write_text(f"feature {index}\n", encoding="utf-8")
                pending_diff = service.task_diff(child["id"], code.name)
                self.assertEqual(pending_diff["files"], [code.name])
                self.assertIn(f"+feature {index}", pending_diff["patch"])
                write_text(report_draft_path(service.tasks_root, child["id"]), f"# 検証 {index}\n")
                append_report(service.tasks_root, child["id"], "supported", [code.name], [], "codex", "test", False)
                preview = service.completion_preview(child["id"])
                if index == 1:
                    with self.assertRaisesRegex(ValueError, "コミットメッセージ"):
                        service.complete_task(child["id"], {**preview, "message": "  "})
                    preview["message"] = "  子Aの編集済みメッセージ  "
                service.complete_task(child["id"], preview)
                commit_hash = read_task(service.tasks_root, child["id"])["commit_hash"]
                self.assertTrue(commit_hash)
                self.assertIn(f"+feature {index}", service.task_diff(child["id"], code.name)["patch"])
                if index == 1:
                    subject = service.git.git("show", "-s", "--format=%s", commit_hash).stdout.strip()
                    self.assertEqual(subject, "子Aの編集済みメッセージ")
                if index == 1:
                    task = read_task(service.tasks_root, child["id"])
                    task.update(status="verifying", commit_hash=None, completed_at=None)
                    save_metadata(service.tasks_root, task)
                    operation_path = next((repo / "projects" / project["id"] / "operations").glob("*.json"))
                    operation = json.loads(operation_path.read_text())
                    operation["state"] = "git_done"
                    write_text(operation_path, json.dumps(operation))
                    service.recover_operations()
                    self.assertEqual(read_task(service.tasks_root, child["id"])["status"], "completed")
            cli = Path(temporary) / "fake-codex"
            cli.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "from pathlib import Path\n"
                "Path('report-draft.md').write_text('# 子タスクの総括\\n達成しました\\n', encoding='utf-8')\n"
                "data = {'message': '総括しました', 'outcome': 'supported', 'needs_followup': False, 'evidence_files': [], 'child_task_proposals': []}\n"
                "Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text(json.dumps(data, ensure_ascii=False))\n",
                encoding="utf-8",
            )
            cli.chmod(0o755)
            manager = AgentManager(service.tasks_root, repo, str(cli), workflow=service)
            service.agent = manager
            self.assertIn("今回の総括への追加指示（計画は変更しない）:\n結果の違いを比較する", manager._summary_prompt(parent_id, "結果の違いを比較する"))
            run = manager.start_summary(parent_id, "結果の違いを比較する")
            for _ in range(100):
                if manager.get_run(run["id"])["state"] != "running":
                    break
                time.sleep(0.02)
            self.assertEqual(manager.get_run(run["id"])["state"], "succeeded")
            self.assertEqual(read_task(service.tasks_root, parent_id)["reports"][-1]["kind"], "summary")
            self.assertEqual(service.task_diff(first["id"])["files"], ["feature1.txt"])
            self.assertEqual(service.task_diff(parent_id)["files"], ["feature1.txt", "feature2.txt"])
            service.complete_task(parent_id, service.completion_preview(parent_id))
            self.assertTrue(read_task(service.tasks_root, parent_id)["merge_hash"])
            self.assertEqual(service.task_diff(parent_id)["files"], ["feature1.txt", "feature2.txt"])
            second_root = service.create_root_task(project["id"], "別のタスク")
            self.assertIsNone(second_root["parent_id"])
            self.assertEqual(service.branch_for(second_root["id"]), project["branch"])
            service.claim_code(second_root["id"])
            (service.code_path(second_root["id"]) / "feature3.txt").write_text("feature 3\n", encoding="utf-8")
            write_text(report_draft_path(service.tasks_root, second_root["id"]), "# 追加タスクの検証\n")
            append_report(service.tasks_root, second_root["id"], "supported", ["feature3.txt"], [], "codex", "test", False)
            service.complete_task(second_root["id"], service.completion_preview(second_root["id"]))
            service.complete_project(project["id"], service.project_preview(project["id"]))
            self.assertEqual((repo / "feature1.txt").read_text(), "feature 1\n")
            self.assertEqual((repo / "feature2.txt").read_text(), "feature 2\n")
            self.assertEqual((repo / "feature3.txt").read_text(), "feature 3\n")
            self.assertEqual(service.git.git("rev-list", "--parents", "-n", "1", "dev").stdout.count(" "), 2)

    def test_legacy_migration_keeps_task_ids_and_is_repeatable(self):
        """Migrate a legacy hierarchy twice and preserve its original records."""
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "dev"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            (repo / ".gitignore").write_text("tasks/\nprojects/\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "初期化"], cwd=repo, check=True)
            root = create_task(repo / "tasks", "旧タスク")
            child = create_task(repo / "tasks", "旧子タスク", root["id"])
            service = WorkflowService(repo, repo / "tasks", repo / "projects")
            pending = repo / "pending.txt"
            pending.write_text("作業中\n", encoding="utf-8")
            service.migrate_legacy()
            self.assertTrue(list_projects(repo / "projects")[0]["needs_review"])
            pending.unlink()
            service.acknowledge_migration(root["id"])
            service.migrate_legacy()
            self.assertEqual(len(list_projects(repo / "projects")), 1)
            self.assertEqual(read_task(repo / "tasks", child["id"])["project_id"], root["id"])
            self.assertEqual(read_task(repo / "tasks", root["id"])["parent_id"], None)
            self.assertEqual(read_task(repo / "tasks", root["id"])["branch"], f"task/旧タスク-{root['id']}")
            renamed = service.rename_legacy_branch(root["id"], "renamed-project")
            self.assertEqual(renamed["branch"], f"project/renamed-project-{root['id'][:6]}")
            interrupted_child = read_task(repo / "tasks", child["id"])
            interrupted_child["project_id"] = None
            save_metadata(repo / "tasks", interrupted_child)
            service.migrate_legacy()
            self.assertEqual(read_task(repo / "tasks", child["id"])["project_id"], root["id"])
            write_text(report_draft_path(repo / "tasks", child["id"]), "# 旧検証\n")
            append_report(repo / "tasks", child["id"], "supported", [], [], "codex", "test", False)
            preview = service.completion_preview(child["id"])
            self.assertEqual(preview["kind"], "legacy")
            completed = service.complete_task(child["id"], preview)
            self.assertTrue(completed["legacy_completed"])
            self.assertIsNone(completed["commit_hash"])

    def test_completed_leaf_can_become_parent_after_scope_expands(self):
        """Keep the first commit when a completed leaf gains its first child."""
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "dev"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            (repo / ".gitignore").write_text("tasks/\nprojects/\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "初期化"], cwd=repo, check=True)
            service = WorkflowService(repo, repo / "tasks", repo / "projects")
            project = service.create_project("追加調査", "follow-up", "初回")
            task_id = project["first_task_id"]
            service.claim_code(task_id)
            (service.code_path(task_id) / "first.txt").write_text("first\n", encoding="utf-8")
            write_text(report_draft_path(service.tasks_root, task_id), "# 最初の検証\n")
            append_report(service.tasks_root, task_id, "supported", ["first.txt"], [], "codex", "test", False)
            first = service.complete_task(task_id, service.completion_preview(task_id))["commit_hash"]
            service.create_child(task_id, "追加の子")
            task = read_task(service.tasks_root, task_id)
            self.assertEqual(task["pre_split_commit"], first)
            self.assertEqual(task["status"], "planning")
            self.assertEqual(task["branch"], f"task/初回-{task_id}")


if __name__ == "__main__":
    unittest.main()
