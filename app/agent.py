"""Connect local task chats and implementation runs to Agent CLIs."""

import hashlib
import json
import shutil
import subprocess
import threading
from pathlib import Path
from uuid import uuid4

from app.evidence import list_evidence
from app.model_catalog import codex_models
from app.storage import append_chat, append_report, delete_task, list_tasks, read_chat, read_task, report_draft_path, set_implementation, set_readiness, task_dir, task_subtree, write_text


SKILL_ROOT = Path(__file__).resolve().parent.parent / ".codex" / "skills" / "research-workflow"


def cli_error(events: str) -> str | None:
    """Read CLI JSONL events and return the last reported error, if any."""
    errors = []
    for line in events.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("msg", event)
        if item.get("type") == "error":
            errors.append(item.get("message", "Codex CLI failed"))
    return errors[-1] if errors else None


class AgentManager:
    """Run selected Agent CLI requests and expose each run's current result."""

    def __init__(self, tasks_root: Path, project_root: Path, cli_path: str | None = None):
        """Initialize roots and an optional CLI path; return an idle manager."""
        self.tasks_root = tasks_root
        self.project_root = project_root
        local_cli = project_root / "app" / "cli" / "node_modules" / ".bin" / "codex"
        self.cli_paths = {"codex": cli_path or (str(local_cli) if local_cli.exists() else shutil.which("codex")), "claude": shutil.which("claude")}
        self.runs: dict[str, dict] = {}
        self.active_tasks: set[str] = set()
        self.implementation_running = False
        self.lock = threading.Lock()

    def availability(self) -> dict:
        """Return CLI availability and Codex's picker-visible model catalog."""
        return {"provider": "codex", "available": bool(self.cli_paths["codex"]), "providers": {name: bool(path) for name, path in self.cli_paths.items()}, "models": {"codex": codex_models(self.cli_paths["codex"]), "claude": []}}

    def remove_task(self, task_id: str) -> list[str]:
        """Remove task_id and descendants unless an Agent uses them; return IDs."""
        with self.lock:
            task_ids = task_subtree(self.tasks_root, task_id)
            if self.active_tasks.intersection(task_ids):
                raise ValueError("Agent実行中のタスクは削除できません")
            return delete_task(self.tasks_root, task_id)

    def recover_interrupted_runs(self) -> None:
        """Mark implementation runs left active by an earlier server as failed."""
        for task in list_tasks(self.tasks_root):
            if task.get("implementation", {}).get("state") == "running":
                set_implementation(self.tasks_root, task["id"], "failed", validation_outcome="inconclusive", needs_followup=True)

    def start_chat(self, task_id: str, phase: str, message: str) -> dict:
        """Start a task-local chat for task ID and phase; return its run record."""
        task = read_task(self.tasks_root, task_id)
        if task["status"] == "completed":
            raise ValueError("Completed tasks cannot be edited")
        if phase not in {"plan", "result"}:
            raise ValueError("Invalid chat phase")
        if phase == "result" and not task["implementation"].get("changed_files"):
            raise ValueError("Result chat starts after the first implementation")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("Message is required")
        return self._start(task_id, "chat", phase, message.strip())

    def start_implementation(self, task_id: str) -> dict:
        """Start implementation of a planned task ID; return its run record."""
        task = read_task(self.tasks_root, task_id)
        if not task["can_implement"]:
            raise ValueError("Agent has not approved the saved plan for implementation")
        if task["status"] == "completed":
            raise ValueError("Completed tasks cannot be implemented again")
        return self._start(task_id, "implement", "plan", "")

    def get_run(self, run_id: str) -> dict | None:
        """Return a copy of run ID or None when it does not exist."""
        with self.lock:
            return dict(self.runs[run_id]) if run_id in self.runs else None

    def _start(self, task_id: str, kind: str, phase: str, message: str) -> dict:
        """Register one task run and start its worker; return the run record."""
        writes_code = kind == "implement" or phase == "result"
        with self.lock:
            task = read_task(self.tasks_root, task_id)
            if not self.cli_paths[task["provider"]]:
                raise ValueError(f"{task['provider']} CLI is not installed")
            if task_id in self.active_tasks:
                raise ValueError("This task already has an active Agent run")
            if writes_code and self.implementation_running:
                raise ValueError("Another implementation is running")
            if not writes_code and self.implementation_running:
                raise ValueError("Wait for the implementation to finish")
            if writes_code and any(item["kind"] == "chat" and item["state"] == "running" for item in self.runs.values()):
                raise ValueError("Wait for active chats to finish")
            if kind == "implement" and not read_task(self.tasks_root, task_id)["can_implement"]:
                raise ValueError("Agent has not approved the saved plan for implementation")
            run_id = uuid4().hex
            run = {"id": run_id, "task_id": task_id, "kind": kind, "phase": phase, "state": "running", "message": "", "model": task["model"], "provider": task["provider"], "previous_implementation": dict(task["implementation"])}
            self.runs[run_id] = run
            self.active_tasks.add(task_id)
            if writes_code:
                self.implementation_running = True
        if kind == "chat":
            append_chat(self.tasks_root, task_id, phase, "user", message)
            if phase == "plan":
                set_readiness(self.tasks_root, task_id, False, "", "Agentが要件を再確認しています")
        if writes_code:
            changed_files = task["implementation"].get("changed_files", []) if phase == "result" else []
            set_implementation(self.tasks_root, task_id, "running", changed_files, task["provider"], task["model"])
            write_text(report_draft_path(self.tasks_root, task_id), "")
        threading.Thread(target=self._work, args=(run_id,), daemon=True).start()
        return dict(run)

    def _work(self, run_id: str) -> None:
        """Execute run ID and store its outcome; return nothing."""
        run = self.get_run(run_id)
        task_id, kind, phase = run["task_id"], run["kind"], run["phase"]
        task_path = task_dir(self.tasks_root, task_id)
        output_path = task_path / f".codex-{run_id}.txt"
        previous_plan = None
        before = None
        previous_reports = []
        writes_code = kind == "implement" or phase == "result"
        try:
            before = self._change_snapshot()
            current_task = read_task(self.tasks_root, task_id)
            previous_reports = [(task_path / report["file"], report["content"]) for report in current_task["reports"]]
            previous_plan = current_task["plan"] if kind == "chat" and phase == "plan" else None
            prompt = self._implementation_prompt(task_id) if kind == "implement" else self._chat_prompt(task_id, phase)
            schema_name = "plan-response.schema.json" if kind == "chat" and phase == "plan" else "validation-response.schema.json"
            schema_path = SKILL_ROOT / "references" / schema_name
            if run["provider"] == "codex":
                command = [
                    self.cli_paths["codex"], "exec", "--json", "--output-last-message", str(output_path),
                    "--cd", str(self.project_root if writes_code else task_path),
                ]
                if run["model"]:
                    command += ["--model", run["model"]]
                command += ["--output-schema", str(schema_path)]
                if kind == "chat" and phase == "result":
                    images = [entry for entry in list_evidence(self.tasks_root, task_id) if entry["kind"] == "image"][-4:]
                    if images:
                        command += ["--image", *(str(task_path / "evidence" / entry["name"]) for entry in images)]
                command += ["--sandbox", "workspace-write", "--config", "sandbox_workspace_write.network_access=true", "-"]
            else:
                command = [self.cli_paths["claude"], "--print", "--output-format", "json", "--permission-mode", "acceptEdits"]
                if run["model"]:
                    command += ["--model", run["model"]]
                command += ["--json-schema", schema_path.read_text(encoding="utf-8")]
            result = subprocess.run(
                command, input=prompt, text=True, capture_output=True,
                cwd=self.project_root if writes_code else task_path, timeout=1800,
            )
            if run["provider"] == "codex":
                answer = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else ""
            else:
                payload = json.loads(result.stdout) if result.returncode == 0 else {}
                value = payload.get("structured_output", payload.get("result"))
                answer = json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value or ""
            if result.returncode != 0:
                raise RuntimeError((cli_error(result.stdout) or result.stderr or f"{run['provider']} CLI failed")[-1200:])
            if not answer:
                raise RuntimeError(cli_error(result.stdout) or "Agent CLI returned no response")
            response = json.loads(answer)
            if any(not path.exists() or path.read_text(encoding="utf-8") != content for path, content in previous_reports):
                raise RuntimeError("Agent changed a previous validation report")
            if kind == "chat" and phase == "plan" and self._change_snapshot() != before:
                raise RuntimeError("A chat changed project files outside its task directory")
            if kind == "chat" and phase == "plan":
                if not isinstance(response, dict) or not isinstance(response.get("message"), str) or not isinstance(response.get("implementation_ready"), bool) or not isinstance(response.get("reason"), str):
                    raise ValueError("Agent returned an invalid plan response")
                questions = response["questions"]
                valid_questions = isinstance(questions, list) and all(
                    isinstance(item, dict)
                    and isinstance(item.get("prompt"), str)
                    and isinstance(item.get("options"), list)
                    and 2 <= len(item["options"]) <= 4
                    and all(isinstance(option, str) for option in item["options"])
                    for item in questions
                )
                if not valid_questions:
                    raise ValueError("Agent returned an invalid choice question")
                plan = read_task(self.tasks_root, task_id)["plan"]
                ready = bool(response["implementation_ready"] and plan.strip() and not questions)
                set_readiness(self.tasks_root, task_id, ready, plan, response["reason"], previous_plan)
                append_chat(self.tasks_root, task_id, phase, "assistant", response["message"], questions=questions, readiness_reason=response["reason"], provider=run["provider"], model=run["model"])
                outcome = {"state": "succeeded", "message": response["message"]}
            elif kind == "chat":
                has_report = self._validate_result(task_id, response)
                after = self._change_snapshot()
                changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
                if has_report:
                    task = append_report(self.tasks_root, task_id, response["outcome"], changed, response["evidence_files"], run["provider"], run["model"], response["needs_followup"])
                    report_number = task["reports"][-1]["number"]
                else:
                    if changed or response["outcome"] != "inconclusive" or not response["needs_followup"]:
                        raise ValueError("A changed implementation or completed validation needs a report")
                    previous = run["previous_implementation"]
                    set_implementation(self.tasks_root, task_id, previous["state"], previous.get("changed_files", []), previous.get("provider", run["provider"]), previous.get("model", run["model"]), previous.get("validation_outcome", ""), True)
                    report_number = None
                append_chat(self.tasks_root, task_id, phase, "assistant", response["message"], report_number=report_number, changed_files=changed, validation_outcome=response["outcome"], needs_followup=response["needs_followup"], provider=run["provider"], model=run["model"])
                outcome = {"state": "succeeded", "message": response["message"], "changed_files": changed, "validation_outcome": response["outcome"], "report_number": report_number}
            else:
                if not self._validate_result(task_id, response):
                    raise ValueError("Implementation needs a validation report")
                after = self._change_snapshot()
                changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
                if not changed:
                    raise RuntimeError("Agent finished, but no project code change was detected")
                task = append_report(self.tasks_root, task_id, response["outcome"], changed, response["evidence_files"], run["provider"], run["model"], response["needs_followup"])
                report_number = task["reports"][-1]["number"]
                append_chat(self.tasks_root, task_id, "result", "system", f"実装と検証を実行し、レポート {report_number} を追加しました。変更ファイル: {', '.join(changed)}\n\n{response['message']}", report_number=report_number)
                outcome = {"state": "succeeded", "message": response["message"], "changed_files": changed, "validation_outcome": response["outcome"], "report_number": report_number}
        except (OSError, RuntimeError, ValueError, KeyError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            for path, content in previous_reports:
                if not path.exists() or path.read_text(encoding="utf-8") != content:
                    write_text(path, content)
            if writes_code:
                previous_files = read_task(self.tasks_root, task_id)["implementation"]["changed_files"]
                after = self._change_snapshot() if before is not None else {}
                changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path)) if before is not None else []
                set_implementation(self.tasks_root, task_id, "failed", sorted(set(previous_files) | set(changed)), run["provider"], run["model"], "inconclusive", True)
                append_chat(self.tasks_root, task_id, "result", "system", f"実装または検証に失敗しました: {error}")
            else:
                if phase == "plan" and previous_plan is not None:
                    plan = read_task(self.tasks_root, task_id)["plan"]
                    set_readiness(self.tasks_root, task_id, False, plan, "Agentの計画更新を確認してください", previous_plan)
                append_chat(self.tasks_root, task_id, phase, "system", f"Agentの実行に失敗しました: {error}")
            outcome = {"state": "failed", "message": str(error)}
        finally:
            output_path.unlink(missing_ok=True)
        with self.lock:
            self.runs[run_id].update(outcome)
            self.active_tasks.discard(task_id)
            if writes_code:
                self.implementation_running = False

    def _chat_prompt(self, task_id: str, phase: str) -> str:
        """Build a task-local prompt from a task ID and phase; return text."""
        task = read_task(self.tasks_root, task_id)
        history = read_chat(self.tasks_root, task_id, phase)[-12:]
        conversation = "\n".join(f"{item['role']}: {item['content']}" for item in history)
        evidence = list_evidence(self.tasks_root, task_id) if phase == "result" else []
        if phase == "plan":
            instructions = "01 計画チャットです。開発コードは変更せず、計画と実装可否を整理してください。"
            references = "references/01-plan.md と assets/plan-template.md"
        else:
            instructions = (
                "02 検証結果チャットです。依頼が検証だけならコードを変更せず、修正が必要ならコードを直して再検証してください。"
                "新しい結果が得られた場合は、今回分だけの独立したレポートを report-draft.md に書いてください。"
                "過去のレポートは編集しないでください。結果がまだない場合はレポートを書かず、チャットで理由を説明してください。"
            )
            references = "references/02-validation.md と assets/result-template.md"
        return (
            "日本語で応答してください。研究開発タスクの相談相手です。指定タスクの記録を直接更新してください。"
            f"{instructions}最初に {SKILL_ROOT / 'SKILL.md'} と {references} を読んでください。"
            f"{'plan.md' if phase == 'plan' else 'report-draft.md'} を必要に応じて更新し、指定のJSON Schemaで短く返してください。"
            "ほかのタスクは変更しないでください。Gitコミットとマージは行わないでください。\n"
            f"タスク: {task['title']}\n画面: {phase}\n"
            f"タスク保存先: {task_dir(self.tasks_root, task_id)}\n開発コードの場所: {self.project_root}\n"
            f"保存済みの計画:\n{task['plan']}\n最新のレポート:\n{task['result']}\n"
            f"検証資料フォルダ: {task_dir(self.tasks_root, task_id) / 'evidence'}\n"
            f"検証資料: {json.dumps(evidence, ensure_ascii=False)}\n"
            f"この画面の会話:\n{conversation}"
        )

    def _implementation_prompt(self, task_id: str) -> str:
        """Build an implementation prompt from task ID; return its text."""
        task = read_task(self.tasks_root, task_id)
        return (
            "日本語で応答してください。以下の承認済み計画に従い、このリポジトリで実際にコードを変更してください。"
            "コード変更後、そのまま計画の確かめ方を実行してください。今回の結果はタスクのreport-draft.md、"
            "検証用スクリプトはvalidation/、画像・動画・測定データはevidence/に保存してください。"
            "生成した画像は自分で開いて体裁を確認し、内容の解釈をreport-draft.mdに書いてください。"
            "この実行はタスクの完了判断ではありません。Gitコミットやマージは行わないでください。"
            "計画を変更する必要が生じた場合は作業を止め、理由を返答してください。"
            "検証で仮説が支持されない場合も観測結果を残し、指定のJSON Schemaで結論を返してください。\n"
            f"最初に {SKILL_ROOT / 'SKILL.md'} と references/02-validation.md、assets/result-template.md を読んでください。\n"
            f"タスク: {task['title']}\nタスク保存先: {task_dir(self.tasks_root, task_id)}\n計画:\n{task['plan']}"
        )

    def _validate_result(self, task_id: str, response: dict) -> bool:
        """Validate a task response and return whether its draft has content."""
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("message"), str)
            or response.get("outcome") not in {"supported", "not_supported", "inconclusive"}
            or not isinstance(response.get("needs_followup"), bool)
            or "questions" in response
        ):
            raise ValueError("Agent returned an invalid validation response")
        files = response.get("evidence_files")
        available = {f"evidence/{entry['name']}" for entry in list_evidence(self.tasks_root, task_id)}
        if not isinstance(files, list) or any(not isinstance(name, str) or name not in available for name in files):
            raise ValueError("Agent referenced a missing evidence file")
        return bool(report_draft_path(self.tasks_root, task_id).read_text(encoding="utf-8").strip())

    def _change_snapshot(self) -> dict[str, str]:
        """Return hashes of changed and untracked project files for comparison."""
        commands = (["git", "diff", "--name-only", "HEAD", "-z"], ["git", "ls-files", "--others", "--exclude-standard", "-z"])
        paths = set()
        for command in commands:
            output = subprocess.run(command, cwd=self.project_root, capture_output=True, check=True).stdout
            paths.update(item.decode("utf-8") for item in output.split(b"\0") if item)
        snapshot = {}
        for name in paths:
            path = self.project_root / name
            snapshot[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "deleted"
        return snapshot
