"""Serve the local GUI and its file-backed task API."""

import json
import mimetypes
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.agent import AgentManager
from app.file_browser import browse_root, list_directory, raw_headers, read_content, resolve_path
from app.evidence import MAX_EVIDENCE_BYTES, evidence_path, list_evidence, save_evidence
from app.repositories import RepositoryRegistry
from app.record_index import sync_record_links
from app.storage import assign_default_models, list_tasks, plan_hash, read_chat, read_task, task_dir, update_task
from app.task_status import display_status
from app.task_workflow import TaskWorkflowService
from app.workflow import WorkflowService as LegacyWorkflowService


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_ROOT = Path(__file__).resolve().parent / "static"
KATEX_ROOT = Path(__file__).resolve().parent / "cli" / "node_modules" / "katex" / "dist"
TASKS_ROOT = PROJECT_ROOT / "tasks"
PROJECTS_ROOT = PROJECT_ROOT / "projects"
WORKFLOW = TaskWorkflowService(PROJECT_ROOT, TASKS_ROOT, PROJECTS_ROOT)
AGENT = AgentManager(TASKS_ROOT, PROJECT_ROOT, workflow=WORKFLOW)
WORKFLOW.agent = AGENT
REGISTRY = RepositoryRegistry(PROJECT_ROOT / ".codex" / "repositories.local.json", AGENT.cli_paths["codex"], TASKS_ROOT)


