"""Check task-local Agent artifacts without a live CLI."""

import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from app.agent import AgentManager
from app.evidence import evidence_path, list_evidence, save_evidence
from app.model_catalog import codex_models
from app.storage import DEFAULT_CODEX_MODEL, assign_default_models, create_task, read_chat, read_task, set_readiness, update_task
from app.task_status import display_status


class WorkflowTest(unittest.TestCase):
    """Exercise a planning exchange and its implementation gate."""

    def test_display_status_shows_next_action_and_active_runs(self):
        """Check task and chat inputs produce the user's visible next step."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = create_task(root, "状態確認")
            self.assertEqual(display_status(task, [], False)["key"], "not-started")
            questions = [{"role": "assistant", "questions": [{"prompt": "対象は？"}]}]
            self.assertEqual(display_status(task, questions, False)["detail"], "質問に回答")
            self.assertEqual(display_status(task, questions, True)["key"], "running")
            plan = "# 計画\n改善する"
            update_task(root, task["id"], {"plan": plan})
            ready = set_readiness(root, task["id"], True, plan, "実装可能")
            self.assertEqual(display_status(ready, questions, False)["detail"], "実装を開始")
            ready["status"] = "verifying"
            self.assertEqual(display_status(ready, questions, False)["detail"], "検証結果を確認")
            ready["status"] = "completed"
            self.assertEqual(display_status(ready, questions, False)["key"], "completed")

    def test_codex_default_model_and_existing_task_migration(self):
        """Check new, switched, and old tasks use the configured Codex default."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = create_task(root, "新規")
            self.assertEqual(task["model"], DEFAULT_CODEX_MODEL)
            update_task(root, task["id"], {"provider": "claude"})
            self.assertEqual(read_task(root, task["id"])["model"], "")
            update_task(root, task["id"], {"provider": "codex"})
            self.assertEqual(read_task(root, task["id"])["model"], DEFAULT_CODEX_MODEL)

            old = create_task(root, "旧タスク")
            update_task(root, old["id"], {"model": "", "plan": "# 計画\n改善する"})
            set_readiness(root, old["id"], True, "# 計画\n改善する", "実装可能")
            custom = create_task(root, "指定済み")
            update_task(root, custom["id"], {"model": "gpt-6-astra"})
            completed = create_task(root, "完了済み")
            update_task(root, completed["id"], {"model": ""})
            metadata_path = root / completed["id"] / "task.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata["status"] = "completed"
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            assign_default_models(root)
            self.assertEqual(read_task(root, old["id"])["model"], DEFAULT_CODEX_MODEL)
            self.assertFalse(read_task(root, old["id"])["can_implement"])
            self.assertEqual(read_task(root, custom["id"])["model"], "gpt-6-astra")
            self.assertEqual(read_task(root, completed["id"])["model"], "")

    def test_delete_task_removes_descendants_and_preserves_siblings(self):
        """Check deletion scope and reject it while a descendant Agent runs."""
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            root = project / "tasks"
            parent = create_task(root, "親")
            child = create_task(root, "子", parent["id"])
            grandchild = create_task(root, "孫", child["id"])
            sibling = create_task(root, "別の開発項目")
            (root / child["id"] / "evidence" / "plot.png").write_bytes(b"plot")
            manager = AgentManager(root, project)
            manager.active_tasks.add(grandchild["id"])
            with self.assertRaises(ValueError):
                manager.remove_task(parent["id"])
            manager.active_tasks.clear()
            deleted = manager.remove_task(parent["id"])
            self.assertEqual(deleted, [grandchild["id"], child["id"], parent["id"]])
            self.assertFalse((root / parent["id"]).exists())
            self.assertFalse((root / child["id"]).exists())
            self.assertTrue((root / sibling["id"]).exists())

    def test_model_picker_reads_visible_codex_models(self):
        """Check app-server handshake and its picker-visible model output."""
        with tempfile.TemporaryDirectory() as temporary:
            cli = Path(temporary) / "fake-codex"
            cli.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "for line in sys.stdin:\n"
                " request = json.loads(line)\n"
                " if request['method'] == 'initialize':\n"
                "  print(json.dumps({'id': request['id'], 'result': {}}), flush=True)\n"
                " if request['method'] == 'model/list':\n"
                "  result = {'data': [{'model': 'gpt-6.1-sol', 'displayName': 'GPT-6.1 Sol', 'hidden': False}, "
                "{'model': 'hidden-model', 'displayName': 'Hidden', 'hidden': True}], 'nextCursor': None}\n"
                "  print(json.dumps({'id': request['id'], 'result': result}), flush=True)\n",
                encoding="utf-8",
            )
            cli.chmod(0o755)
            self.assertEqual(codex_models(str(cli)), [{"id": "gpt-6.1-sol", "name": "GPT-6.1 Sol"}])

    def test_agent_readiness_tracks_saved_plan_and_model(self):
        """Return after checking Agent approval, plan edits, and model invalidation."""
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            self._init_project(project)
            tasks = project / "tasks"
            tasks.mkdir()
            cli = project / "fake-codex"
            cli.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "from pathlib import Path\n"
                "prompt = sys.stdin.read()\n"
                "answered = '選択式質問への回答' in prompt\n"
                "Path('plan.md').write_text('# 計画\\n処理速度を改善', encoding='utf-8')\n"
                "data = {'message': '計画を確認しました' if answered else '対象を選んでください', "
                "'implementation_ready': answered, "
                "'reason': '実装可能' if answered else '対象が未確定', "
                "'questions': [] if answered else [{'prompt': f'対象{i}は？', 'options': ['A', 'B']} for i in range(4)]}\n"
                "Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text(json.dumps(data, ensure_ascii=False))\n",
                encoding="utf-8",
            )
            cli.chmod(0o755)
            task = create_task(tasks, "高速化")
            manager = AgentManager(tasks, project, str(cli))
            first = manager.start_chat(task["id"], "plan", "処理速度を改善したい")
            self._await_run(manager, first["id"])
            self.assertFalse(read_task(tasks, task["id"])["can_implement"])
            self.assertEqual(len(read_chat(tasks, task["id"], "plan")[-1]["questions"]), 4)
            second = manager.start_chat(task["id"], "plan", "選択式質問への回答: A")
            self._await_run(manager, second["id"])
            task = read_task(tasks, task["id"])
            self.assertTrue(task["can_implement"])
            self.assertIn("処理速度を改善", task["plan"])
            update_task(tasks, task["id"], {"model": "example-model"})
            self.assertFalse(read_task(tasks, task["id"])["can_implement"])
            with self.assertRaises(ValueError):
                manager.start_implementation(task["id"])

    def test_plan_agent_records_child_split_proposal(self):
        """Store a structured split suggestion without creating a child task."""
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            self._init_project(project)
            tasks = project / "tasks"
            cli = project / "fake-codex"
            cli.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "from pathlib import Path\n"
                "Path('plan.md').write_text('# 計画\\n作業を分割する', encoding='utf-8')\n"
                "data = {'message': '分割を提案します', 'implementation_ready': True, 'reason': '範囲を分ける', 'questions': [], "
                "'child_task_proposals': [{'title': '検証A', 'purpose': 'Aを調べる', 'scope': 'Aの実装', 'verification': 'Aを試す', 'reason': '独立して検証できる'}]}\n"
                "Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text(json.dumps(data, ensure_ascii=False))\n",
                encoding="utf-8",
            )
            cli.chmod(0o755)
            task = create_task(tasks, "親")
            manager = AgentManager(tasks, project, str(cli))
            self._await_run(manager, manager.start_chat(task["id"], "plan", "調べて")["id"])
            saved = read_task(tasks, task["id"])
            self.assertEqual(saved["child_proposals"][0]["title"], "検証A")
            self.assertFalse(saved["can_implement"])
            self.assertEqual(len([item for item in tasks.iterdir() if item.is_dir()]), 1)

    def test_evidence_is_task_local_and_safe(self):
        """Return after checking uploaded media and unsafe path rejection."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = create_task(root, "画像確認")
            self.assertTrue((root / task["id"] / "validation").is_dir())
            saved = save_evidence(root, task["id"], "plot.png", b"image-data")
            self.assertEqual(saved["kind"], "image")
            self.assertEqual(len(list_evidence(root, task["id"])), 1)
            with self.assertRaises(ValueError):
                evidence_path(root, task["id"], "../AGENTS.md")

    def test_claude_plan_uses_structured_output(self):
        """Return after checking a mock Claude CLI response and provider record."""
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            self._init_project(project)
            tasks = project / "tasks"
            tasks.mkdir()
            cli = project / "fake-claude"
            cli.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "from pathlib import Path\n"
                "Path('plan.md').write_text('# 計画\\n対象A', encoding='utf-8')\n"
                "data = {'message': '計画案です', "
                "'implementation_ready': True, 'reason': '明確です', 'questions': []}\n"
                "print(json.dumps({'structured_output': data, 'result': ''}, ensure_ascii=False))\n",
                encoding="utf-8",
            )
            cli.chmod(0o755)
            task = create_task(tasks, "Claude確認")
            update_task(tasks, task["id"], {"provider": "claude", "model": "sonnet"})
            manager = AgentManager(tasks, project)
            manager.cli_paths["claude"] = str(cli)
            run = manager.start_chat(task["id"], "plan", "対象Aを改善")
            self._await_run(manager, run["id"])
            task = read_task(tasks, task["id"])
            self.assertTrue(task["can_implement"])
            self.assertEqual(task["provider"], "claude")

    def test_result_chat_can_validate_and_revise_code(self):
        """Return after checking validation, code revision, and recorded files."""
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            self._init_project(project)
            tasks = project / "tasks"
            tasks.mkdir()
            cli = project / "fake-codex"
            cli.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "from pathlib import Path\n"
                "prompt = sys.stdin.read()\n"
                "schema = Path(sys.argv[sys.argv.index('--output-schema') + 1]).name\n"
                "if schema == 'plan-response.schema.json':\n"
                "    Path('plan.md').write_text('# 計画\\n' + ('別案' if '別案' in prompt else '改善する'), encoding='utf-8')\n"
                "    data = {'message': '計画しました', 'implementation_ready': True, 'reason': '明確', 'questions': []}\n"
                "elif '以下の承認済み計画' in prompt:\n"
                "    root = Path.cwd()\n"
                "    task = next((root / 'tasks').iterdir())\n"
                "    (root / 'src').mkdir()\n"
                "    (root / 'src' / 'change.py').write_text('VALUE = 2\\n')\n"
                "    (task / 'report-draft.md').write_text('# 検証結果\\n## 結論\\n> [!NOTE]\\n> **改善を確認**', encoding='utf-8')\n"
                "    data = {'message': '実装と検証を完了', 'outcome': 'supported', 'evidence_files': [], 'needs_followup': False}\n"
                "else:\n"
                "    root = Path.cwd()\n"
                "    task = next((root / 'tasks').iterdir())\n"
                "    latest = prompt.rsplit('user: ', 1)[-1].strip()\n"
                "    if latest == '失敗する修正':\n"
                "        (root / 'src' / 'change.py').write_text('VALUE = 4\\n')\n"
                "        sys.exit(1)\n"
                "    if latest == 'コードを直して再検証して':\n"
                "        (root / 'src' / 'change.py').write_text('VALUE = 3\\n')\n"
                "        (task / 'report-draft.md').write_text('# 検証結果\\n## 結論\\n> [!NOTE]\\n> **コード修正後に再検証**', encoding='utf-8')\n"
                "    elif latest == 'どこを直す':\n"
                "        pass\n"
                "    else:\n"
                "        (task / 'report-draft.md').write_text('# 検証結果\\n## 結論\\n> [!NOTE]\\n> **追加で画像を確認**', encoding='utf-8')\n"
                "    (task / 'evidence' / 'plot.png').write_bytes(b'image-data')\n"
                "    data = {'message': '追加の依頼を確認', 'outcome': 'inconclusive', 'evidence_files': ['evidence/plot.png'], 'needs_followup': True}\n"
                "Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text(json.dumps(data, ensure_ascii=False))\n",
                encoding="utf-8",
            )
            cli.chmod(0o755)
            task = create_task(tasks, "改善")
            manager = AgentManager(tasks, project, str(cli))
            self._await_run(manager, manager.start_chat(task["id"], "plan", "改善したい")["id"])
            self._await_run(manager, manager.start_implementation(task["id"])["id"])
            task = read_task(tasks, task["id"])
            self.assertEqual(task["implementation"]["validation_outcome"], "supported")
            self.assertIn("src/change.py", task["implementation"]["changed_files"])
            self.assertIn("改善を確認", task["result"])
            self.assertEqual(len(task["reports"]), 1)
            self.assertTrue((tasks / task["id"] / "reports" / "0001.md").exists())
            self._await_run(manager, manager.start_chat(task["id"], "result", "画像でも確かめて")["id"])
            task = read_task(tasks, task["id"])
            self.assertIn("追加で画像を確認", task["result"])
            self.assertEqual(len(task["reports"]), 2)
            self.assertIn("改善を確認", task["reports"][0]["content"])
            self.assertEqual(task["reports"][1]["evidence_files"], ["evidence/plot.png"])
            self.assertEqual(task["implementation"]["validation_outcome"], "inconclusive")
            self.assertTrue(task["implementation"]["needs_followup"])
            self.assertEqual(len(list_evidence(tasks, task["id"])), 1)
            self.assertEqual((project / "src" / "change.py").read_text(), "VALUE = 2\n")
            self._await_run(manager, manager.start_chat(task["id"], "result", "コードを直して再検証して")["id"])
            task = read_task(tasks, task["id"])
            self.assertEqual((project / "src" / "change.py").read_text(), "VALUE = 3\n")
            self.assertIn("コード修正後に再検証", task["result"])
            self.assertEqual(len(task["reports"]), 3)
            self.assertEqual(task["reports"][2]["changed_files"], ["src/change.py"])
            self.assertIn("src/change.py", task["implementation"]["changed_files"])
            self._await_run(manager, manager.start_chat(task["id"], "result", "どこを直す")["id"])
            self.assertEqual(len(read_task(tasks, task["id"])["reports"]), 3)
            self.assertNotIn("questions", read_chat(tasks, task["id"], "result")[-1])
            failed = manager.start_chat(task["id"], "result", "失敗する修正")
            self._await_run(manager, failed["id"], "failed")
            task = read_task(tasks, task["id"])
            self.assertEqual(task["implementation"]["state"], "failed")
            self.assertIn("src/change.py", task["implementation"]["changed_files"])
            self._await_run(manager, manager.start_chat(task["id"], "result", "コードを直して再検証して")["id"])
            task = read_task(tasks, task["id"])
            self.assertEqual(task["implementation"]["state"], "succeeded")
            self.assertEqual(len(task["reports"]), 4)
            self._await_run(manager, manager.start_chat(task["id"], "plan", "別案を検討")["id"])
            self.assertEqual(read_task(tasks, task["id"])["implementation"]["state"], "not_started")

    def test_existing_result_is_kept_as_first_report(self):
        """Read a legacy result and return after checking report migration."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = create_task(root, "既存タスク")
            (root / task["id"] / "result.md").write_text("# 以前の検証結果", encoding="utf-8")
            task = read_task(root, task["id"])
            self.assertEqual(task["reports"][0]["file"], "result.md")
            self.assertEqual(task["result"], "# 以前の検証結果")
            with self.assertRaises(ValueError):
                update_task(root, task["id"], {"result": "# 上書き"})

    def _init_project(self, project: Path) -> None:
        """Initialize a temporary Git project path and return nothing."""
        (project / ".gitignore").write_text("tasks/\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=project, check=True)
        subprocess.run(["git", "add", ".gitignore"], cwd=project, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-q", "-m", "Initialize test project"], cwd=project, check=True)

    def _await_run(self, manager: AgentManager, run_id: str, expected: str = "succeeded") -> None:
        """Wait for a mock run ID and assert expected state; return nothing."""
        for _ in range(100):
            run = manager.get_run(run_id)
            if run["state"] != "running":
                self.assertEqual(run["state"], expected, run["message"])
                return
            time.sleep(0.02)
        self.fail("Mock Agent run timed out")


if __name__ == "__main__":
    unittest.main()
