"""Describe the stage and next action for a task in the local GUI."""


def status(key: str, label: str, detail: str) -> dict:
    """Return one display status record from its key, label, and detail."""
    return {"key": key, "label": label, "detail": detail}


def display_status(task: dict, plan_messages: list[dict], active: bool, children: list[dict] | None = None) -> dict:
    """Return the stage key, label, and next step for task, plan chat, run, and child inputs."""
    if task["status"] == "completed":
        return status("completed", "完了", "タスクは完了しています")
    if active or task["implementation"]["state"] == "running":
        return status("running", "実行中", "Agentが作業しています")
    if task.get("last_run_cancelled"):
        return status("review", "確認待ち", "中断しました。変更を確認して再実行")
    if task.get("commit_pending"):
        return status("review", "確認待ち", "残りの変更を確認してコミット")
    if children:
        if any(child["status"] != "completed" for child in children):
            return status("verifying", "検証中", "子タスクの完了を待機")
        if task.get("reports") and task["reports"][-1].get("kind") == "summary":
            return status("review", "確認待ち", "総括を確認してマージ")
        return status("review", "確認待ち", "子タスクの結果を総括")
    if task["status"] == "verifying":
        return status("verifying", "検証中", "検証結果を確認")
    if task["status"] == "ready":
        if task.get("plan_approved"):
            if task.get("auto_run_plan_hash") == task["plan_approval"]["plan_hash"]:
                return status("approved", "承認済み", "夜間に自動実行済み。必要なら実装を再開")
            return status("approved", "承認済み", "夜間（0:00）に実装します")
        return status("awaiting-approval", "承認待ち", "計画を承認、または実装を開始")
    if plan_messages and plan_messages[-1]["role"] == "assistant" and plan_messages[-1].get("questions"):
        return status("questions", "質問あり", "質問に回答")
    if plan_messages or task["plan"].strip():
        return status("planning", "計画中", "計画を相談")
    return status("not-started", "未着手", "計画を開始")
