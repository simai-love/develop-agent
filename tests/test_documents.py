"""Check HTML plan and report storage and compatibility with legacy Markdown tasks."""

import json
import tempfile
import unittest
from pathlib import Path

from app.agent import AgentManager
from app.storage import append_report, create_task, initial_plan_html, plan_hash, read_task, report_draft_path, set_readiness, task_dir, update_task, write_text


class DocumentFormatTest(unittest.TestCase):
    """Verify which files hold plans and reports and how their formats are reported."""

    def test_new_task_uses_html_plan_and_reports(self):
        """Create a task, save an HTML plan and report, and check files, formats, and hashes."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = create_task(root, "HTML計画")
            directory = task_dir(root, task["id"])
            self.assertEqual((task["plan_file"], task["plan_format"]), ("plan.html", "html"))
            self.assertTrue((directory / "plan.html").exists())
            self.assertFalse((directory / "plan.md").exists())
            self.assertFalse((directory / "result.md").exists())
            plan = "<h1>計画</h1><h2>目的</h2><p>速くする</p>"
            task = update_task(root, task["id"], {"plan": plan})
            self.assertEqual((directory / "plan.html").read_text(encoding="utf-8"), plan)
            task = set_readiness(root, task["id"], True, plan, "実装可能")
            self.assertEqual(task["readiness"]["plan_hash"], plan_hash(plan))
            self.assertTrue(task["can_implement"])
            self.assertEqual(report_draft_path(root, task["id"]).name, "report-draft.html")
            write_text(report_draft_path(root, task["id"]), "<h2>結論</h2><p>改善した</p>")
            task = append_report(root, task["id"], "supported", ["src/a.py"], [], "codex", "", False)
            self.assertEqual(task["reports"][0]["file"], "reports/0001.html")
            self.assertEqual(task["reports"][0]["format"], "html")
            self.assertIn("改善した", task["result"])
            metadata = json.loads((directory / "task.json").read_text(encoding="utf-8"))
            self.assertNotIn("plan_format", metadata)
            self.assertNotIn("format", metadata["reports"][0])

    def test_legacy_markdown_plan_and_reports_stay_markdown(self):
        """Read a task that only has plan.md and Markdown reports; check it keeps using them."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = create_task(root, "既存計画")
            directory = task_dir(root, task["id"])
            (directory / "plan.html").unlink()
            write_text(directory / "plan.md", "# 計画\n## 目的\n既存")
            write_text(directory / "reports" / "0001.md", "# 検証結果\n## 結論\n以前の結果")
            metadata = json.loads((directory / "task.json").read_text(encoding="utf-8"))
            metadata["reports"] = [{"number": 1, "file": "reports/0001.md", "created_at": metadata["created_at"], "outcome": "supported", "changed_files": [], "evidence_files": []}]
            write_text(directory / "task.json", json.dumps(metadata, ensure_ascii=False))
            task = read_task(root, task["id"])
            self.assertEqual((task["plan_file"], task["plan_format"]), ("plan.md", "markdown"))
            self.assertEqual(task["plan"], "# 計画\n## 目的\n既存")
            self.assertEqual(task["reports"][0]["format"], "markdown")
            update_task(root, task["id"], {"plan": "# 計画\n## 目的\n更新"})
            self.assertEqual((directory / "plan.md").read_text(encoding="utf-8"), "# 計画\n## 目的\n更新")
            self.assertFalse((directory / "plan.html").exists())
            write_text(report_draft_path(root, task["id"]), "<h2>結論</h2><p>新しい結果</p>")
            task = append_report(root, task["id"], "supported", [], [], "codex", "", False)
            self.assertEqual([report["format"] for report in task["reports"]], ["markdown", "html"])
            prompt = AgentManager(root, root)._chat_prompt(task["id"], "plan")
            self.assertIn("plan.md はMarkdownのまま更新", prompt)

    def test_initial_child_plan_is_escaped_html(self):
        """Build an initial child plan from proposal text and check headings and escaping."""
        plan = initial_plan_html("子<A>", [("目的", "a & b"), ("確かめ方", "<script>x</script>")])
        self.assertIn("<h1>子&lt;A&gt;</h1>", plan)
        self.assertIn("<h2>目的</h2>\n<p>a &amp; b</p>", plan)
        self.assertNotIn("<script>", plan)

    def test_markdown_draft_is_reported_as_misplaced(self):
        """Write a report to the old Markdown draft path and check the validation error."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = create_task(root, "下書き")
            write_text(report_draft_path(root, task["id"]), "")
            write_text(task_dir(root, task["id"]) / "report-draft.md", "# 結果")
            response = {"message": "完了", "outcome": "supported", "needs_followup": False, "evidence_files": [], "child_task_proposals": [], "commit_message": ""}
            with self.assertRaisesRegex(ValueError, "report-draft.html"):
                AgentManager(root, root)._validate_result(task["id"], response)


if __name__ == "__main__":
    unittest.main()
