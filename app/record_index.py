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


def ensure_record_link(root: Path, record_id: str, title: str, created_at: str) -> Path:
    """Create a readable link to an ID directory and return its path."""
    index = root / "by-name"
    index.mkdir(exist_ok=True)
    existing = next((path for path in index.iterdir() if path.is_symlink() and path.name.endswith(f"_{record_id}")), None)
    if existing:
        return existing
    link = index / readable_record_name(title, record_id, created_at)
    link.symlink_to(Path("..") / record_id, target_is_directory=True)
    return link


def remove_record_link(root: Path, record_id: str) -> None:
    """Remove the readable link for record_id from root, if present; return nothing."""
    index = root / "by-name"
    if index.is_dir():
        for path in index.iterdir():
            if path.is_symlink() and path.name.endswith(f"_{record_id}"):
                path.unlink()


def sync_record_links(root: Path, filename: str, title_key: str) -> None:
    """Add readable links for ID records in root without changing their storage paths."""
    if not root.exists():
        return
    for directory in root.iterdir():
        record_file = directory / filename
        if directory.is_dir() and RECORD_ID.fullmatch(directory.name) and record_file.is_file():
            record = json.loads(record_file.read_text(encoding="utf-8"))
            ensure_record_link(root, record["id"], record[title_key], record["created_at"])
