"""Run each project's night job at 0:00 and daily report at 6:00 inside the GUI server."""

import json
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from app.daily_report import run_daily_report
from app.home import HomeProject
from app.memos import home_dir
from app.nightly import run_nightly
from app.record_index import JAPAN_TIME
from app.storage import write_text


JOB_HOURS = {"nightly": 0, "daily_report": 6}


def latest_slot(at: datetime, hour: int) -> datetime:
    """Return the latest Japan-time hour:00 at or before the aware datetime at."""
    local = at.astimezone(JAPAN_TIME)
    slot = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    return slot if local >= slot else slot - timedelta(days=1)


def initial_state(at: datetime) -> dict:
    """Return a scheduler state that treats every slot up to at as already handled."""
    return {job: latest_slot(at, hour).isoformat() for job, hour in JOB_HOURS.items()}


def due_jobs(state: dict, at: datetime) -> list[tuple[str, datetime]]:
    """Return (job, slot) pairs whose latest slot at or before at is newer than state, night job first."""
    due = []
    for job, hour in JOB_HOURS.items():
        slot = latest_slot(at, hour)
        if job not in state or datetime.fromisoformat(state[job]) < slot:
            due.append((job, slot))
    return due


def state_path(tasks_root: Path) -> Path:
    """Return the scheduler state file for a project's task record root."""
    return home_dir(tasks_root) / "scheduler.json"


def read_state(tasks_root: Path) -> dict | None:
    """Return the saved scheduler state for tasks_root, or None before the first start."""
    path = state_path(tasks_root)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def save_state(tasks_root: Path, state: dict) -> None:
    """Write the scheduler state for tasks_root and return nothing."""
    write_text(state_path(tasks_root), json.dumps(state, ensure_ascii=False, indent=2) + "\n")


class Scheduler:
    """Check registered projects periodically and run their missed or due jobs once."""

    def __init__(self, projects: Callable[[], list[HomeProject]], clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc), interval: float = 30, poll: float = 5):
        """Store the project source, clock, check interval, and run poll interval; return an idle scheduler."""
        self.projects = projects
        self.clock = clock
        self.interval = interval
        self.poll = poll
        self.workers: dict[str, threading.Thread] = {}
        self.jobs = {"nightly": run_nightly, "daily_report": run_daily_report}

    def run_due(self, project: HomeProject) -> list[str]:
        """Run project's due jobs in order, saving each handled slot; return the job names run."""
        at = self.clock()
        state = read_state(project.tasks_root)
        if state is None:
            save_state(project.tasks_root, initial_state(at))
            return []
        ran = []
        for job, slot in due_jobs(state, at):
            try:
                self.jobs[job](project, slot, self.poll)
            except Exception:
                traceback.print_exc()
            state[job] = slot.isoformat()
            save_state(project.tasks_root, state)
            ran.append(job)
        return ran

    def check(self) -> None:
        """Start a worker for each project with due jobs and no running worker; return nothing."""
        for project in self.projects():
            worker = self.workers.get(project.id)
            if worker and worker.is_alive():
                continue
            state = read_state(project.tasks_root)
            if state is not None and not due_jobs(state, self.clock()):
                continue
            worker = threading.Thread(target=self.run_due, args=(project,), daemon=True, name=f"home-scheduler-{project.id}")
            self.workers[project.id] = worker
            worker.start()

    def start(self) -> threading.Thread:
        """Start the periodic check loop in a daemon thread and return the thread."""
        def loop() -> None:
            """Check projects every interval until the server exits; return nothing."""
            while True:
                try:
                    self.check()
                except Exception:
                    traceback.print_exc()
                time.sleep(self.interval)

        thread = threading.Thread(target=loop, daemon=True, name="home-scheduler")
        thread.start()
        return thread
