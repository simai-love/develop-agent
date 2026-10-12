"""Stream Agent CLI events and extract visible tool activity."""

import json
import subprocess
import tempfile
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path


def tool_activity(event: dict, provider: str) -> list[dict]:
    """Convert one CLI event and provider into visible activity updates."""
    if provider == "codex":
        item = event.get("item", {})
        kind = item.get("type")
        event_type = event.get("type")
        if kind == "command_execution" and event_type in {"item.started", "item.completed"}:
            state = "running" if event_type == "item.started" else "failed" if item.get("exit_code") not in (None, 0) else "done"
            return [{"id": item.get("id"), "label": item.get("command", "コマンドを実行中"), "state": state}]
        if kind == "file_change" and event_type == "item.completed":
            return [{"id": item.get("id"), "label": "ファイルを変更しました", "state": "done"}]
    elif provider == "claude":
        if event.get("type") == "assistant":
            return [
                {"id": block.get("id"), "label": _claude_tool_label(block), "state": "running"}
                for block in event.get("message", {}).get("content", [])
                if block.get("type") == "tool_use"
            ]
        if event.get("type") == "user":
            return [
                {"id": block.get("tool_use_id"), "label": "", "state": "failed" if block.get("is_error") else "done"}
                for block in event.get("message", {}).get("content", [])
                if block.get("type") == "tool_result"
            ]
    return []


def _claude_tool_label(block: dict) -> str:
    """Return a short display label for one Claude tool call block."""
    name = block.get("name", "Tool")
    values = block.get("input", {})
    detail = values.get("command") if name == "Bash" else values.get("file_path") or values.get("path") or values.get("pattern")
    return str(detail) if name == "Bash" and detail else f"{name}: {detail}" if detail else name


def run_streamed(command: list[str], prompt: str, cwd: str | Path, provider: str, on_activity: Callable[[dict], None], timeout: int = 1800) -> subprocess.CompletedProcess:
    """Run a CLI with prompt in cwd, report tool events, and return its result."""
    output = deque(maxlen=200)
    expired = threading.Event()
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr_file:
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr_file, text=True, cwd=cwd, bufsize=1) as process:
            def stop_process() -> None:
                """Kill the timed-out process and record that timeout; return nothing."""
                if process.poll() is None:
                    expired.set()
                    process.kill()

            timer = threading.Timer(timeout, stop_process)
            timer.start()
            try:
                process.stdin.write(prompt)
                process.stdin.close()
                for line in process.stdout:
                    output.append(line[-200_000:])
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    for activity in tool_activity(event, provider):
                        on_activity(activity)
                process.wait()
            finally:
                timer.cancel()
        stderr_file.seek(0)
        stderr = stderr_file.read()
    if expired.is_set():
        raise subprocess.TimeoutExpired(command, timeout)
    return subprocess.CompletedProcess(command, process.returncode, "".join(output), stderr)
