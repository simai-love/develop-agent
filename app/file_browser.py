"""Resolve, list, and read repository files for the read-only file browser."""

import mimetypes
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from app.storage import read_task


MAX_TEXT_BYTES = 1024 * 1024
MAX_ENTRIES = 2000
EXCLUDED_NAMES = {".git"}
UNSAFE_RAW_TYPES = {"text/html", "application/xhtml+xml"}
TEXT_TYPES = {"application/json", "application/javascript", "application/xml", "application/x-sh", "application/toml", "application/x-yaml"}


def browse_root(workflow, tasks_root: Path, task_id: str | None) -> dict:
    """Return the root path and its kind for a task worktree or the repository."""
    repository = Path(workflow.repo).resolve()
    if task_id:
        branch = read_task(tasks_root, task_id)["branch"]
        if branch:
            worktree = workflow.git.path(branch)
            if worktree.is_dir():
                return {"path": worktree.resolve(), "kind": "worktree", "branch": branch}
    return {"path": repository, "kind": "repository", "branch": None}


def resolve_path(root: Path, relative: str) -> Path:
    """Validate a root-relative POSIX path and return its resolved location inside root."""
    root = root.resolve()
    parts = PurePosixPath(relative or ".").parts
    if relative.startswith("/") or ".." in parts:
        raise ValueError("プロジェクト内の相対パスを指定してください")
    if EXCLUDED_NAMES & set(parts):
        raise ValueError(".git は参照できません")
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or EXCLUDED_NAMES & set(target.relative_to(root).parts):
        raise ValueError("プロジェクトの外は参照できません")
    if not target.exists():
        raise FileNotFoundError(relative)
    return target


def relative_name(root: Path, path: Path) -> str:
    """Return path relative to root as a POSIX string, or an empty string for root."""
    value = path.relative_to(root.resolve()).as_posix()
    return "" if value == "." else value


def modified_time(path: Path) -> str:
    """Return the file's modification time as an ISO 8601 UTC string."""
    return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")


def list_directory(root: Path, relative: str = "") -> dict:
    """Return sorted directory entries, directories first, for a root-relative path."""
    directory = resolve_path(root, relative)
    if not directory.is_dir():
        raise NotADirectoryError("ディレクトリではありません")
    entries = []
    for item in directory.iterdir():
        if item.name in EXCLUDED_NAMES:
            continue
        resolved = item.resolve()
        if not resolved.exists() or not resolved.is_relative_to(root.resolve()) or EXCLUDED_NAMES & set(resolved.relative_to(root.resolve()).parts):
            continue
        is_dir = resolved.is_dir()
        entries.append({"name": item.name, "path": f"{relative_name(root, directory)}/{item.name}".lstrip("/"), "type": "dir" if is_dir else "file", "size": None if is_dir else resolved.stat().st_size, "modified": modified_time(resolved), "symlink": item.is_symlink()})
    entries.sort(key=lambda entry: (entry["type"] != "dir", entry["name"].lower(), entry["name"]))
    return {"path": relative_name(root, directory), "entries": entries[:MAX_ENTRIES], "truncated": len(entries) > MAX_ENTRIES}


def guess_type(path: Path) -> str:
    """Return the guessed media type for path, defaulting to octet-stream."""
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def decode_text(data: bytes, truncated: bool) -> str | None:
    """Decode UTF-8 bytes, tolerating a cut final character; return None for binary."""
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        if truncated and error.start >= len(data) - 3:
            return data[:error.start].decode("utf-8", errors="replace")
        return None


def read_content(root: Path, relative: str, limit: int = MAX_TEXT_BYTES) -> dict:
    """Return file metadata and text (up to limit bytes) or an image/binary kind."""
    path = resolve_path(root, relative)
    if path.is_dir():
        raise IsADirectoryError("ファイルではありません")
    size = path.stat().st_size
    media_type = guess_type(path)
    result = {"path": relative_name(root, path), "name": path.name, "size": size, "modified": modified_time(path), "mime": media_type, "truncated": False, "text": None}
    if media_type.startswith("image/"):
        return {**result, "kind": "image"}
    with path.open("rb") as source:
        data = source.read(limit + 1)
    truncated = len(data) > limit
    text = decode_text(data[:limit], truncated)
    if text is None:
        return {**result, "kind": "binary"}
    return {**result, "kind": "text", "text": text, "truncated": truncated}


def raw_headers(path: Path, download: bool) -> dict[str, str]:
    """Return safe response headers that keep served files from running as pages."""
    media_type = guess_type(path)
    if media_type in UNSAFE_RAW_TYPES:
        media_type = "text/plain"
    if media_type.startswith("text/") or media_type in TEXT_TYPES:
        media_type = f"{media_type}; charset=utf-8"
    headers = {"Content-Type": media_type, "Content-Length": str(path.stat().st_size), "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'"}
    if download:
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(path.name, safe='')}"
    return headers

