"""Store task-local chat attachments of any file type."""

import re
from pathlib import Path
from uuid import uuid4

from app.storage import task_dir


MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024
MAX_CHAT_FILES = 10
SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def attachment_path(root: Path, task_id: str, name: str) -> Path:
    """Validate a task ID and attachment filename, then return its path."""
    if not SAFE_NAME.fullmatch(name):
        raise ValueError("Invalid attachment filename")
    return task_dir(root, task_id) / "attachments" / name


def save_attachment(root: Path, task_id: str, original_name: str, content: bytes) -> dict:
    """Save uploaded bytes under a safe unique name; return its name, original name, and size."""
    if not content or len(content) > MAX_ATTACHMENT_BYTES:
        raise ValueError("添付ファイルは1バイト以上100 MB以下にしてください")
    original = Path(original_name or "file").name
    suffix = re.sub(r"[^a-zA-Z0-9.]", "", Path(original).suffix)[:20].lower()
    stem = re.sub(r"[^a-zA-Z0-9_-]", "-", Path(original).stem)[:60].strip("-") or "file"
    name = f"{uuid4().hex[:8]}-{stem}{suffix}"
    path = attachment_path(root, task_id, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {"name": name, "original_name": original, "size": len(content)}


def validate_chat_files(root: Path, task_id: str, names: object) -> list[str]:
    """Validate attachment names saved for one task chat; return them in order."""
    if not isinstance(names, list) or len(names) > MAX_CHAT_FILES:
        raise ValueError(f"ファイルは{MAX_CHAT_FILES}件まで添付できます")
    if any(not isinstance(name, str) for name in names) or len(set(names)) != len(names):
        raise ValueError("添付ファイルの指定が正しくありません")
    if any(not attachment_path(root, task_id, name).is_file() for name in names):
        raise ValueError("添付ファイルが見つかりません")
    return names
