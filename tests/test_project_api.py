"""Exercise project creation through the local HTTP API."""

import json
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
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
                task_id = project["first_task_id"]
                request = Request(base + f"/api/tasks/{task_id}/children", data=json.dumps({"title": "子タスク"}).encode(), headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(request) as response:
                    child = json.load(response)
                with urlopen(base + "/api/tasks") as response:
                    tasks = json.load(response)
                self.assertEqual(child["parent_id"], task_id)
                self.assertEqual(len(tasks), 2)
                self.assertEqual(workflow.branch_for(child["id"]), f"task/{task_id}")
            finally:
                http.shutdown()
                http.server_close()
                thread.join(timeout=2)
                server.PROJECT_ROOT, server.TASKS_ROOT, server.PROJECTS_ROOT, server.WORKFLOW, server.AGENT = old


if __name__ == "__main__":
    unittest.main()