class Handler(BaseHTTPRequestHandler):
    """Handle local HTTP requests and return HTML assets or JSON task records."""

    def load_context(self) -> bool:
        """Select the request's repository services and return whether it exists."""
        repository_id = parse_qs(urlparse(self.path).query).get("repo", ["develop-agent"])[0]
        try:
            if repository_id == "develop-agent":
                self.tasks_root, self.workflow, self.agent = TASKS_ROOT, WORKFLOW, AGENT
            else:
                context = REGISTRY.get(repository_id)
                self.tasks_root, self.workflow, self.agent = context.tasks_root, context.workflow, context.agent
            return True
        except (KeyError, OSError, ValueError) as error:
            self.send_json(400, {"error": str(error)})
            return False

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
        with self.agent.lock:
            active = task["id"] in self.agent.active_tasks
            active_run_id = next((run_id for run_id, run in self.agent.runs.items() if run["task_id"] == task["id"] and run["state"] == "running"), None)
        full_task = task if "plan" in task else read_task(self.tasks_root, task["id"])
        children = self.workflow.children(task["id"])
        proposals = [{**item, "stale": item["plan_hash"] != plan_hash(full_task["plan"])} for item in task.get("child_proposals", [])]
        return {**task, "storage_path": str(task_dir(self.tasks_root, task["id"])), "can_implement": full_task["can_implement"], "child_proposals": proposals, "active": active, "active_run_id": active_run_id, "child_count": len(children), "display_status": display_status(full_task, read_chat(self.tasks_root, task["id"], "plan"), active, children)}

    def send_file_view(self, path: str, query: dict) -> None:
        """Send a read-only directory listing, file content, or raw file for query."""
        try:
            root = browse_root(self.workflow, self.tasks_root, query.get("task", [None])[0])
            relative = query.get("path", [""])[0]
            if path == "/api/files":
                self.send_json(200, {**list_directory(root["path"], relative), "root": str(root["path"]), "root_kind": root["kind"], "branch": root["branch"]})
            elif path == "/api/files/content":
                self.send_json(200, read_content(root["path"], relative))
            else:
                file_path = resolve_path(root["path"], relative)
                if not file_path.is_file():
                    raise IsADirectoryError("ファイルではありません")
                self.send_response(200)
                for name, value in raw_headers(file_path, query.get("download", ["0"])[0] == "1").items():
                    self.send_header(name, value)
                self.end_headers()
                with file_path.open("rb") as source:
                    shutil.copyfileobj(source, self.wfile)
        except FileNotFoundError:
            self.send_json(404, {"error": "ファイルが見つかりません"})
        except (NotADirectoryError, IsADirectoryError, ValueError) as error:
            self.send_json(400, {"error": str(error)})

    def do_GET(self) -> None:
        """Return a task list, one task, or a static GUI asset."""
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/repositories":
            try:
                entries = [{"id": "develop-agent", "name": "Develop Agent", "path": str(PROJECT_ROOT)}]
                entries.extend({"id": item["id"], "name": item["name"], "path": item["path"]} for item in REGISTRY.entries())
                self.send_json(200, entries)
            except (KeyError, ValueError) as error:
                self.send_json(400, {"error": str(error)})
            return
        if path == "/api/repository-candidates":
            try:
                self.send_json(200, REGISTRY.candidates())
            except (OSError, ValueError) as error:
                self.send_json(400, {"error": str(error)})
            return
        if path.startswith("/api/") and not self.load_context():
            return
        if path == "/api/agent":
            self.send_json(200, self.agent.availability())
            return
        if path.startswith("/api/runs/"):
            run = self.agent.get_run(path.removeprefix("/api/runs/"))
            self.send_json(200 if run else 404, run or {"error": "Run not found"})
            return
        if path == "/api/tasks":
            self.send_json(200, [self.present_task(task) for task in list_tasks(self.tasks_root)])
            return
        if path in {"/api/files", "/api/files/content", "/api/files/raw"}:
            self.send_file_view(path, parse_qs(parsed.query))
            return
        if path.startswith("/api/tasks/") and path.endswith("/completion-preview"):
            try:
                task_id = path.removeprefix("/api/tasks/").removesuffix("/completion-preview")
                self.send_json(200, self.workflow.completion_preview(task_id))
            except (FileNotFoundError, ValueError) as error:
                self.send_json(400, {"error": str(error)})
            return
        if path.startswith("/api/tasks/") and path.endswith("/diff"):
            try:
                task_id = path.removeprefix("/api/tasks/").removesuffix("/diff")
                selected_path = parse_qs(parsed.query).get("path", [None])[0]
                self.send_json(200, self.workflow.task_diff(task_id, selected_path))
            except (FileNotFoundError, ValueError) as error:
                self.send_json(400, {"error": str(error)})
            return
        if path.startswith("/api/tasks/") and "/evidence" in path:
            parts = path.strip("/").split("/")
            try:
                if len(parts) == 4 and parts[3] == "evidence":
                    read_task(self.tasks_root, parts[2])
                    self.send_json(200, list_evidence(self.tasks_root, parts[2]))
                    return
                if len(parts) == 5 and parts[3] == "evidence":
                    file_path = evidence_path(self.tasks_root, parts[2], parts[4])
                    size = file_path.stat().st_size
                    self.send_response(200)
                    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
                    self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type == "text/plain" else content_type)
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Content-Length", str(size))
                    self.end_headers()
                    with file_path.open("rb") as source:
                        shutil.copyfileobj(source, self.wfile)
                    return
            except (FileNotFoundError, ValueError):
                self.send_json(404, {"error": "Evidence not found"})
                return
        if path.startswith("/api/tasks/") and path.endswith("/chat"):
            task_id = path.removeprefix("/api/tasks/").removesuffix("/chat")
            phase = parse_qs(parsed.query).get("phase", ["plan"])[0]
            try:
                read_task(self.tasks_root, task_id)
                self.send_json(200, read_chat(self.tasks_root, task_id, phase))
            except (FileNotFoundError, ValueError):
                self.send_json(404, {"error": "Task or chat phase not found"})
            return
        if path.startswith("/api/tasks/"):
            try:
                self.send_json(200, self.present_task(read_task(self.tasks_root, path.removeprefix("/api/tasks/"))))
            except (FileNotFoundError, ValueError):
                self.send_json(404, {"error": "Task not found"})
            return
        if path.startswith("/vendor/katex/"):
            filename = path.removeprefix("/vendor/katex/")
            asset = (KATEX_ROOT / filename).resolve()
            if not asset.is_relative_to(KATEX_ROOT) or not asset.is_file():
                self.send_error(404)
                return
        else:
            filename = {"/": "index.html", "/styles.css": "styles.css", "/app.js": "app.js", "/markdown.mjs": "markdown.mjs", "/files.mjs": "files.mjs", "/files.css": "files.css"}.get(path)
            if filename is None:
                self.send_error(404)
                return
            asset = STATIC_ROOT / filename
        body = asset.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/javascript" if filename.endswith(".mjs") else mimetypes.guess_type(filename)[0] or "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        """Create a task or Agent run from JSON and return its record."""
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/repositories":
            try:
                body = self.read_json()
                self.send_json(201, REGISTRY.add(body["name"], body["path"], body.get("gpu", False)))
            except (KeyError, OSError, TypeError, ValueError) as error:
                self.send_json(400, {"error": str(error)})
            return
        if path.startswith("/api/") and not self.load_context():
            return
        if path.startswith("/api/runs/") and path.endswith("/cancel"):
            try:
                run_id = path.removeprefix("/api/runs/").removesuffix("/cancel")
                self.send_json(202, self.agent.cancel_run(run_id))
            except ValueError as error:
                self.send_json(400, {"error": str(error)})
            return
        if path.startswith("/api/tasks/") and (path.endswith("/children") or path.endswith("/summary") or path.endswith("/parallel-execute") or "/proposals/" in path):
            try:
                body = self.read_json()
                if path.endswith("/children"):
                    task_id = path.removeprefix("/api/tasks/").removesuffix("/children")
                    self.send_json(201, self.present_task(self.workflow.create_child(task_id, body["title"], body.get("initial_plan", ""))))
                elif path.endswith("/summary"):
                    task_id = path.removeprefix("/api/tasks/").removesuffix("/summary")
                    self.send_json(202, self.agent.start_summary(task_id, body.get("additional_instruction", "")))
                elif path.endswith("/parallel-execute"):
                    task_id = path.removeprefix("/api/tasks/").removesuffix("/parallel-execute")
                    self.send_json(202, self.agent.start_parallel_children(task_id, body["task_ids"], body.get("additional_instruction", "")))
                elif "/proposals/" in path:
                    parts = path.strip("/").split("/")
                    if len(parts) != 6 or parts[0] != "api" or parts[1] != "tasks" or parts[3] != "proposals":
                        self.send_error(404)
                        return
                    task_id, proposal_id, action = parts[2], parts[4], parts[5]
                    if action == "approve":
                        task = read_task(self.tasks_root, task_id)
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
                        self.send_json(201, self.present_task(self.workflow.create_child(task_id, title, initial_plan, proposal_id)))
                    elif action == "reject":
                        self.send_json(200, self.present_task(self.workflow.reject_proposal(task_id, proposal_id)))
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
                read_task(self.tasks_root, task_id)
                size = int(self.headers.get("Content-Length", "0"))
                if size < 1 or size > MAX_EVIDENCE_BYTES:
                    raise ValueError("Evidence must be between 1 byte and 100 MB")
                name = parse_qs(parsed.query).get("name", [""])[0]
                self.send_json(201, save_evidence(self.tasks_root, task_id, name, self.rfile.read(size)))
            except (FileNotFoundError, ValueError) as error:
                self.send_json(400, {"error": str(error)})
            return
        if path not in {"/api/tasks"} and not (path.startswith("/api/tasks/") and path.endswith(("/chat", "/execute", "/complete"))):
            self.send_error(404)
            return
        try:
            body = self.read_json()
            if path == "/api/tasks":
                self.send_json(201, self.present_task(self.workflow.create_root_task(body["title"])))
            elif path.endswith("/chat"):
                task_id = path.removeprefix("/api/tasks/").removesuffix("/chat")
                self.send_json(202, self.agent.start_chat(task_id, body["phase"], body.get("message", ""), body.get("images", [])))
            elif path.endswith("/complete"):
                task_id = path.removeprefix("/api/tasks/").removesuffix("/complete")
                if task_id in self.agent.active_tasks:
                    raise ValueError("Cannot complete a task during an Agent run")
                if body.get("kind") in {"commit", "merge"}:
                    self.send_json(202, self.agent.start_completion(task_id, body, body.get("commit_instruction", "")))
                else:
                    self.send_json(200, self.present_task(self.workflow.complete_task(task_id, body)))
            else:
                task_id = path.removeprefix("/api/tasks/").removesuffix("/execute")
                self.send_json(202, self.agent.start_implementation(task_id, body.get("additional_instruction", "")))
        except (KeyError, TypeError, ValueError, FileNotFoundError) as error:
            self.send_json(400, {"error": str(error)})

    def do_PATCH(self) -> None:
        """Update a task from a JSON request and return its record."""
        path = urlparse(self.path).path
        if path.startswith("/api/") and not self.load_context():
            return
        if not path.startswith("/api/tasks/"):
            self.send_error(404)
            return
        try:
            task_id = path.removeprefix("/api/tasks/")
            changes = self.read_json()
            if task_id in self.agent.active_tasks and ({"title", "plan", "model", "provider"} & changes.keys()):
                raise ValueError("Cannot change task metadata during an Agent run")
            self.send_json(200, self.present_task(update_task(self.tasks_root, task_id, changes)))
        except FileNotFoundError:
            self.send_json(404, {"error": "Task not found"})
        except (KeyError, TypeError, ValueError) as error:
            self.send_json(400, {"error": str(error)})

    def do_DELETE(self) -> None:
        """Delete one task and its descendants; return their IDs."""
        path = urlparse(self.path).path
        if path.startswith("/api/") and not self.load_context():
            return
        if not path.startswith("/api/tasks/") or "/" in path.removeprefix("/api/tasks/"):
            self.send_error(404)
            return
        try:
            deleted_ids = self.workflow.delete_task(path.removeprefix("/api/tasks/"))
            self.send_json(200, {"deleted_ids": deleted_ids})
        except FileNotFoundError:
            self.send_json(404, {"error": "Task not found"})
        except ValueError as error:
            self.send_json(400, {"error": str(error)})


def main() -> None:
    """Start the local GUI server and return when it is interrupted."""
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    AGENT.recover_interrupted_runs()
    LegacyWorkflowService(PROJECT_ROOT, TASKS_ROOT, PROJECTS_ROOT).recover_operations()
    WORKFLOW.migrate_projects()
    WORKFLOW.recover_operations()
    assign_default_models(TASKS_ROOT)
    sync_record_links(TASKS_ROOT, "task.json", "title")
    sync_record_links(PROJECTS_ROOT, "project.json", "name")
    print("Develop Agent: http://127.0.0.1:8765", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
