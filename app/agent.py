"""Connect local task chats and implementation runs to Agent CLIs."""

import hashlib
import json
import shutil
import subprocess
import threading
from pathlib import Path
from uuid import uuid4

from app.agent_stream import run_streamed
from app.evidence import list_evidence
from app.model_catalog import codex_models
from app.projects import read_project
from app.storage import append_chat, append_report, delete_task, list_tasks, plan_hash, read_chat, read_task, report_draft_path, save_child_proposals, set_implementation, set_readiness, task_dir, task_subtree, write_text


SKILL_ROOT = Path(__file__).resolve().parent.parent / ".codex" / "skills" / "research-workflow"


class PlanChangeNeeded(ValueError):
    """Carry the Agent's reason for stopping before implementation."""


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


def valid_proposals(value: object) -> bool:
    """Return whether value is a list of complete child task proposals."""
    fields = {"title", "purpose", "scope", "verification", "reason"}
    return isinstance(value, list) and all(isinstance(item, dict) and set(item) == fields and all(isinstance(item[key], str) and item[key].strip() for key in fields) for item in value)


class AgentManager:
    """Run selected Agent CLI requests and expose each run's current result."""

    def __init__(self, tasks_root: Path, project_root: Path, cli_path: str | None = None, workflow=None):
        """Initialize roots and an optional CLI path; return an idle manager."""
        self.tasks_root = tasks_root
        self.project_root = project_root
        self.workflow = workflow
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
        if phase == "result" and self.workflow and self.workflow.children(task_id):
            raise ValueError("親タスクでは子タスクの総括を実行してください")
        if phase == "result" and not task["implementation"].get("changed_files"):
            raise ValueError("Result chat starts after the first implementation")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("Message is required")
        return self._start(task_id, "chat", phase, message.strip())

    def start_implementation(self, task_id: str, additional_instruction: str = "") -> dict:
        """Start a planned task with an optional one-run instruction; return its run."""
        task = read_task(self.tasks_root, task_id)
        if not task["can_implement"]:
            raise ValueError("Agent has not approved the saved plan for implementation")
        if task["status"] == "completed":
            raise ValueError("Completed tasks cannot be implemented again")
        if self.workflow and self.workflow.children(task_id):
            raise ValueError("子タスクがある場合は親の総括を作成してください")
        return self._start(task_id, "implement", "plan", "", additional_instruction)

    def start_summary(self, task_id: str, additional_instruction: str = "") -> dict:
        """Start a parent summary with an optional one-run instruction; return its run."""
        task = read_task(self.tasks_root, task_id)
        if task["status"] == "completed":
            raise ValueError("完了済みタスクの総括は更新できません")
        if self.workflow and read_project(self.workflow.projects_root, task["project_id"]).get("needs_review"):
            raise ValueError("移行前のGit変更を確認してください")
        if not self.workflow or not self.workflow.children(task_id):
            raise ValueError("総括する子タスクがありません")
        if any(child["status"] != "completed" for child in self.workflow.children(task_id)):
            raise ValueError("全ての子タスクを完了してください")
        return self._start(task_id, "summary", "result", "", additional_instruction)

    def get_run(self, run_id: str) -> dict | None:
        """Return a copy of run ID or None when it does not exist."""
        with self.lock:
            if run_id not in self.runs:
                return None
            run = self.runs[run_id]
            return {**run, "activity": [dict(item) for item in run["activity"]]}

    def _record_activity(self, run_id: str, activity: dict) -> None:
        """Apply one tool activity update to run ID and return nothing."""
        with self.lock:
            entries = self.runs[run_id]["activity"]
            activity_id = activity.get("id")
            match = next((item for item in entries if activity_id and item["id"] == activity_id), None)
            if match:
                match["state"] = activity["state"]
                if activity.get("label"):
                    match["label"] = str(activity["label"])[:800]
            elif activity.get("label"):
                entries.append({"id": activity_id, "label": str(activity["label"])[:800], "state": activity["state"]})
                del entries[:-12]

    def _start(self, task_id: str, kind: str, phase: str, message: str, additional_instruction: str = "") -> dict:
        """Register one task run and start its worker; return the run record."""
        if not isinstance(additional_instruction, str):
            raise ValueError("追加指示は文字列で入力してください")
        writes_code = kind == "implement" or (kind == "chat" and phase == "result")
        with self.lock:
            task = read_task(self.tasks_root, task_id)
            if task["status"] == "completed":
                raise ValueError("完了済みタスクではAgentを実行できません")
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
            if writes_code and self.workflow and task["project_id"]:
                self.workflow.claim_code(task_id)
            run_id = uuid4().hex
            run = {"id": run_id, "task_id": task_id, "kind": kind, "phase": phase, "state": "running", "message": "", "model": task["model"], "provider": task["provider"], "previous_implementation": dict(task["implementation"]), "additional_instruction": additional_instruction.strip(), "activity": []}
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
        elif kind == "summary":
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
        writes_code = kind == "implement" or (kind == "chat" and phase == "result")
        try:
            code_root = self.workflow.code_path(task_id) if self.workflow and read_task(self.tasks_root, task_id)["project_id"] else self.project_root
            before = self._change_snapshot(code_root)
            current_task = read_task(self.tasks_root, task_id)
            previous_reports = [(task_path / report["file"], report["content"]) for report in current_task["reports"]]
            previous_plan = current_task["plan"] if kind == "chat" and phase == "plan" else None
            prompt = self._implementation_prompt(task_id, run["additional_instruction"]) if kind == "implement" else self._summary_prompt(task_id, run["additional_instruction"]) if kind == "summary" else self._chat_prompt(task_id, phase)
            schema_name = "plan-response.schema.json" if kind == "chat" and phase == "plan" else "validation-response.schema.json"
            schema_path = SKILL_ROOT / "references" / schema_name
            if run["provider"] == "codex":
                command = [
                    self.cli_paths["codex"], "exec", "--json", "--output-last-message", str(output_path),
                    "--cd", str(code_root if writes_code else task_path),
                ]
                if writes_code and self.workflow:
                    command += ["--add-dir", str(task_path)]
                if run["model"]:
                    command += ["--model", run["model"]]
                command += ["--output-schema", str(schema_path)]
                if kind == "chat" and phase == "result":
                    images = [entry for entry in list_evidence(self.tasks_root, task_id) if entry["kind"] == "image"][-4:]
                    if images:
                        command += ["--image", *(str(task_path / "evidence" / entry["name"]) for entry in images)]
                command += ["--sandbox", "workspace-write", "--config", "sandbox_workspace_write.network_access=true", "-"]
            else:
                command = [self.cli_paths["claude"], "--print", "--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits"]
                if run["model"]:
                    command += ["--model", run["model"]]
                command += ["--json-schema", schema_path.read_text(encoding="utf-8")]
            result = run_streamed(command, prompt, code_root if writes_code else task_path, run["provider"], lambda activity: self._record_activity(run_id, activity))
            if run["provider"] == "codex":
                answer = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else ""
            else:
                events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
                payload = next((event for event in reversed(events) if event.get("type") == "result" or "structured_output" in event), {}) if result.returncode == 0 else {}
                value = payload.get("structured_output", payload.get("result"))
                answer = json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value or ""
            if result.returncode != 0:
                raise RuntimeError((cli_error(result.stdout) or result.stderr or f"{run['provider']} CLI failed")[-1200:])
            if not answer:
                raise RuntimeError(cli_error(result.stdout) or "Agent CLI returned no response")
            response = json.loads(answer)
            if any(not path.exists() or path.read_text(encoding="utf-8") != content for path, content in previous_reports):
                raise RuntimeError("Agent changed a previous validation report")
            if kind == "chat" and phase == "plan" and self._change_snapshot(code_root) != before:
                raise RuntimeError("A chat changed project files outside its task directory")
            if kind == "summary" and self._change_snapshot(code_root) != before:
                raise RuntimeError("A summary changed project code")
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
                if not valid_questions or not valid_proposals(response.get("child_task_proposals", [])):
                    raise ValueError("Agent returned an invalid choice question")
                plan = read_task(self.tasks_root, task_id)["plan"]
                ready = bool(response["implementation_ready"] and plan.strip() and not questions and not response.get("child_task_proposals"))
                set_readiness(self.tasks_root, task_id, ready, plan, response["reason"], previous_plan)
                save_child_proposals(self.tasks_root, task_id, response.get("child_task_proposals", []), plan_hash(plan))
                append_chat(self.tasks_root, task_id, phase, "assistant", response["message"], questions=questions, readiness_reason=response["reason"], provider=run["provider"], model=run["model"])
                outcome = {"state": "succeeded", "message": response["message"]}
            elif kind == "summary":
                if not self._validate_result(task_id, response):
                    raise ValueError("総括レポートを保存してください")
                basis = self.workflow.summary_basis(task_id)
                task = append_report(self.tasks_root, task_id, response["outcome"], [], response["evidence_files"], run["provider"], run["model"], response["needs_followup"], "summary", basis)
                append_chat(self.tasks_root, task_id, "result", "assistant", response["message"], report_number=task["reports"][-1]["number"])
                outcome = {"state": "succeeded", "message": response["message"], "report_number": task["reports"][-1]["number"]}
            elif kind == "chat":
                has_report = self._validate_result(task_id, response)
                after = self._change_snapshot(code_root)
                changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
                if has_report:
                    task = append_report(self.tasks_root, task_id, response["outcome"], changed, response["evidence_files"], run["provider"], run["model"], response["needs_followup"], commit_message=response.get("commit_message", ""))
                    report_number = task["reports"][-1]["number"]
                else:
                    if changed or response["outcome"] != "inconclusive" or not response["needs_followup"]:
                        raise ValueError("A changed implementation or completed validation needs a report")
                    previous = run["previous_implementation"]
                    set_implementation(self.tasks_root, task_id, previous["state"], previous.get("changed_files", []), previous.get("provider", run["provider"]), previous.get("model", run["model"]), previous.get("validation_outcome", ""), True)
                    report_number = None
                append_chat(self.tasks_root, task_id, phase, "assistant", response["message"], report_number=report_number, changed_files=changed, validation_outcome=response["outcome"], needs_followup=response["needs_followup"], provider=run["provider"], model=run["model"])
                if response.get("child_task_proposals"):
                    save_child_proposals(self.tasks_root, task_id, response["child_task_proposals"], plan_hash(read_task(self.tasks_root, task_id)["plan"]))
                outcome = {"state": "succeeded", "message": response["message"], "changed_files": changed, "validation_outcome": response["outcome"], "report_number": report_number}
            else:
                has_report = self._validate_result(task_id, response)
                after = self._change_snapshot(code_root)
                changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
                if not has_report:
                    if not changed and response["outcome"] == "inconclusive" and response["needs_followup"]:
                        raise PlanChangeNeeded(response["message"].strip() or "計画の変更が必要です")
                    raise ValueError("Implementation needs a validation report")
                if not changed:
                    raise RuntimeError("Agent finished, but no project code change was detected")
                task = append_report(self.tasks_root, task_id, response["outcome"], changed, response["evidence_files"], run["provider"], run["model"], response["needs_followup"], commit_message=response.get("commit_message", ""))
                report_number = task["reports"][-1]["number"]
                append_chat(self.tasks_root, task_id, "result", "system", f"実装と検証を実行し、レポート {report_number} を追加しました。変更ファイル: {', '.join(changed)}\n\n{response['message']}", report_number=report_number)
                outcome = {"state": "succeeded", "message": response["message"], "changed_files": changed, "validation_outcome": response["outcome"], "report_number": report_number}
        except (OSError, RuntimeError, ValueError, KeyError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            for path, content in previous_reports:
                if not path.exists() or path.read_text(encoding="utf-8") != content:
                    write_text(path, content)
            if writes_code:
                previous_files = read_task(self.tasks_root, task_id)["implementation"]["changed_files"]
                after = self._change_snapshot(code_root) if before is not None else {}
                changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path)) if before is not None else []
                set_implementation(self.tasks_root, task_id, "failed", sorted(set(previous_files) | set(changed)), run["provider"], run["model"], "inconclusive", True)
                message = (
                    f"実装を停止しました。理由: {error}\n\n01 計画に戻り、Agentに計画書の再生成を依頼してください。"
                    if isinstance(error, PlanChangeNeeded)
                    else f"実装または検証に失敗しました: {error}"
                )
                append_chat(self.tasks_root, task_id, "result", "system", message)
            else:
                if phase == "plan" and previous_plan is not None:
                    plan = read_task(self.tasks_root, task_id)["plan"]
                    set_readiness(self.tasks_root, task_id, False, plan, "Agentの計画更新を確認してください", previous_plan)
                append_chat(self.tasks_root, task_id, phase, "system", f"Agentの実行に失敗しました: {error}")
            outcome = {"state": "failed", "message": message if isinstance(error, PlanChangeNeeded) else str(error)}
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
                "コードを変更した場合は、JSONのcommit_messageに簡潔な日本語のコミットメッセージ案を書いてください。"
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
            f"タスク保存先: {task_dir(self.tasks_root, task_id)}\n開発コードの場所: {self.workflow.code_path(task_id) if self.workflow and task['project_id'] else self.project_root}\n"
            f"保存済みの計画:\n{task['plan']}\n最新のレポート:\n{task['result']}\n"
            f"検証資料フォルダ: {task_dir(self.tasks_root, task_id) / 'evidence'}\n"
            f"検証資料: {json.dumps(evidence, ensure_ascii=False)}\n"
            f"この画面の会話:\n{conversation}"
        )

    def _implementation_prompt(self, task_id: str, additional_instruction: str = "") -> str:
        """Build an implementation prompt from task ID and instruction; return text."""
        task = read_task(self.tasks_root, task_id)
        return (
            "日本語で応答してください。以下の承認済み計画に従い、このリポジトリで実際にコードを変更してください。"
            "コード変更後、そのまま計画の確かめ方を実行してください。今回の結果はタスクのreport-draft.md、"
            "検証用スクリプトはvalidation/、画像・動画・測定データはevidence/に保存してください。"
            "生成した画像は自分で開いて体裁を確認し、内容の解釈をreport-draft.mdに書いてください。"
            "JSONのcommit_messageに今回の変更を表す簡潔な日本語のコミットメッセージ案を書いてください。"
            "この実行はタスクの完了判断ではありません。Gitコミットやマージは行わないでください。"
            "計画を変更する必要が生じた場合は作業を止め、理由を返答してください。"
            "検証で仮説が支持されない場合も観測結果を残し、指定のJSON Schemaで結論を返してください。\n"
            f"最初に {SKILL_ROOT / 'SKILL.md'} と references/02-validation.md、assets/result-template.md を読んでください。\n"
            f"タスク: {task['title']}\nタスク保存先: {task_dir(self.tasks_root, task_id)}\n計画:\n{task['plan']}"
            + (f"\n今回の実行への追加指示（計画は変更しない）:\n{additional_instruction}" if additional_instruction else "")
        )

    def _summary_prompt(self, task_id: str, additional_instruction: str = "") -> str:
        """Build a read-only summary prompt from task ID and instruction; return text."""
        task = read_task(self.tasks_root, task_id)
        children = [read_task(self.tasks_root, child["id"]) for child in self.workflow.children(task_id)]
        details = "\n".join(f"{child['title']} / report={child['reports'][-1]['number'] if child['reports'] else 'なし'} / commit={child['merge_hash'] or child['commit_hash']} / result={child['result']}" for child in children)
        return (
            "日本語で子タスクの結果を総括してください。コードとGitは変更しません。"
            "子タスクの資料リンクを親タスクの資料としてコピーせず、子タスク名とレポート番号で根拠を示してください。"
            f"最初に {SKILL_ROOT / 'SKILL.md'} と references/02-validation.md を読んでください。"
            f"総括を {report_draft_path(self.tasks_root, task_id)} に保存し、検証用JSON Schemaで返してください。"
            f"親タスク: {task['title']}\n計画: {task['plan']}\n分割前のcommit: {task['pre_split_commit']}\n分割前の検証: {task['result']}\n子タスク:\n{details}"
            + (f"\n今回の総括への追加指示（計画は変更しない）:\n{additional_instruction}" if additional_instruction else "")
        )

    def _validate_result(self, task_id: str, response: dict) -> bool:
        """Validate a task response and return whether its draft has content."""
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("message"), str)
            or response.get("outcome") not in {"supported", "not_supported", "inconclusive"}
            or not isinstance(response.get("needs_followup"), bool)
            or "questions" in response
            or not valid_proposals(response.get("child_task_proposals", []))
            or not isinstance(response.get("commit_message", ""), str)
        ):
            raise ValueError("Agent returned an invalid validation response")
        files = response.get("evidence_files")
        available = {f"evidence/{entry['name']}" for entry in list_evidence(self.tasks_root, task_id)}
        if not isinstance(files, list) or any(not isinstance(name, str) or name not in available for name in files):
            raise ValueError("Agent referenced a missing evidence file")
        return bool(report_draft_path(self.tasks_root, task_id).read_text(encoding="utf-8").strip())

    def _change_snapshot(self, code_root: Path | None = None) -> dict[str, str]:
        """Return hashes of changed and untracked files under code_root."""
        code_root = code_root or self.project_root
        commands = (["git", "diff", "--name-only", "HEAD", "-z"], ["git", "ls-files", "--others", "--exclude-standard", "-z"])
        paths = set()
        for command in commands:
            output = subprocess.run(command, cwd=code_root, capture_output=True, check=True).stdout
            paths.update(item.decode("utf-8") for item in output.split(b"\0") if item)
        snapshot = {}
        for name in paths:
            path = code_root / name
            snapshot[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "deleted"
        return snapshot
