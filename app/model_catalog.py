"""Read the picker-visible model catalog from the installed Codex CLI."""

import json
import os
import select
import subprocess
import time


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
