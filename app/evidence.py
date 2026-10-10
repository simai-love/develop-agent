"""Store and enumerate task-local images, videos, and data files."""

import mimetypes
import re
from pathlib import Path
from uuid import uuid4

from app.storage import task_dir


MAX_EVIDENCE_BYTES = 100 * 1024 * 1024
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm", ".mov", ".csv", ".json", ".pdf"}
SAFE_NAME = re.compile(r"^[a-zA-Z0-9._-]+$")


def evidence_path(root: Path, task_id: str, name: str) -> Path:
    """Validate a task ID and evidence filename, then return its path."""
    if not SAFE_NAME.fullmatch(name) or Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError("Invalid evidence filename")
    return task_dir(root, task_id) / "evidence" / name


def list_evidence(root: Path, task_id: str) -> list[dict]:
    """Return image, video, and data metadata for a task ID."""
    directory = task_dir(root, task_id) / "evidence"
    if not directory.exists():
        return []
    entries = []
    for path in sorted(directory.iterdir(), key=lambda item: item.stat().st_mtime):
        if path.is_file() and SAFE_NAME.fullmatch(path.name) and path.suffix.lower() in ALLOWED_EXTENSIONS:
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            entries.append({"name": path.name, "kind": "image" if content_type.startswith("image/") else "video" if content_type.startswith("video/") else "file", "size": path.stat().st_size})
    return entries


def save_evidence(root: Path, task_id: str, original_name: str, content: bytes) -> dict:
    """Save uploaded evidence bytes for a task and return its metadata."""
    if not isinstance(content, bytes) or not content or len(content) > MAX_EVIDENCE_BYTES:
        raise ValueError("Evidence must be between 1 byte and 100 MB")
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError("Unsupported evidence file type")
    safe_stem = re.sub(r"[^a-zA-Z0-9_-]", "-", Path(original_name).stem)[:60].strip("-") or "evidence"
    name = f"{uuid4().hex[:8]}-{safe_stem}{suffix}"
    path = evidence_path(root, task_id, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return next(entry for entry in list_evidence(root, task_id) if entry["name"] == name)
