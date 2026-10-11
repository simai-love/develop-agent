"""Store each task as readable files below the ignored tasks directory."""

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


TASK_ID = re.compile(r"^[a-f0-9]{12}$")
MODEL_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,99}$")
DEFAULT_CODEX_MODEL = "gpt-6-sol"


def now() -> str:
    """Return the current UTC timestamp as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def task_dir(root: Path, task_id: str) -> Path:
    """Return the directory for a validated task ID under root."""
    if not TASK_ID.fullmatch(task_id):
        raise ValueError("Invalid task ID")
    return root / task_id


def write_text(path: Path, content: str) -> None:
    """Atomically write content to path and return nothing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as file:
        file.write(content)
        temporary_path = Path(file.name)
    os.replace(temporary_path, path)


def read_task(root: Path, task_id: str) -> dict:
    """Read a task ID from root and return its metadata and Markdown content."""
    directory = task_dir(root, task_id)
    task = json.loads((directory / "task.json").read_text(encoding="utf-8"))
    task.setdefault("implementation", {"state": "not_started", "changed_files": []})
    task.setdefault("readiness", {"ready": False, "plan_hash": None, "reason": "Agentに計画を相談してください"})
    task.setdefault("model", "")
    task.setdefault("provider", "codex")
    task.setdefault("project_id", None)
    task.setdefault("branch", None)
    task.setdefault("commit_hash", None)
    task.setdefault("pre_split_commit", None)
    task.setdefault("merge_hash", None)
    task.setdefault("legacy_completed", False)
    task.setdefault("child_proposals", [])
    task["plan"] = (directory / "plan.md").read_text(encoding="utf-8")
    reports = task.setdefault("reports", [])
    legacy_result = (directory / "result.md").read_text(encoding="utf-8")
    if not reports and legacy_result.strip():
        reports.append({"number": 1, "file": "result.md", "created_at": task["updated_at"], "outcome": task["implementation"].get("validation_outcome", "inconclusive"), "changed_files": task["implementation"].get("changed_files", []), "evidence_files": []})
    for report in reports:
        report["content"] = (directory / report["file"]).read_text(encoding="utf-8")
    task["result"] = reports[-1]["content"] if reports else ""
    task["can_implement"] = bool(task["readiness"]["ready"] and task["readiness"]["plan_hash"] == plan_hash(task["plan"]) and task["plan"].strip())
    if task["status"] == "ready" and not task["can_implement"]:
        task["status"] = "planning"
    return task


def plan_hash(plan: str) -> str:
    """Hash plan Markdown input and return its stable digest."""
    return hashlib.sha256(plan.encode("utf-8")).hexdigest()


