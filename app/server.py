"""Serve the local GUI and its file-backed task API."""

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.agent import AgentManager
from app.evidence import MAX_EVIDENCE_BYTES, evidence_path, list_evidence, save_evidence
from app.storage import assign_default_models, complete_task, create_task, list_tasks, read_chat, read_task, update_task
from app.task_status import display_status


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_ROOT = Path(__file__).resolve().parent / "static"
TASKS_ROOT = PROJECT_ROOT / "tasks"
AGENT = AgentManager(TASKS_ROOT, PROJECT_ROOT)


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
        full_task = task if "plan" in task else read_task(TASKS_ROOT, task["id"])
        return {**task, "active": active, "display_status": display_status(full_task, read_chat(TASKS_ROOT, task["id"], "plan"), active)}

    def do_GET(self) -> None:
        """Return a task list, one task, or a static GUI asset."""
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/agent":
            self.send_json(200, AGENT.availability())
            return
        if path.startswith("/api/runs/"):
            run = AGENT.get_run(path.removeprefix("/api/runs/"))
            self.send_json(200 if run else 404, run or {"error": "Run not found"})
            return
        if path == "/api/tasks":
            self.send_json(200, [self.present_task(task) for task in list_tasks(TASKS_ROOT)])
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
                self.send_json(201, self.present_task(create_task(TASKS_ROOT, body["title"], body.get("parent_id"))))
            elif path.endswith("/chat"):
                task_id = path.removeprefix("/api/tasks/").removesuffix("/chat")
                self.send_json(202, AGENT.start_chat(task_id, body["phase"], body["message"]))
            elif path.endswith("/complete"):
                task_id = path.removeprefix("/api/tasks/").removesuffix("/complete")
                if task_id in AGENT.active_tasks:
                    raise ValueError("Cannot complete a task during an Agent run")
                self.send_json(200, self.present_task(complete_task(TASKS_ROOT, task_id, body["result"])))
            else:
                task_id = path.removeprefix("/api/tasks/").removesuffix("/execute")
                self.send_json(202, AGENT.start_implementation(task_id))
        except (KeyError, TypeError, ValueError, FileNotFoundError) as error:
            self.send_json(400, {"error": str(error)})

    def do_PATCH(self) -> None:
        """Update a task from a JSON request and return its record."""
        path = urlparse(self.path).path
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
        if not path.startswith("/api/tasks/") or "/" in path.removeprefix("/api/tasks/"):
            self.send_error(404)
            return
        try:
            deleted_ids = AGENT.remove_task(path.removeprefix("/api/tasks/"))
            self.send_json(200, {"deleted_ids": deleted_ids})
        except FileNotFoundError:
            self.send_json(404, {"error": "Task not found"})
        except ValueError as error:
            self.send_json(400, {"error": str(error)})


def main() -> None:
    """Start the local GUI server and return when it is interrupted."""
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    AGENT.recover_interrupted_runs()
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
