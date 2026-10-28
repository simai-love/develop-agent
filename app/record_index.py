"""Provide readable links to task and legacy project record directories."""

import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


RECORD_ID = re.compile(r"^[a-f0-9]{12}$")
JAPAN_TIME = ZoneInfo("Asia/Tokyo")


def readable_record_name(title: str, record_id: str, created_at: str) -> str:
    """Return a stable date, time, title, and ID directory name from record fields."""
    created = datetime.fromisoformat(created_at).astimezone(JAPAN_TIME)
    label = re.sub(r"[^\w-]+", "-", unicodedata.normalize("NFKC", title)).strip("-_ ")[:40].rstrip("-_ ") or "record"
    return f"{created:%Y-%m-%d_%H%M}_{label}_{record_id}"


def find_record_directory(root: Path, record_id: str, filename: str) -> Path:
    """Return an existing dated or legacy ID directory for a record ID."""
    legacy = root / record_id
    if (legacy / filename).is_file():
        return legacy
    if root.is_dir():
        for directory in root.iterdir():
            if (directory.is_dir() and not directory.is_symlink()
                    and directory.name.endswith(f"_{record_id}")
                    and (directory / filename).is_file()):
                return directory
    return legacy


def ensure_record_link(root: Path, record_id: str, title: str, created_at: str, directory_name: str | None = None) -> Path:
    """Create a readable link to a record directory and return its path."""
    index = root / "by-name"
    index.mkdir(exist_ok=True)
    existing = next((path for path in index.iterdir()
                     if path.is_symlink() and path.name.endswith(f"_{record_id}")), None)
    if existing:
        return existing
    link = index / readable_record_name(title, record_id, created_at)
    link.symlink_to(Path("..") / (directory_name or record_id), target_is_directory=True)
    return link


def remove_record_link(root: Path, record_id: str) -> None:
    """Remove the readable link for record_id from root, if present; return nothing."""
    index = root / "by-name"
    if index.is_dir():
        for path in index.iterdir():
            if path.is_symlink() and path.name.endswith(f"_{record_id}"):
                path.unlink()


def sync_record_links(root: Path, filename: str, title_key: str) -> None:
    """Add readable links for dated and legacy records in root."""
    if not root.exists():
        return
    for directory in root.iterdir():
        record_file = directory / filename
        if directory.is_dir() and not directory.is_symlink() and record_file.is_file():
            record = json.loads(record_file.read_text(encoding="utf-8"))
            record_id = record["id"]
            if RECORD_ID.fullmatch(record_id) and (directory.name == record_id or directory.name.endswith(f"_{record_id}")):
                ensure_record_link(root, record_id, record[title_key], record["created_at"], directory.name)
