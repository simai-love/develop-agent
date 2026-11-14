"""Store user approval of a task plan and the plan hash run automatically at night."""

from pathlib import Path

from app.storage import now, plan_hash, read_task, save_metadata


def approve_plan(root: Path, task_id: str, approved_at: str | None = None, source: str = "user") -> dict:
    """Approve task_id's saved plan at approved_at by source ("user" or "memo"); return the task."""
    task = read_task(root, task_id)
    if task["status"] == "completed":
        raise ValueError("完了済みタスクの計画は承認できません")
    if not task["can_implement"]:
        raise ValueError("Agentが実装可能と判断した計画だけ承認できます")
    task["plan_approval"] = {"plan_hash": plan_hash(task["plan"]), "approved_at": approved_at or now(), "source": source}
    task["updated_at"] = now()
    save_metadata(root, task)
    return read_task(root, task_id)


def revoke_plan_approval(root: Path, task_id: str) -> dict:
    """Remove the plan approval from task_id under root; return the updated task."""
    task = read_task(root, task_id)
    task["plan_approval"] = None
    task["updated_at"] = now()
    save_metadata(root, task)
    return read_task(root, task_id)


def record_auto_run(root: Path, task_id: str, approved_hash: str) -> dict:
    """Record approved_hash as automatically run for task_id under root; return the task."""
    task = read_task(root, task_id)
    task["auto_run_plan_hash"] = approved_hash
    save_metadata(root, task)
    return read_task(root, task_id)
