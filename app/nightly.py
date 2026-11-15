"""Select and run approved plans and Agent-permitted memos for one project at night."""

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

from app.home import HomeProject, read_agent_settings
from app.memos import home_dir, link_memo_task, list_memos
from app.plan_approval import approve_plan, record_auto_run
from app.record_index import JAPAN_TIME
from app.storage import list_tasks, now, read_chat, read_task, update_task, write_text


NIGHT_INSTRUCTION = "夜間の自動実行です。ユーザーは不在のため、判断が必要な点は作業を止めずにレポートに記録してください。"
EXPERIMENT_MESSAGE = (
    "夜間の自動実行です。承認済みの計画はすでに実装済みです。同じ計画の範囲で追加の実験・検証を行い、"
    "必要な修正と再検証をして、今回分のレポートを書いてください。計画の範囲外の変更はしないでください。"
)


def approved_targets(tasks: list[dict], active: set[str]) -> list[dict]:
    """Return full task records with a valid, not yet auto-run approval that can start now."""
    parents = {task["parent_id"] for task in tasks if task["parent_id"]}
    return [
        task for task in tasks
        if task["status"] != "completed" and task["id"] not in active and task["id"] not in parents
        and task["implementation"]["state"] != "running" and task["plan_approved"]
        and task["auto_run_plan_hash"] != task["plan_approval"]["plan_hash"]
    ]


def memo_targets(memos: list[dict]) -> list[dict]:
    """Return memos that permit Agent planning and implementation and have no task yet."""
    return [memo for memo in memos if memo["allow_agent"] and not memo["task_id"]]


def memo_message(memo: dict) -> str:
    """Return the first plan chat message built from memo's title and body."""
    return (
        "夜間の自動実行です。次のメモを元に、このタスクの計画を作成してください。"
        "計画はスキルの指示どおりの計画ファイル（plan.html）に HTML で書き、構成図・流れ図・比較表をインライン SVG や表で入れて本文は短くしてください。"
        "<script> は使わないでください。不明点が残る場合は選択式の質問で返し、実装可能と判断できる場合だけ implementation_ready を true にしてください。\n"
        f"メモのタイトル: {memo['title']}\nメモの本文:\n{memo['body']}"
    )


def wait_for_run(agent, run_id: str, poll: float) -> dict:
    """Poll agent until run_id stops running and return its final run record."""
    while (run := agent.get_run(run_id)) and run["state"] == "running":
        time.sleep(poll)
    return run or {"state": "failed", "message": "実行記録が見つかりません"}


def entry(kind: str, task: dict, run_kind: str, state: str, message: str, **details: object) -> dict:
    """Return one night record entry from the task, run kind, outcome state, and message."""
    return {"kind": kind, "task_id": task["id"], "title": task["title"], "run": run_kind, "state": state, "message": message[:600], "finished_at": now(), **details}


def run_approved_task(project: HomeProject, task: dict, poll: float) -> dict:
    """Implement or extend an approved task's plan, wait for the run, and return its record entry."""
    previous_hash = task["auto_run_plan_hash"]
    record_auto_run(project.tasks_root, task["id"], task["plan_approval"]["plan_hash"])
    experiment = task["implementation"]["state"] == "succeeded"
    run_kind = "experiment" if experiment else "implement"
    try:
        run = project.agent.start_chat(task["id"], "result", EXPERIMENT_MESSAGE) if experiment else project.agent.start_implementation(task["id"], NIGHT_INSTRUCTION)
    except ValueError as error:
        record_auto_run(project.tasks_root, task["id"], previous_hash)
        return entry("approved_task", task, run_kind, "skipped", str(error))
    run = wait_for_run(project.agent, run["id"], poll)
    return entry("approved_task", task, run_kind, run["state"], run.get("message", ""))


def run_memo(project: HomeProject, memo: dict, poll: float) -> list[dict]:
    """Create a task from memo, plan it, implement it when ready, and return its record entries."""
    root = project.tasks_root
    task = project.workflow.create_root_task(memo["title"])
    link_memo_task(root, memo["id"], task["id"])
    settings = read_agent_settings(root)
    update_task(root, task["id"], {"provider": settings["provider"]})
    update_task(root, task["id"], {"model": settings["model"]})
    run = wait_for_run(project.agent, project.agent.start_chat(task["id"], "plan", memo_message(memo))["id"], poll)
    task = read_task(root, task["id"])
    if run["state"] != "succeeded":
        return [entry("memo", task, "plan", run["state"], run.get("message", ""), memo_id=memo["id"])]
    if not task["can_implement"] or project.workflow.children(task["id"]):
        messages = read_chat(root, task["id"], "plan")
        questions = messages[-1].get("questions", []) if messages and messages[-1]["role"] == "assistant" else []
        reason = "質問が残っているため計画中で止めました" if questions else task["readiness"]["reason"]
        return [entry("memo", task, "plan", "stopped", reason, memo_id=memo["id"], questions=[item["prompt"] for item in questions])]
    task = approve_plan(root, task["id"], source="memo")
    planned = entry("memo", task, "plan", "succeeded", run.get("message", ""), memo_id=memo["id"])
    return [planned, {**run_approved_task(project, task, poll), "kind": "memo", "memo_id": memo["id"]}]


def record_path(tasks_root: Path, slot: datetime) -> Path:
    """Return the night record file for the Japan-time date of slot."""
    return home_dir(tasks_root) / "nightly" / f"{slot.astimezone(JAPAN_TIME):%Y-%m-%d}.json"


def run_nightly(project: HomeProject, slot: datetime, poll: float = 5) -> list[dict]:
    """Run project's due approved tasks and memos one by one, record them for slot, and return the entries."""
    root = project.tasks_root
    with project.agent.lock:
        active = set(project.agent.active_tasks)
    tasks = [read_task(root, task["id"]) for task in list_tasks(root)]
    targets, memos = approved_targets(tasks, active), memo_targets(list_memos(root))
    if not targets and not memos:
        return []
    started_at = now()
    entries = [run_approved_task(project, task, poll) for task in targets]
    for memo in memos:
        try:
            entries.extend(run_memo(project, memo, poll))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            entries.append({"kind": "memo", "memo_id": memo["id"], "task_id": None, "title": memo["title"], "run": "plan", "state": "failed", "message": str(error)[:600], "finished_at": now()})
    path = record_path(root, slot)
    record = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"date": path.stem, "project": project.id, "started_at": started_at, "entries": []}
    record["entries"].extend(entries)
    record["finished_at"] = now()
    write_text(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return entries
