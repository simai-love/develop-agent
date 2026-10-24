"""Read the picker-visible model catalogs for the installed Agent CLIs."""

import json
import os
import select
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache


CLAUDE_MODEL_ALIASES = [("fable", "Fable"), ("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku")]


def claude_model_name(alias: str, name: str, model_id: str) -> str:
    """Return a display name such as "Opus 5.5" from a resolved model ID like claude-opus-5-5."""
    prefix = f"claude-{alias}-"
    version = [part for part in model_id.removeprefix(prefix).split("-") if len(part) < 8] if model_id.startswith(prefix) else []
    return f"{name} {'.'.join(version)}" if version and all(part.isdigit() for part in version) else name


def resolve_claude_model(cli_path: str, alias: str) -> str:
    """Start cli_path with alias, read the model ID from its init event, and stop it; return "" on failure."""
    with tempfile.TemporaryDirectory() as directory:
        try:
            process = subprocess.Popen(
                [cli_path, "--print", "--output-format", "stream-json", "--verbose", "--no-session-persistence", "--model", alias],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=directory, start_new_session=True,
            )
        except OSError:
            return ""
        try:
            process.stdin.write(b".")
            process.stdin.close()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
                line = process.stdout.readline()
                if not line:
                    break
                event = json.loads(line)
                if event.get("type") == "system" and event.get("subtype") == "init":
                    return str(event.get("model", ""))
            return ""
        except (BrokenPipeError, OSError, ValueError):
            return ""
        finally:
            process.kill()
            process.wait()
            process.stdout.close()


@lru_cache
def claude_models(cli_path: str | None) -> tuple[dict[str, str], ...]:
    """Return Claude Code aliases with resolved versions in their names, or nothing without cli_path."""
    if not cli_path:
        return ()
    with ThreadPoolExecutor(len(CLAUDE_MODEL_ALIASES)) as pool:
        model_ids = list(pool.map(lambda item: resolve_claude_model(cli_path, item[0]), CLAUDE_MODEL_ALIASES))
    return tuple({"id": alias, "name": claude_model_name(alias, name, model_id)} for (alias, name), model_id in zip(CLAUDE_MODEL_ALIASES, model_ids))


def codex_models(cli_path: str | None) -> list[dict[str, str]]:
    """Read model IDs and display names from cli_path; return an empty list on failure."""
    if not cli_path:
        return []
    try:
        process = subprocess.Popen(
            [cli_path, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return []

    pending = b""
    deadline = time.monotonic() + 8

    def send(message: dict) -> None:
        """Write one JSON-RPC message to process and return nothing."""
        process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        process.stdin.flush()

    def receive(request_id: int) -> dict:
        """Wait for request_id until deadline and return its JSON-RPC response."""
        nonlocal pending
        while time.monotonic() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                break
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                message = json.loads(line)
                if message.get("id") == request_id:
                    return message
        raise TimeoutError("Codex model catalog did not respond")

    try:
        send({"method": "initialize", "id": 0, "params": {"clientInfo": {"name": "develop_agent", "title": "Develop Agent", "version": "0.1.0"}}})
        if "error" in receive(0):
            return []
        send({"method": "initialized", "params": {}})
        models = []
        cursor = None
        while True:
            params = {"limit": 100, "includeHidden": False}
            if cursor:
                params["cursor"] = cursor
            send({"method": "model/list", "id": 1, "params": params})
            response = receive(1)
            if "error" in response:
                return []
            result = response["result"]
            models.extend({"id": item["model"], "name": item["displayName"]} for item in result["data"] if not item.get("hidden"))
            cursor = result.get("nextCursor")
            if not cursor:
                return models
    except (BrokenPipeError, KeyError, OSError, ValueError, TimeoutError):
        return []
    finally:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()
