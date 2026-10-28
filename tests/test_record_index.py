"""Check readable record links without changing stable task and project IDs."""

import json
import tempfile
import unittest
from pathlib import Path

from app.projects import save_project
from app.record_index import readable_record_name, sync_record_links
from app.storage import create_task, delete_task, list_tasks, read_task, update_task


class RecordIndexTests(unittest.TestCase):
    """Keep human-readable links separate from the record storage path."""

    def test_task_link_keeps_id_path_and_survives_title_change(self):
        """Create one dated link, keep it stable on rename, and remove it with the task."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "tasks"
            task = create_task(root, "結果 / 図の確認")
            links = list((root / "by-name").iterdir())
            self.assertEqual(len(links), 1)
            self.assertTrue(links[0].is_symlink())
            self.assertEqual(links[0].resolve(), root / task["id"])
            self.assertIn("結果-図の確認", links[0].name)
            update_task(root, task["id"], {"title": "改名後"})
            self.assertEqual(read_task(root, task["id"])["title"], "改名後")
            self.assertEqual(list((root / "by-name").iterdir()), links)
            self.assertEqual([item["id"] for item in list_tasks(root)], [task["id"]])
            delete_task(root, task["id"])
            self.assertEqual(list((root / "by-name").iterdir()), [])

    def test_existing_task_and_project_get_dated_links(self):
        """Index old ID folders using their saved creation times and names."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_root = root / "tasks"
            old_task = task_root / "abcdef123456"
            old_task.mkdir(parents=True)
            old_task.joinpath("task.json").write_text(json.dumps({
                "id": "abcdef123456", "title": "文書作成", "created_at": "2026-10-24T23:04:00+00:00",
            }), encoding="utf-8")
            sync_record_links(task_root, "task.json", "title")
            link = task_root / "by-name" / "2026-10-25_0804_文書作成_abcdef123456"
            self.assertEqual(link.resolve(), old_task)
            sync_record_links(task_root, "task.json", "title")
            self.assertEqual(len(list((task_root / "by-name").iterdir())), 1)

            project_root = root / "projects"
            project = {"id": "123456abcdef", "name": "旧プロジェクト", "created_at": "2026-10-24T23:04:00+00:00"}
            save_project(project_root, project)
            self.assertEqual((project_root / "by-name" / "2026-10-25_0804_旧プロジェクト_123456abcdef").resolve(), project_root / project["id"])
            self.assertEqual(readable_record_name("図/評価", project["id"], project["created_at"]), "2026-10-25_0804_図-評価_123456abcdef")