def save_metadata(root: Path, task: dict) -> None:
    """Persist task metadata input without derived Markdown fields; return nothing."""
    metadata = {key: value for key, value in task.items() if key not in ("plan", "result", "can_implement")}
    metadata["reports"] = [{key: value for key, value in report.items() if key != "content"} for report in task["reports"]]
    write_text(task_dir(root, task["id"]) / "task.json", json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")


def list_tasks(root: Path) -> list[dict]:
    """Return task metadata from root, ordered by creation time."""
    if not root.exists():
        return []
    tasks = []
    for directory in root.iterdir():
        if directory.is_dir() and TASK_ID.fullmatch(directory.name) and (directory / "task.json").exists():
            task = read_task(root, directory.name)
            tasks.append({key: value for key, value in task.items() if key not in ("plan", "result", "can_implement", "reports")})
    return sorted(tasks, key=lambda task: task["created_at"])


def assign_default_models(root: Path) -> None:
    """Set the default on unfinished Codex tasks without a model; return nothing."""
    for task in list_tasks(root):
        if task["provider"] == "codex" and task["status"] != "completed" and not task["model"]:
            update_task(root, task["id"], {"model": DEFAULT_CODEX_MODEL})


def task_subtree(root: Path, task_id: str) -> list[str]:
    """Return task_id and all descendant IDs from root in child-first order."""
    read_task(root, task_id)
    children: dict[str, list[str]] = {}
    for task in list_tasks(root):
        if task["parent_id"]:
            children.setdefault(task["parent_id"], []).append(task["id"])

    ordered = []

    def visit(current_id: str) -> None:
        """Append descendants of current_id before the current ID; return nothing."""
        for child_id in children.get(current_id, []):
            visit(child_id)
        ordered.append(current_id)

    visit(task_id)
    return ordered


def delete_task(root: Path, task_id: str) -> list[str]:
    """Delete task_id and descendants from root; return the deleted IDs."""
    task_ids = task_subtree(root, task_id)
    for current_id in task_ids:
        shutil.rmtree(task_dir(root, current_id))
    return task_ids


def create_task(root: Path, title: str, parent_id: str | None = None, project_id: str | None = None, initial_plan: str = "", task_id: str | None = None) -> dict:
    """Create a task from title and hierarchy IDs, returning its full record."""
    title = title.strip()
    if not title:
        raise ValueError("Title is required")
    if parent_id is not None:
        read_task(root, parent_id)
    task_id = task_id or uuid4().hex[:12]
    directory = task_dir(root, task_id)
    directory.mkdir(parents=True)
    timestamp = now()
    task = {
        "id": task_id,
        "parent_id": parent_id,
        "project_id": project_id,
        "branch": None,
        "commit_hash": None,
        "pre_split_commit": None,
        "merge_hash": None,
        "legacy_completed": False,
        "child_proposals": [],
        "title": title,
        "status": "planning",
        "created_at": timestamp,
        "updated_at": timestamp,
        "completed_at": None,
        "implementation": {"state": "not_started", "changed_files": []},
        "readiness": {"ready": False, "plan_hash": None, "reason": "Agentに計画を相談してください"},
        "model": DEFAULT_CODEX_MODEL,
        "provider": "codex",
        "reports": [],
    }
    write_text(directory / "task.json", json.dumps(task, ensure_ascii=False, indent=2) + "\n")
    write_text(directory / "plan.md", initial_plan)
    write_text(directory / "result.md", "")
    (directory / "reports").mkdir()
    (directory / "evidence").mkdir()
    (directory / "validation").mkdir()
    return read_task(root, task_id)


def update_task(root: Path, task_id: str, changes: dict) -> dict:
    """Apply editable fields in changes to task ID and return the updated task."""
    task = read_task(root, task_id)
    directory = task_dir(root, task_id)
    if set(changes) - {"title", "plan", "model", "provider"}:
        raise ValueError("Only title, plan, model, and provider can be edited")
    if task["implementation"]["state"] == "running" and "plan" in changes:
        raise ValueError("Plan cannot change during implementation")
    if "title" in changes:
        title = changes["title"].strip()
        if not title:
            raise ValueError("Title is required")
        task["title"] = title
    if "model" in changes:
        model = changes["model"]
        if not isinstance(model, str) or (model and not MODEL_NAME.fullmatch(model)):
            raise ValueError("Invalid model name")
        if model != task["model"]:
            task["model"] = model
            task["readiness"] = {"ready": False, "plan_hash": None, "reason": "モデルを変更しました。Agentに計画を再確認してください"}
            if task["implementation"]["state"] != "succeeded":
                task["status"] = "planning"
    if "provider" in changes:
        provider = changes["provider"]
        if provider not in {"codex", "claude"}:
            raise ValueError("Invalid Agent provider")
        if provider != task["provider"]:
            task["provider"] = provider
            task["model"] = DEFAULT_CODEX_MODEL if provider == "codex" else ""
            task["readiness"] = {"ready": False, "plan_hash": None, "reason": "Agentを変更しました。計画を再確認してください"}
            if task["implementation"]["state"] != "succeeded":
                task["status"] = "planning"
    if "plan" in changes:
        write_text(directory / "plan.md", changes["plan"])
    if "plan" in changes and changes["plan"] != task["plan"]:
        task["implementation"] = {"state": "not_started", "changed_files": []}
        task["status"] = "planning"
        task["completed_at"] = None
        if task["readiness"]["plan_hash"] != plan_hash(changes["plan"]):
            task["readiness"] = {"ready": False, "plan_hash": None, "reason": "計画を変更しました。Agentに再確認してください"}
        elif task["readiness"]["ready"]:
            task["status"] = "ready"
    task["updated_at"] = now()
    save_metadata(root, task)
    return read_task(root, task_id)


def set_readiness(root: Path, task_id: str, ready: bool, plan: str, reason: str, previous_plan: str | None = None) -> dict:
    """Store readiness for task ID and prior plan input; return its record."""
    task = read_task(root, task_id)
    if previous_plan is not None and task["plan"] != previous_plan:
        task["implementation"] = {"state": "not_started", "changed_files": []}
        task["completed_at"] = None
    task["readiness"] = {"ready": ready, "plan_hash": plan_hash(plan) if ready else None, "reason": reason}
    if task["implementation"]["state"] not in {"running", "succeeded"} and task["status"] != "completed":
        task["status"] = "ready" if ready and plan_hash(task["plan"]) == plan_hash(plan) and task["plan"].strip() else "planning"
    task["updated_at"] = now()
    save_metadata(root, task)
    return read_task(root, task_id)


def complete_task(root: Path, task_id: str, result: str) -> dict:
    """Complete task ID after verified implementation and result; return its record."""
    task = read_task(root, task_id)
    if task["implementation"]["state"] != "succeeded":
        raise ValueError("Implementation must finish before completing a task")
    if not isinstance(result, str) or not result.strip() or result != task["result"]:
        raise ValueError("The latest saved report is required to complete a task")
    task["status"] = "completed"
    task["completed_at"] = now()
    task["updated_at"] = now()
    save_metadata(root, task)
    return read_task(root, task_id)


def mark_task_completed(root: Path, task_id: str, git_hash: str | None, kind: str) -> dict:
    """Mark task_id complete after Git or legacy review and return its record."""
    if kind not in {"commit", "merge", "legacy"}:
        raise ValueError("Invalid completion kind")
    task = read_task(root, task_id)
    task["status"] = "completed"
    task["completed_at"] = now()
    task["updated_at"] = now()
    if kind == "legacy":
        task["legacy_completed"] = True
    else:
        task["commit_hash" if kind == "commit" else "merge_hash"] = git_hash
    save_metadata(root, task)
    return read_task(root, task_id)


def save_child_proposals(root: Path, task_id: str, proposals: list[dict], current_plan_hash: str) -> dict:
    """Store proposals for task_id and return the updated task."""
    task = read_task(root, task_id)
    existing = task["child_proposals"]
    incoming = {hashlib.sha256(json.dumps(proposal, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12] for proposal in proposals}
    for prior in existing:
        if prior["status"] == "pending" and prior["id"] not in incoming:
            prior["status"] = "superseded"
    for proposal in proposals:
        identity = hashlib.sha256(json.dumps(proposal, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
        prior = next((item for item in existing if item["id"] == identity), None)
        if prior:
            if prior["status"] == "pending":
                prior["plan_hash"] = current_plan_hash
            continue
        existing.append({"id": identity, **proposal, "plan_hash": current_plan_hash, "status": "pending", "child_id": None})
    task["updated_at"] = now()
    save_metadata(root, task)
    return read_task(root, task_id)


def update_child_proposal(root: Path, task_id: str, proposal_id: str, status: str, child_id: str | None = None) -> dict:
    """Change a proposal's status and return the updated task."""
    task = read_task(root, task_id)
    proposal = next((item for item in task["child_proposals"] if item["id"] == proposal_id), None)
    if proposal is None:
        raise ValueError("提案が見つかりません")
    proposal["status"] = status
    proposal["child_id"] = child_id
    task["updated_at"] = now()
    save_metadata(root, task)
    return read_task(root, task_id)


def report_draft_path(root: Path, task_id: str) -> Path:
    """Return the temporary report path for task ID under root."""
    return task_dir(root, task_id) / "report-draft.md"


def append_report(root: Path, task_id: str, outcome: str, changed_files: list[str], evidence_files: list[str], provider: str, model: str, needs_followup: bool, report_kind: str = "validation", summary_basis: dict | None = None, commit_message: str = "") -> dict:
    """Save the task's draft as a new report and return the updated task."""
    task = read_task(root, task_id)
    draft = report_draft_path(root, task_id)
    content = draft.read_text(encoding="utf-8")
    if not content.strip():
        raise ValueError("Agent did not write a report")
    number = len(task["reports"]) + 1
    filename = f"reports/{number:04}.md"
    write_text(task_dir(root, task_id) / filename, content)
    draft.unlink()
    task["reports"].append({"number": number, "file": filename, "created_at": now(), "outcome": outcome, "changed_files": changed_files, "evidence_files": evidence_files, "kind": report_kind, "summary_basis": summary_basis})
    previous = task["implementation"]
    task["implementation"] = {"state": "succeeded", "changed_files": sorted(set(previous.get("changed_files", [])) | set(changed_files)), "provider": provider, "model": model, "validation_outcome": outcome, "needs_followup": needs_followup, "commit_message": commit_message or previous.get("commit_message", "")}
    task["status"] = "verifying"
    task["updated_at"] = now()
    save_metadata(root, task)
    return read_task(root, task_id)


def set_implementation(root: Path, task_id: str, state: str, changed_files: list[str] | None = None, provider: str = "", model: str = "", validation_outcome: str = "", needs_followup: bool = False) -> dict:
    """Store implementation state, changed files, CLI, and validation; return task."""
    task = read_task(root, task_id)
    if state not in {"running", "succeeded", "failed"}:
        raise ValueError("Invalid implementation state")
    previous = task["implementation"]
    task["implementation"] = {"state": state, "changed_files": previous.get("changed_files", []) if changed_files is None else changed_files, "provider": provider or previous.get("provider", task["provider"]), "model": model or previous.get("model", task["model"]), "validation_outcome": validation_outcome, "needs_followup": needs_followup}
    task["status"] = "verifying" if state == "succeeded" else "implementing" if state == "running" else "ready" if task["can_implement"] else "planning"
    task["updated_at"] = now()
    save_metadata(root, task)
    return read_task(root, task_id)


def set_validation_result(root: Path, task_id: str, outcome: str, needs_followup: bool) -> dict:
    """Store the latest validation outcome for task ID and return its record."""
    task = read_task(root, task_id)
    if task["implementation"]["state"] != "succeeded":
        raise ValueError("Implementation must finish before extra validation")
    task["implementation"]["validation_outcome"] = outcome
    task["implementation"]["needs_followup"] = needs_followup
    task["updated_at"] = now()
    save_metadata(root, task)
    return read_task(root, task_id)


def read_chat(root: Path, task_id: str, phase: str) -> list[dict]:
    """Read the chat for a task ID and phase, returning its message list."""
    if phase not in {"plan", "result"}:
        raise ValueError("Invalid chat phase")
    path = task_dir(root, task_id) / f"chat_{phase}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def append_chat(root: Path, task_id: str, phase: str, role: str, content: str, **details: object) -> dict:
    """Append a role, content, and optional details; return the stored message."""
    if role not in {"user", "assistant", "system"}:
        raise ValueError("Invalid chat role")
    messages = read_chat(root, task_id, phase)
    message = {"role": role, "content": content, "created_at": now(), **details}
    messages.append(message)
    write_text(task_dir(root, task_id) / f"chat_{phase}.json", json.dumps(messages, ensure_ascii=False, indent=2) + "\n")
    return message
