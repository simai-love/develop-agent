"""Ask a project's assigned Agent to write the previous day's HTML daily report."""

import json
import re
import tempfile
from datetime import date, datetime, time as day_time, timedelta
from pathlib import Path

from app.agent import cli_error, claude_command
from app.agent_stream import run_streamed
from app.claude_response import read_claude_response
from app.home import HomeProject, read_agent_settings, task_summaries
from app.memos import home_dir, list_memos
from app.nightly import record_path
from app.record_index import JAPAN_TIME
from app.storage import list_tasks, task_dir


DOCUMENT_SKILL = Path(__file__).resolve().parent.parent / ".codex" / "skills" / "document-writing"
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "html": {"type": "string"}},
    "required": ["summary", "html"],
    "additionalProperties": False,
}
SCRIPT_TAG = re.compile(r"<script\b[^>]*>.*?</script\s*>|<script\b[^>]*/?>", re.IGNORECASE | re.DOTALL)


def report_day(slot: datetime) -> date:
    """Return the Japan-time day before the morning slot, which the report covers."""
    return slot.astimezone(JAPAN_TIME).date() - timedelta(days=1)


def changed_on(tasks: list[dict], day: date) -> list[dict]:
    """Return tasks whose updated_at falls within day from 0:00 to 24:00 Japan time."""
    start = datetime.combine(day, day_time(), JAPAN_TIME)
    end = start + timedelta(days=1)
    return [task for task in tasks if start <= datetime.fromisoformat(task["updated_at"]) < end]


def report_path(tasks_root: Path, day: date) -> Path:
    """Return the daily report HTML path for day under tasks_root."""
    return home_dir(tasks_root) / "daily-reports" / f"{day:%Y-%m-%d}.html"


def report_prompt(project: HomeProject, day: date, changed: list[dict], night: dict | None, output: Path) -> str:
    """Return the Agent prompt for project's report on day from changed tasks and the night record."""
    lines = []
    for task in changed:
        directory = task_dir(project.tasks_root, task["id"])
        lines.append(
            f"- {task['title']}（ID {task['id']}、状態: {task['display_status']['label']} / {task['display_status']['detail']}、更新 {task['updated_at']}）"
            f" 記録: {directory}（計画は plan.html、無ければ plan.md。レポートは reports/、会話は chat_plan.json と chat_result.json）"
        )
    memos = [f"- {memo['title']}{'（タスク化済み）' if memo['task_id'] else ''}" for memo in list_memos(project.tasks_root)]
    return (
        "日本語で作業してください。研究開発プロジェクトの日報を 1 つの HTML 文書として作成します。\n"
        f"最初に {DOCUMENT_SKILL / 'SKILL.md'} を読み、読者（このプロジェクトの担当者）が朝に要点をつかめる構成を設計してから書いてください。"
        "結論と要対応事項を上部に置き、各タスクの進捗・結果・次の行動を短く示してください。"
        "文字だけの文書を避け、進捗の流れ図や状態の比較をインライン SVG や表で示してください。\n"
        "HTML は <!doctype html> から始まる自己完結した文書にし、<script>、外部の CSS・画像・フォントは使わないでください。"
        "スタイルは <style> に最小限で書き、白い背景で読みやすい配色にしてください。\n"
        "ファイルやコード、Git は変更せず、必要な記録だけを読んでください。"
        "完成した HTML 全体を JSON の html に、1〜2 文の要約を summary に入れて返してください。"
        f"GUI がそれを {output} に保存します。\n"
        f"プロジェクト: {project.name}（{project.root}）\n対象日: {day:%Y-%m-%d}（日本時間 0:00–24:00）\n"
        f"対象日に更新されたタスク:\n{chr(10).join(lines) or '- なし'}\n"
        f"対象日の夜（翌日 0:00）の自動実行記録:\n{json.dumps(night, ensure_ascii=False, indent=2) if night else 'なし'}\n"
        f"メモ一覧:\n{chr(10).join(memos) or '- なし'}\n"
    )


def generate_html(project: HomeProject, prompt: str, day: date) -> str:
    """Run the project's assigned Agent CLI read-only with prompt and return its HTML answer."""
    settings = read_agent_settings(project.tasks_root)
    provider, model = settings["provider"], settings["model"]
    cli = project.agent.cli_paths[provider]
    if not cli:
        raise ValueError(f"{provider} CLI is not installed")
    workdir = home_dir(project.tasks_root)
    workdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        schema_path = Path(temporary) / "daily-report.schema.json"
        schema_path.write_text(json.dumps(RESPONSE_SCHEMA), encoding="utf-8")
        output_path = Path(temporary) / "answer.json"
        if provider == "codex":
            command = [cli, "exec", "--json", "--output-last-message", str(output_path), "--cd", str(workdir), "--output-schema", str(schema_path), "--sandbox", "read-only"]
            command += ["--model", model] if model else []
            command += ["-"]
        else:
            command = claude_command(cli, model, schema_path, [project.root, project.tasks_root, DOCUMENT_SKILL], project.agent.codex_sandbox)
            command += ["--disallowedTools", "Edit", "Write", "NotebookEdit"]
        result = run_streamed(command, prompt, workdir, provider, lambda activity: None, None, project.agent.process_env)
        if provider == "codex":
            if result.returncode != 0 or not output_path.exists():
                raise RuntimeError((cli_error(result.stdout) or result.stderr or "codex CLI failed")[-1200:])
            response = json.loads(output_path.read_text(encoding="utf-8"))
        else:
            response = read_claude_response(result, workdir / "diagnostics" / f"daily-report-{day:%Y-%m-%d}.json", cli_error(result.stdout))
    if not isinstance(response.get("html"), str) or not response["html"].strip():
        raise ValueError("Agent returned no daily report HTML")
    return SCRIPT_TAG.sub("", response["html"])


def run_daily_report(project: HomeProject, slot: datetime, poll: float = 5) -> Path | None:
    """Write the report for the day before slot when tasks changed or a night record exists; return its path."""
    day = report_day(slot)
    night_path = record_path(project.tasks_root, slot)
    changed_ids = {task["id"] for task in changed_on(list_tasks(project.tasks_root), day)}
    if not changed_ids and not night_path.exists():
        return None
    with project.agent.lock:
        active = set(project.agent.active_tasks)
    changed = [task for task in task_summaries(project.tasks_root, active) if task["id"] in changed_ids]
    night = json.loads(night_path.read_text(encoding="utf-8")) if night_path.exists() else None
    output = report_path(project.tasks_root, day)
    html = generate_html(project, report_prompt(project, day, changed, night, output), day)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    return output
