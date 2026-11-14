"""Store each project's home memos in the task record root's .home directory."""

import json
import threading
from pathlib import Path
from uuid import uuid4

from app.storage import now, write_text


MEMO_LOCK = threading.RLock()


def home_dir(tasks_root: Path) -> Path:
    """Return the .home directory for a project's task record root."""
    return tasks_root / ".home"


def list_memos(tasks_root: Path) -> list[dict]:
    """Read and return the memos stored for tasks_root, oldest first."""
    path = home_dir(tasks_root) / "memos.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _save(tasks_root: Path, memos: list[dict]) -> None:
    """Write memos for tasks_root and return nothing."""
    write_text(home_dir(tasks_root) / "memos.json", json.dumps(memos, ensure_ascii=False, indent=2) + "\n")


def _clean(changes: dict) -> dict:
    """Validate editable memo fields in changes and return their normalized values."""
    if set(changes) - {"title", "body", "allow_agent"}:
        raise ValueError("メモはタイトル・本文・許可フラグだけ編集できます")
    cleaned = {}
    if "title" in changes:
        if not isinstance(changes["title"], str) or not changes["title"].strip():
            raise ValueError("メモのタイトルを入力してください")
        cleaned["title"] = changes["title"].strip()[:120]
    if "body" in changes:
        if not isinstance(changes["body"], str):
            raise ValueError("メモの本文は文字列で入力してください")
        cleaned["body"] = changes["body"]
    if "allow_agent" in changes:
        if not isinstance(changes["allow_agent"], bool):
            raise ValueError("許可フラグは真偽値で指定してください")
        cleaned["allow_agent"] = changes["allow_agent"]
    return cleaned


def create_memo(tasks_root: Path, title: str, body: str = "", allow_agent: bool = False) -> dict:
    """Add a memo with title, body, and Agent permission to tasks_root; return it."""
    fields = _clean({"title": title, "body": body, "allow_agent": allow_agent})
    timestamp = now()
    memo = {"id": uuid4().hex[:12], **fields, "task_id": None, "created_at": timestamp, "updated_at": timestamp}
    with MEMO_LOCK:
        _save(tasks_root, [*list_memos(tasks_root), memo])
    return memo


def update_memo(tasks_root: Path, memo_id: str, changes: dict) -> dict:
    """Apply title, body, or allow_agent changes to memo_id; return the updated memo."""
    fields = _clean(changes)
    with MEMO_LOCK:
        memos = list_memos(tasks_root)
        memo = next((item for item in memos if item["id"] == memo_id), None)
        if memo is None:
            raise ValueError("メモが見つかりません")
        memo.update(fields, updated_at=now())
        _save(tasks_root, memos)
        return memo


def delete_memo(tasks_root: Path, memo_id: str) -> None:
    """Remove memo_id from tasks_root and return nothing."""
    with MEMO_LOCK:
        memos = list_memos(tasks_root)
        if not any(item["id"] == memo_id for item in memos):
            raise ValueError("メモが見つかりません")
        _save(tasks_root, [item for item in memos if item["id"] != memo_id])


def link_memo_task(tasks_root: Path, memo_id: str, task_id: str) -> dict:
    """Record task_id as the task created from memo_id; return the updated memo."""
    with MEMO_LOCK:
        memos = list_memos(tasks_root)
        memo = next(item for item in memos if item["id"] == memo_id)
        memo.update(task_id=task_id, updated_at=now())
        _save(tasks_root, memos)
        return memo
