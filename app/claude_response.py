"""Read Claude Code's final structured response and retain failed output."""

import json
import subprocess
from pathlib import Path


def read_claude_response(result: subprocess.CompletedProcess, diagnostic_path: Path, cli_error: str | None = None) -> dict:
    """Return a response object from CLI output, or save output and raise a useful error."""
    events = []
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    payload = next((event for event in reversed(events) if event.get("type") == "result"), None)

    if result.returncode or payload is None or payload.get("is_error"):
        fallback = "Claude Codeの結果イベントがありません" if payload is None else "Claude Codeがエラーを報告しました"
        detail = cli_error or result.stderr.strip() or fallback
        _save_diagnostic(result, diagnostic_path)
        raise RuntimeError(f"Claude Codeの実行に失敗しました: {detail[-500:]}。診断ファイル: {diagnostic_path}")

    value = payload.get("structured_output")
    if value is None:
        value = payload.get("result")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = None
    if isinstance(value, dict):
        return value

    _save_diagnostic(result, diagnostic_path)
    raise RuntimeError(
        "Claude Codeは終了しましたが、指定形式のJSON応答がありません。"
        f"診断ファイル: {diagnostic_path}。作業中の変更を確認してから再実行してください。"
    )


def _save_diagnostic(result: subprocess.CompletedProcess, path: Path) -> None:
    """Save recent stdout and stderr from result to path; return nothing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-200_000:],
        "stderr_tail": result.stderr[-20_000:],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
