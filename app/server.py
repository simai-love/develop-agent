"""Serve the local GUI and its file-backed task API."""

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.agent import AgentManager
from app.evidence import MAX_EVIDENCE_BYTES, evidence_path, list_evidence, save_evidence
from app.projects import list_projects, read_project
from app.storage import assign_default_models, list_tasks, plan_hash, read_chat, read_task, update_task
from app.task_status import display_status
from app.workflow import WorkflowService


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_ROOT = Path(__file__).resolve().parent / "static"
TASKS_ROOT = PROJECT_ROOT / "tasks"
PROJECTS_ROOT = PROJECT_ROOT / "projects"
WORKFLOW = WorkflowService(PROJECT_ROOT, TASKS_ROOT, PROJECTS_ROOT)
AGENT = AgentManager(TASKS_ROOT, PROJECT_ROOT, workflow=WORKFLOW)
WORKFLOW.agent = AGENT


class Handler(BaseHTTPRequestHandler):
    """Handle local HTTP requests and return HTML assets or JSON task records."""

    def send_json(self, status: int, value: object) -> None:
        """Send value as a JSON response with the given HTTP status."""
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        """Read a JSON request body and return its object value."""
        return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

    def present_task(self, task: dict) -> dict:
        """Add the task's current display status and return its API record."""
        with AGENT.lock:
            active = task["id"] in AGENT.active_tasks
            active_run_id = next((run_id for run_id, run in AGENT.runs.items() if run["task_id"] == task["id"] and run["state"] == "running"), None)
        full_task = task if "plan" in task else read_task(TASKS_ROOT, task["id"])
        children = WORKFLOW.children(task["id"])
        proposals = [{**item, "stale": item["plan_hash"] != plan_hash(full_task["plan"])} for item in task.get("child_proposals", [])]
        return {**task, "can_implement": full_task["can_implement"], "child_proposals": proposals, "active": active, "active_run_id": active_run_id, "child_count": len(children), "display_status": display_status(full_task, read_chat(TASKS_ROOT, task["id"], "plan"), active, children)}

    def present_project(self, project: dict) -> dict:
        """Add task counts to project and return its API record."""
        tasks = WORKFLOW.project_tasks(project["id"])
        return {**project, "task_count": len(tasks), "completed_count": sum(task["status"] == "completed" for task in tasks)}

    def do_GET(self) -> None:
        """Return a task list, one task, or a static GUI asset."""
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/agent":
            self.send_json(200, AGENT.availability())
            return
        if path == "/api/projects":
            self.send_json(200, [self.present_project(project) for project in list_projects(PROJECTS_ROOT)])
            return
        if path.startswith("/api/projects/"):
            project_id = path.removeprefix("/api/projects/").removesuffix("/completion-preview")
            try:
                value = WORKFLOW.project_preview(project_id) if path.endswith("/completion-preview") else self.present_project(read_project(PROJECTS_ROOT, project_id))
                self.send_json(200, value)
            except (FileNotFoundError, ValueError) as error:
                self.send_json(400, {"error": str(error)})
            return
        if path.startswith("/api/runs/"):
            run = AGENT.get_run(path.removeprefix("/api/runs/"))
            self.send_json(200 if run else 404, run or {"error": "Run not found"})
            return
        if path == "/api/tasks":
            self.send_json(200, [self.present_task(task) for task in list_tasks(TASKS_ROOT)])
            return
        if path.startswith("/api/tasks/") and path.endswith("/completion-preview"):
            try:
                task_id = path.removeprefix("/api/tasks/").removesuffix("/completion-preview")
                self.send_json(200, WORKFLOW.completion_preview(task_id))
            except (FileNotFoundError, ValueError) as error:
                self.send_json(400, {"error": str(error)})
            return
        if path.startswith("/api/tasks/") and path.endswith("/diff"):
            try:
                task_id = path.removeprefix("/api/tasks/").removesuffix("/diff")
                selected_path = parse_qs(parsed.query).get("path", [None])[0]
                self.send_json(200, WORKFLOW.task_diff(task_id, selected_path))
            except (FileNotFoundError, ValueError) as error:
                self.send_json(400, {"error": str(error)})
            return
        if path.startswith("/api/tasks/") and "/evidence" in path:
            parts = path.strip("/").split("/")
            try:
                if len(parts) == 4 and parts[3] == "evidence":
                    read_task(TASKS_ROOT, parts[2])
                    self.send_json(200, list_evidence(TASKS_ROOT, parts[2]))
                    return
                if len(parts) == 5 and parts[3] == "evidence":
                    file_path = evidence_path(TASKS_ROOT, parts[2], parts[4])
                    body = file_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
            except (FileNotFoundError, ValueError):
                self.send_json(404, {"error": "Evidence not found"})
                return
        if path.startswith("/api/tasks/") and path.endswith("/chat"):
            task_id = path.removeprefix("/api/tasks/").removesuffix("/chat")
            phase = parse_qs(parsed.query).get("phase", ["plan"])[0]
            try:
                read_task(TASKS_ROOT, task_id)
                self.send_json(200, read_chat(TASKS_ROOT, task_id, phase))
            except (FileNotFoundError, ValueError):
                self.send_json(404, {"error": "Task or chat phase not found"})
            return
        if path.startswith("/api/tasks/"):
            try:
                self.send_json(200, self.present_task(read_task(TASKS_ROOT, path.removeprefix("/api/tasks/"))))
            except (FileNotFoundError, ValueError):
                self.send_json(404, {"error": "Task not found"})
            return
        filename = {"/": "index.html", "/styles.css": "styles.css", "/app.js": "app.js", "/markdown.mjs": "markdown.mjs"}.get(path)
        if filename is None:
            self.send_error(404)
            return
        body = (STATIC_ROOT / filename).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript" if filename.endswith(".mjs") else mimetypes.guess_type(filename)[0] or "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        """Create a task or Agent run from JSON and return its record."""
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/projects" or path.startswith("/api/projects/") or path.startswith("/api/tasks/") and (path.endswith("/children") or path.endswith("/summary") or path.endswith("/parallel-execute") or "/proposals/" in path):
            try:
                body = self.read_json()
                if path == "/api/projects":
                    self.send_json(201, self.present_project(WORKFLOW.create_project(body["name"], body["slug"], body.get("first_task_title") or body["name"])))
                elif path.startswith("/api/projects/") and path.endswith("/tasks"):
                    project_id = path.removeprefix("/api/projects/").removesuffix("/tasks")
                    self.send_json(201, self.present_task(WORKFLOW.create_root_task(project_id, body["title"])))
                elif path.startswith("/api/projects/") and path.endswith("/complete"):
                    self.send_json(200, self.present_project(WORKFLOW.complete_project(path.removeprefix("/api/projects/").removesuffix("/complete"), body)))
                elif path.startswith("/api/projects/") and path.endswith("/acknowledge-migration"):
                    project_id = path.removeprefix("/api/projects/").removesuffix("/acknowledge-migration")
                    self.send_json(200, self.present_project(WORKFLOW.acknowledge_migration(project_id)))
                elif path.endswith("/children"):
                    task_id = path.removeprefix("/api/tasks/").removesuffix("/children")
                    self.send_json(201, self.present_task(WORKFLOW.create_child(task_id, body["title"], body.get("initial_plan", ""))))
                elif path.endswith("/summary"):
                    task_id = path.removeprefix("/api/tasks/").removesuffix("/summary")
                    self.send_json(202, AGENT.start_summary(task_id))
                elif path.endswith("/parallel-execute"):
                    task_id = path.removeprefix("/api/tasks/").removesuffix("/parallel-execute")
                    self.send_json(202, AGENT.start_parallel_children(task_id, body["task_ids"]))
                elif "/proposals/" in path:
                    parts = path.strip("/").split("/")
                    if len(parts) != 6 or parts[0] != "api" or parts[1] != "tasks" or parts[3] != "proposals":
                        self.send_error(404)
                        return
                    task_id, proposal_id, action = parts[2], parts[4], parts[5]
                    if action == "approve":
                        task = read_task(TASKS_ROOT, task_id)
                        proposal = next((item for item in task["child_proposals"] if item["id"] == proposal_id), None)
                        if not proposal:
                            raise ValueError("分割案が見つかりません")
                        title = (body.get("title") or proposal["title"]).strip()
                        purpose = (body.get("purpose") or proposal["purpose"]).strip()
                        scope = (body.get("scope") or proposal["scope"]).strip()
                        verification = (body.get("verification") or proposal["verification"]).strip()
                        if not all((title, purpose, scope, verification)):
                            raise ValueError("子タスク案の各項目を入力してください")
                        initial_plan = "\n".join((f"# {title}", f"## 目的\n{purpose}", f"## 実装方針\n{scope}", f"## 確かめ方\n{verification}"))
                        self.send_json(201, self.present_task(WORKFLOW.create_child(task_id, title, initial_plan, proposal_id)))
                    elif action == "reject":
                        self.send_json(200, self.present_task(WORKFLOW.reject_proposal(task_id, proposal_id)))
                    else:
                        self.send_error(404)
                else:
                    self.send_error(404)
            except (KeyError, TypeError, ValueError, FileNotFoundError) as error:
                self.send_json(400, {"error": str(error)})
            return
        if path.startswith("/api/tasks/") and path.endswith("/evidence"):
            try:
                task_id = path.removeprefix("/api/tasks/").removesuffix("/evidence")
                read_task(TASKS_ROOT, task_id)
                size = int(self.headers.get("Content-Length", "0"))
                if size < 1 or size > MAX_EVIDENCE_BYTES:
                    raise ValueError("Evidence must be between 1 byte and 100 MB")
                name = parse_qs(parsed.query).get("name", [""])[0]
                self.send_json(201, save_evidence(TASKS_ROOT, task_id, name, self.rfile.read(size)))
            except (FileNotFoundError, ValueError) as error:
                self.send_json(400, {"error": str(error)})
            return
        if path not in {"/api/tasks"} and not (path.startswith("/api/tasks/") and path.endswith(("/chat", "/execute", "/complete"))):
            self.send_error(404)
            return
        try:
            body = self.read_json()
            if path == "/api/tasks":
                raise ValueError("プロジェクトまたは親タスクから作成してください")
            elif path.endswith("/chat"):
                task_id = path.removeprefix("/api/tasks/").removesuffix("/chat")
                self.send_json(202, AGENT.start_chat(task_id, body["phase"], body["message"]))
            elif path.endswith("/complete"):
                task_id = path.removeprefix("/api/tasks/").removesuffix("/complete")
                if task_id in AGENT.active_tasks:
                    raise ValueError("Cannot complete a task during an Agent run")
                self.send_json(200, self.present_task(WORKFLOW.complete_task(task_id, body)))
            else:
                task_id = path.removeprefix("/api/tasks/").removesuffix("/execute")
                self.send_json(202, AGENT.start_implementation(task_id))
        except (KeyError, TypeError, ValueError, FileNotFoundError) as error:
            self.send_json(400, {"error": str(error)})

    def do_PATCH(self) -> None:
        """Update a task from a JSON request and return its record."""
        path = urlparse(self.path).path
        if path.startswith("/api/projects/") and "/" not in path.removeprefix("/api/projects/"):
            try:
                body = self.read_json()
                project_id = path.removeprefix("/api/projects/")
                if set(body) == {"name"}:
                    project = WORKFLOW.rename_project(project_id, body["name"])
                elif set(body) == {"slug"}:
                    project = WORKFLOW.rename_legacy_branch(project_id, body["slug"])
                else:
                    raise ValueError("更新するプロジェクト項目を指定してください")
                self.send_json(200, self.present_project(project))
            except (KeyError, TypeError, ValueError, FileNotFoundError) as error:
                self.send_json(400, {"error": str(error)})
            return
        if not path.startswith("/api/tasks/"):
            self.send_error(404)
            return
        try:
            task_id = path.removeprefix("/api/tasks/")
            changes = self.read_json()
            if task_id in AGENT.active_tasks and ({"title", "plan", "model", "provider"} & changes.keys()):
                raise ValueError("Cannot change task metadata during an Agent run")
            self.send_json(200, self.present_task(update_task(TASKS_ROOT, task_id, changes)))
        except FileNotFoundError:
            self.send_json(404, {"error": "Task not found"})
        except (KeyError, TypeError, ValueError) as error:
            self.send_json(400, {"error": str(error)})

    def do_DELETE(self) -> None:
        """Delete one task and its descendants; return their IDs."""
        path = urlparse(self.path).path
        if path.startswith("/api/projects/") and "/" not in path.removeprefix("/api/projects/"):
            try:
                WORKFLOW.delete_project(path.removeprefix("/api/projects/"))
                self.send_json(200, {"deleted": True})
            except (FileNotFoundError, ValueError) as error:
                self.send_json(400, {"error": str(error)})
            return
        if not path.startswith("/api/tasks/") or "/" in path.removeprefix("/api/tasks/"):
            self.send_error(404)
            return
        try:
            deleted_ids = WORKFLOW.delete_task(path.removeprefix("/api/tasks/"))
            self.send_json(200, {"deleted_ids": deleted_ids})
        except FileNotFoundError:
            self.send_json(404, {"error": "Task not found"})
        except ValueError as error:
            self.send_json(400, {"error": str(error)})


def main() -> None:
    """Start the local GUI server and return when it is interrupted."""
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    AGENT.recover_interrupted_runs()
    WORKFLOW.migrate_legacy()
    WORKFLOW.recover_operations()
    assign_default_models(TASKS_ROOT)
    print("Develop Agent: http://127.0.0.1:8765", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
