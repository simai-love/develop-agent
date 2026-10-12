"""Exercise project creation through the local HTTP API."""

import json
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from app import server
from app.agent import AgentManager
from app.workflow import WorkflowService


class ProjectApiTest(unittest.TestCase):
    """Check API routes with isolated storage and Git history."""

    def test_project_and_child_creation_routes(self):
        """Create project and child over HTTP and verify hierarchy responses."""
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "dev"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            (repo / ".gitignore").write_text("tasks/\nprojects/\n", encoding="utf-8")
            (repo / "source.py").write_text("old\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "初期化"], cwd=repo, check=True)
            old = (server.PROJECT_ROOT, server.TASKS_ROOT, server.PROJECTS_ROOT, server.WORKFLOW, server.AGENT)
            workflow = WorkflowService(repo, repo / "tasks", repo / "projects")
            agent = AgentManager(repo / "tasks", repo, workflow=workflow)
            workflow.agent = agent
            server.PROJECT_ROOT, server.TASKS_ROOT, server.PROJECTS_ROOT, server.WORKFLOW, server.AGENT = repo, workflow.tasks_root, workflow.projects_root, workflow, agent
            http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()
            try:
                base = f"http://127.0.0.1:{http.server_port}"
                request = Request(base + "/api/projects", data=json.dumps({"name": "検証", "slug": "validation", "first_task_title": "初回タスク"}).encode(), headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(request) as response:
                    project = json.load(response)
                branch = project["branch"]
                request = Request(base + f"/api/projects/{project['id']}", data=json.dumps({"name": "新しい検証名"}).encode(), headers={"Content-Type": "application/json"}, method="PATCH")
                with urlopen(request) as response:
                    renamed = json.load(response)
                self.assertEqual(renamed["name"], "新しい検証名")
                self.assertEqual(renamed["branch"], branch)
                with urlopen(base + "/api/projects") as response:
                    self.assertEqual(json.load(response)[0]["name"], "新しい検証名")
                task_id = project["first_task_id"]
                request = Request(base + f"/api/projects/{project['id']}/tasks", data=json.dumps({"title": "追加タスク"}).encode(), headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(request) as response:
                    root_task = json.load(response)
                self.assertIsNone(root_task["parent_id"])
                self.assertEqual(root_task["project_id"], project["id"])
                self.assertEqual(workflow.branch_for(root_task["id"]), branch)
                request = Request(base + f"/api/tasks/{task_id}/children", data=json.dumps({"title": "子タスク"}).encode(), headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(request) as response:
                    child = json.load(response)
                with urlopen(base + "/api/tasks") as response:
                    tasks = json.load(response)
                self.assertEqual(child["parent_id"], task_id)
                self.assertEqual(len(tasks), 3)
                self.assertEqual(workflow.branch_for(child["id"]), f"task/初回タスク-{task_id}")
                workflow.claim_code(child["id"])
                (workflow.code_path(child["id"]) / "change note.txt").write_text("updated\n", encoding="utf-8")
                (workflow.code_path(child["id"]) / "source.py").write_text("new\n", encoding="utf-8")
                diff_url = base + f"/api/tasks/{child['id']}/diff"
                with urlopen(diff_url) as response:
                    self.assertEqual(json.load(response)["files"], ["change note.txt", "source.py"])
                with urlopen(diff_url + "?path=" + quote("change note.txt")) as response:
                    self.assertIn("+updated", json.load(response)["patch"])
                with urlopen(diff_url + "?path=source.py") as response:
                    self.assertIn("-old\n+new", json.load(response)["patch"])
                with self.assertRaises(HTTPError) as error:
                    urlopen(diff_url + "?path=../README.md")
                self.assertEqual(error.exception.code, 400)
            finally:
                http.shutdown()
                http.server_close()
                thread.join(timeout=2)
                server.PROJECT_ROOT, server.TASKS_ROOT, server.PROJECTS_ROOT, server.WORKFLOW, server.AGENT = old


if __name__ == "__main__":
    unittest.main()
