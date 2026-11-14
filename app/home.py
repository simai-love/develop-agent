"""Collect each project's tasks, memos, assigned Agent, reports, and night runs for the home view."""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.memos import home_dir, list_memos
from app.storage import DEFAULT_CODEX_MODEL, MODEL_NAME, list_tasks, read_chat, read_task, write_text
from app.task_status import display_status


REPORT_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class HomeProject:
    """Hold one project's ID, name, repository root, task root, and task services."""

    id: str
    name: str
    root: Path
    tasks_root: Path
    workflow: object
    agent: object


def read_agent_settings(tasks_root: Path) -> dict:
    """Return the project's assigned Agent CLI and model, defaulting to Codex."""
    path = home_dir(tasks_root) / "agent.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"provider": "codex", "model": DEFAULT_CODEX_MODEL}


def save_agent_settings(tasks_root: Path, provider: str, model: str) -> dict:
    """Validate and store provider and model as the project's assigned Agent; return them."""
    if provider not in {"codex", "claude"}:
        raise ValueError("Agent CLI は codex か claude を指定してください")
    if not isinstance(model, str) or (model and not MODEL_NAME.fullmatch(model)):
        raise ValueError("モデル名が不正です")
    settings = {"provider": provider, "model": model}
    write_text(home_dir(tasks_root) / "agent.json", json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    return settings


def task_summaries(tasks_root: Path, active_tasks: set[str]) -> list[dict]:
    """Return card fields and display status for each task under tasks_root."""
    tasks = list_tasks(tasks_root)
    titles = {task["id"]: task["title"] for task in tasks}
    children: dict[str, list[dict]] = {}
    for task in tasks:
        if task["parent_id"]:
            children.setdefault(task["parent_id"], []).append(task)
    summaries = []
    for task in tasks:
        full = read_task(tasks_root, task["id"])
        active = task["id"] in active_tasks
        summaries.append({
            "id": task["id"], "title": task["title"], "parent_id": task["parent_id"], "parent_title": titles.get(task["parent_id"]),
            "status": task["status"], "created_at": task["created_at"], "updated_at": task["updated_at"], "active": active,
            "plan_approved": full["plan_approved"], "can_implement": full["can_implement"],
            "display_status": display_status(full, read_chat(tasks_root, task["id"], "plan"), active, children.get(task["id"])),
        })
    return summaries


def report_dates(tasks_root: Path) -> list[str]:
    """Return saved daily report dates for tasks_root, newest first."""
    directory = home_dir(tasks_root) / "daily-reports"
    return sorted((path.stem for path in directory.glob("*.html") if REPORT_DATE.fullmatch(path.stem)), reverse=True) if directory.is_dir() else []


def read_report(tasks_root: Path, date: str) -> str:
    """Return the daily report HTML saved for date under tasks_root."""
    if not REPORT_DATE.fullmatch(date):
        raise ValueError("日付は YYYY-MM-DD で指定してください")
    return (home_dir(tasks_root) / "daily-reports" / f"{date}.html").read_text(encoding="utf-8")


def nightly_records(tasks_root: Path, limit: int = 14) -> list[dict]:
    """Return up to limit recent night run records for tasks_root, newest first."""
    directory = home_dir(tasks_root) / "nightly"
    if not directory.is_dir():
        return []
    paths = sorted((path for path in directory.glob("*.json") if REPORT_DATE.fullmatch(path.stem)), reverse=True)[:limit]
    return [json.loads(path.read_text(encoding="utf-8")) for path in paths]


def project_home(project: HomeProject) -> dict:
    """Return the home record of project: task cards, memos, Agent, reports, and night runs."""
    with project.agent.lock:
        active_tasks = set(project.agent.active_tasks)
    return {
        "id": project.id, "name": project.name, "path": str(project.root),
        "tasks": task_summaries(project.tasks_root, active_tasks),
        "memos": list_memos(project.tasks_root),
        "agent": read_agent_settings(project.tasks_root),
        "reports": report_dates(project.tasks_root),
        "nightly": nightly_records(project.tasks_root),
    }
