"""Store local project records outside Git tracking."""

import json
import re
from pathlib import Path

from app.storage import now, write_text


PROJECT_ID = re.compile(r"^[a-f0-9]{12}$")


def project_dir(root: Path, project_id: str) -> Path:
    """Return a validated project directory for root and project_id."""
    if not PROJECT_ID.fullmatch(project_id):
        raise ValueError("Invalid project ID")
    return root / project_id


def read_project(root: Path, project_id: str) -> dict:
    """Read project_id from root and return its record."""
    return json.loads((project_dir(root, project_id) / "project.json").read_text(encoding="utf-8"))


def list_projects(root: Path) -> list[dict]:
    """Read all project records from root and return them by creation time."""
    if not root.exists():
        return []
    records = [read_project(root, path.name) for path in root.iterdir() if path.is_dir() and PROJECT_ID.fullmatch(path.name) and (path / "project.json").exists()]
    return sorted(records, key=lambda item: item["created_at"])


def save_project(root: Path, project: dict) -> dict:
    """Write project to root and return the same record."""
    path = project_dir(root, project["id"]) / "project.json"
    write_text(path, json.dumps(project, ensure_ascii=False, indent=2) + "\n")
    return project


def new_project(project_id: str, name: str, slug: str, branch: str, base_commit: str, first_task_id: str) -> dict:
    """Build a project record from creation values and return it."""
    timestamp = now()
    return {"id": project_id, "name": name.strip(), "slug": slug, "branch": branch, "base_commit": base_commit, "first_task_id": first_task_id, "status": "active", "merge_hash": None, "created_at": timestamp, "updated_at": timestamp}
