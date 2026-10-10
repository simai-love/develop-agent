"""Describe the next action for a task in the local GUI."""


def display_status(task: dict, plan_messages: list[dict], active: bool) -> dict:
    """Return a display key, label, and next step for task and its run state."""
    if task["status"] == "completed":
        return {"key": "completed", "label": "完了済み", "detail": "タスクは完了しています"}
    if active or task["implementation"]["state"] == "running":
        return {"key": "running", "label": "実行中", "detail": "Agentが作業しています"}
    if task["status"] == "verifying":
        return {"key": "waiting", "label": "ユーザー待ち", "detail": "検証結果を確認"}
    if task["status"] == "ready":
        return {"key": "waiting", "label": "ユーザー待ち", "detail": "実装を開始"}
    if plan_messages and plan_messages[-1]["role"] == "assistant" and plan_messages[-1].get("questions"):
        return {"key": "waiting", "label": "ユーザー待ち", "detail": "質問に回答"}
    if plan_messages or task["plan"].strip():
        return {"key": "waiting", "label": "ユーザー待ち", "detail": "計画を相談"}
    return {"key": "not-started", "label": "未着手", "detail": "計画を開始"}
