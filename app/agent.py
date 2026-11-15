"""Connect local task chats and implementation runs to Agent CLIs."""

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import threading
from pathlib import Path
from uuid import uuid4

from app.agent_stream import run_streamed
from app.claude_response import read_claude_response, save_diagnostic
from app.attachments import validate_chat_files
from app.evidence import evidence_rule, list_evidence, validate_chat_images
from app.model_catalog import claude_models, codex_models
from app.storage import append_chat, append_report, delete_task, list_tasks, now, plan_hash, read_chat, read_task, report_draft_path, save_child_proposals, set_completion_waiting, set_implementation, set_readiness, set_run_cancelled, task_dir, task_subtree, write_text


SKILL_ROOT = Path(__file__).resolve().parent.parent / ".codex" / "skills" / "research-workflow"
CLAUDE_ALLOWED_DOMAINS = [
    "github.com", "*.github.com", "*.githubusercontent.com", "pypi.org", "*.pypi.org", "*.pythonhosted.org",
    "registry.npmjs.org", "*.npmjs.org", "huggingface.co", "*.huggingface.co", "*.hf.co", "*.pytorch.org",
    "*.anaconda.org", "astral.sh", "*.astral.sh",
]


def html_document_rule() -> str:
    """Return the prompt text that defines how plan and report HTML documents are written."""
    return (
        "新しい計画とレポートはHTMLで書いてください（<body>の中身だけの断片で可）。"
        "<script>、イベント属性（onclick など）、外部のCSS・フォント・画像・iframeは使わず、ファイル単体で表示できる形にしてください。"
        "文字ばかりにせず、構成図・処理の流れ・比較表・結果グラフをインラインSVGと<table>で示し、本文は短く要点だけを書いてください。"
        "SVGにはviewBoxと表示幅のwidthを付け、文字と線はcurrentColorか明示した色にしてください（表示は暗い背景です）。"
        "画像は<img src=\"../evidence/<ファイル名>\" alt=\"説明\">（計画では evidence/<ファイル名> も可）、動画は<video controls src=\"../evidence/<ファイル名>\">で参照してください。"
    )


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
        if not isinstance(event, dict):
            continue
        item = event.get("msg", event)
        if not isinstance(item, dict):
            continue
        if item.get("type") == "error":
            errors.append(item.get("message", "Codex CLI failed"))
        elif item.get("type") == "result" and item.get("is_error") and (item.get("result") or item.get("errors")):
            errors.append(str(item.get("result") or "; ".join(map(str, item["errors"]))))
    return errors[-1] if errors else None


def claude_command(cli_path: str, model: str, schema_path: Path, directories: list[Path], sandbox: str) -> list[str]:
    """Build a Claude Code print command from model, schema, extra directories, and sandbox mode."""
    command = [cli_path, "--print", "--output-format", "stream-json", "--verbose", "--add-dir", *map(str, directories)]
    if sandbox == "danger-full-access":
        command += ["--dangerously-skip-permissions"]
    else:
        domains = CLAUDE_ALLOWED_DOMAINS + [item.strip() for item in os.environ.get("DEVELOP_AGENT_CLAUDE_ALLOWED_DOMAINS", "").split(",") if item.strip()]
        settings = {"sandbox": {"enabled": True, "failIfUnavailable": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False, "network": {"allowedDomains": domains}}}
        command += ["--permission-mode", "acceptEdits", "--settings", json.dumps(settings)]
    if model:
        command += ["--model", model]
    return command + ["--json-schema", schema_path.read_text(encoding="utf-8")]


def valid_proposals(value: object) -> bool:
    """Return whether value is a list of complete child task proposals."""
    fields = {"title", "purpose", "scope", "verification", "reason"}
    return isinstance(value, list) and all(isinstance(item, dict) and set(item) == fields and all(isinstance(item[key], str) and item[key].strip() for key in fields) for item in value)


def prompt_excerpt(value: str, limit: int = 3000) -> str:
    """Return at most limit characters of text for a CLI prompt."""
    return value if len(value) <= limit else value[:limit] + "\n…（続きは保存済みファイルを参照）"


class AgentManager:
    """Run selected Agent CLI requests and expose each run's current result."""

    def __init__(self, tasks_root: Path, project_root: Path, cli_path: str | None = None, workflow=None, codex_sandbox: str = "workspace-write", process_env: dict[str, str] | None = None):
        """Initialize roots, CLI settings, and process variables; return an idle manager."""
        self.tasks_root = tasks_root
        self.project_root = project_root
        self.workflow = workflow
        if codex_sandbox not in {"workspace-write", "danger-full-access"}:
            raise ValueError("Unsupported Codex sandbox")
        self.codex_sandbox = codex_sandbox
        self.process_env = process_env
        local_cli = project_root / "app" / "cli" / "node_modules" / ".bin" / "codex"
        self.cli_paths = {"codex": cli_path or (str(local_cli) if local_cli.exists() else shutil.which("codex")), "claude": shutil.which("claude")}
        self.runs: dict[str, dict] = {}
        self.cancel_events: dict[str, threading.Event] = {}
        self.active_tasks: set[str] = set()
        self.merge_targets: dict[str, str] = {}
        self.lock = threading.RLock()

    def related_active(self, task_id: str) -> bool:
        """Return whether task_id or an ancestor or descendant has an active run."""
        task = read_task(self.tasks_root, task_id)
        ancestors = set()
        while task.get("parent_id"):
            ancestors.add(task["parent_id"])
            task = read_task(self.tasks_root, task["parent_id"])
        return bool(self.active_tasks.intersection({task_id, *ancestors, *task_subtree(self.tasks_root, task_id)}))

    def availability(self) -> dict:
        """Return CLI availability and each CLI's picker-visible model catalog."""
        return {"provider": "codex", "available": bool(self.cli_paths["codex"]), "providers": {name: bool(path) for name, path in self.cli_paths.items()}, "models": {"codex": codex_models(self.cli_paths["codex"]), "claude": claude_models(self.cli_paths["claude"])}}

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

    def start_chat(self, task_id: str, phase: str, message: str, images: object = None, files: object = None) -> dict:
        """Start a task chat with text, image names, and attachment names; return its run record."""
        task = read_task(self.tasks_root, task_id)
        if task["status"] == "completed":
            raise ValueError("Completed tasks cannot be edited")
        if phase not in {"plan", "result"}:
            raise ValueError("Invalid chat phase")
        if phase == "plan" and task["completion_waiting"]:
            raise ValueError("Agentの完了確認があります。02 検証結果のチャットで回答してください")
        if phase == "result" and task["completion_waiting"]:
            if not isinstance(message, str) or not message.strip() or images or files:
                raise ValueError("Agentの完了確認には、文章だけで回答してください")
            return self.resume_completion(task_id, message)
        if phase == "result" and self.workflow and self.workflow.children(task_id):
            raise ValueError("親タスクでは子タスクの総括を実行してください")
        if phase == "result" and not task["implementation"].get("changed_files"):
            raise ValueError("Result chat starts after the first implementation")
        image_names = validate_chat_images(self.tasks_root, task_id, images if images is not None else [])
        file_names = validate_chat_files(self.tasks_root, task_id, files if files is not None else [])
        if not isinstance(message, str) or not (message.strip() or image_names or file_names):
            raise ValueError("Message is required")
        return self._start(task_id, "chat", phase, message.strip(), images=image_names, files=file_names)

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

    def start_parallel_children(self, parent_id: str, task_ids: list[str], additional_instruction: str = "") -> list[dict]:
        """Start sibling IDs with an optional shared instruction; return their runs."""
        if not self.workflow:
            raise ValueError("並列実行にはGit作業領域が必要です")
        if not isinstance(additional_instruction, str):
            raise ValueError("追加指示は文字列で入力してください")
        with self.lock:
            for task_id in task_ids:
                task = read_task(self.tasks_root, task_id)
                if not self.cli_paths[task["provider"]]:
                    raise ValueError(f"{task['provider']} CLI is not installed")
                if self.related_active(task_id):
                    raise ValueError("このタスクまたは親子タスクでAgentを実行中です")
            self.workflow.prepare_parallel_children(parent_id, task_ids)
            return [self.start_implementation(task_id, additional_instruction) for task_id in task_ids]

    def start_summary(self, task_id: str, additional_instruction: str = "") -> dict:
        """Start a parent summary with an optional one-run instruction; return its run."""
        task = read_task(self.tasks_root, task_id)
        if task["status"] == "completed":
            raise ValueError("完了済みタスクの総括は更新できません")
        if task.get("migration_pending"):
            raise ValueError("移行前のGit変更を確認してください")
        if not self.workflow or not self.workflow.children(task_id):
            raise ValueError("総括する子タスクがありません")
        if any(child["status"] != "completed" for child in self.workflow.children(task_id)):
            raise ValueError("全ての子タスクを完了してください")
        return self._start(task_id, "summary", "result", "", additional_instruction)

    def resume_completion(self, task_id: str, answer: str) -> dict:
        """Answer the Agent's pending completion question with answer and rerun the completion; return its run record."""
        waiting = read_task(self.tasks_root, task_id)["completion_waiting"]
        if not waiting:
            raise ValueError("Agentの完了確認はありません")
        preview = self.workflow.completion_preview(task_id)
        if preview["kind"] == "commit" and not set(preview["paths"]) <= set(waiting["paths"]):
            raise ValueError("質問のあとに変更が増えました。「タスクを完了」から確認し直してください")
        instruction = f"Agentの質問: {waiting['question']}\nユーザーの回答: {answer.strip()}"
        return self.start_completion(task_id, preview, instruction, answer.strip())

    def start_completion(self, task_id: str, expected: dict, instruction: str = "", answer: str | None = None) -> dict:
        """Start an approved Agent run that commits task ID's changes if needed and merges it; return its run record."""
        if not isinstance(instruction, str):
            raise ValueError("コミット・統合の方針は文字列で入力してください")
        with self.lock:
            if self.related_active(task_id):
                raise ValueError("このタスクまたは親子タスクでAgentを実行中です")
            task = read_task(self.tasks_root, task_id)
            if not self.cli_paths[task["provider"]]:
                raise ValueError(f"{task['provider']} CLI is not installed")
            target = self.workflow.merge_target(task_id)
            if target in self.merge_targets:
                raise ValueError(f"{target} への統合を別のタスクで実行中です。終わってから再実行してください")
            operations = self.workflow.begin_agent_completion(task_id, expected)
            kind = "commit" if operations["commit"] else "merge"
            request = "タスク完了のコミットと統合を指示しました。" if kind == "commit" else "タスクの統合を指示しました。"
            append_chat(self.tasks_root, task_id, "result", "user", answer if answer is not None else f"{request}\n方針: {instruction.strip() or 'なし'}")
            set_completion_waiting(self.tasks_root, task_id, None)
            run_id = uuid4().hex
            run = {"id": run_id, "task_id": task_id, "kind": kind, "phase": "result", "state": "running", "message": "", "model": task["model"], "provider": task["provider"], "activity": [], "cancel_requested": False, "cancelable": True}
            self.runs[run_id] = run
            self.cancel_events[run_id] = threading.Event()
            self.active_tasks.add(task_id)
            self.merge_targets[target] = task_id
            set_run_cancelled(self.tasks_root, task_id, False)
        threading.Thread(target=self._guard, args=(self._work_completion, run_id, operations, instruction.strip()), daemon=True).start()
        return dict(run)

    def _work_completion(self, run_id: str, operations: dict, instruction: str) -> None:
        """Ask the Agent to commit, merge, and delete the task branch, then verify and record the result in the result chat."""
        run = self.get_run(run_id)
        task_id = run["task_id"]
        merge = operations["merge"]
        task_path = task_dir(self.tasks_root, task_id)
        output_path = task_path / f".codex-{run_id}.txt"
        diagnostic_path = task_path / "diagnostics" / f"completion-{run_id}.json"
        previous_hashes = set(read_task(self.tasks_root, task_id)["commit_hashes"])
        result = None
        reply = ""
        error = None
        aborted = False
        try:
            git = self.workflow.git
            source_path = self.workflow.code_path(task_id)
            target_path = git.ensure(merge["destination"])
            common_dir = git.git("rev-parse", "--git-common-dir", cwd=target_path).stdout.strip()
            directories = [task_path, (target_path / common_dir).resolve(), git.worktrees]
            prompt = self._completion_prompt(task_id, operations, source_path, target_path, instruction)
            schema_path = SKILL_ROOT / "references" / "commit-response.schema.json"
            if run["provider"] == "codex":
                command = [self.cli_paths["codex"], "exec", "--json", "--output-last-message", str(output_path), "--cd", str(target_path)]
                for directory in directories:
                    command += ["--add-dir", str(directory)]
                if run["model"]:
                    command += ["--model", run["model"]]
                command += ["--output-schema", str(schema_path), "--sandbox", "workspace-write", "-"]
            else:
                command = claude_command(self.cli_paths["claude"], run["model"], schema_path, [*directories, SKILL_ROOT], "workspace-write")
            result = run_streamed(command, prompt, target_path, run["provider"], lambda activity: self._record_activity(run_id, activity), self.cancel_events[run_id], self.process_env)
            with self.lock:
                self.runs[run_id]["cancelable"] = False
            if result.returncode:
                error = cli_error(result.stdout) or result.stderr or f"{run['provider']} CLI failed"
            response = self._commit_response(run["provider"], result, output_path, diagnostic_path)
            reply = response["message"]
            if response["needs_user_input"] and response["question"].strip():
                question = response["question"].strip()
                aborted = self.workflow.abandon_agent_merge(merge)
                if operations["commit"] and self.workflow.git.head(merge["branch"]) != operations["commit"]["base_head"]:
                    self.workflow.reconcile_agent_commit(operations["commit"], merge)
                set_completion_waiting(self.tasks_root, task_id, {"question": question, "asked_at": now(), "paths": operations["commit"]["expected_paths"] if operations["commit"] else []})
                outcome = {"state": "needs_input", "message": question}
            else:
                self.workflow.finish_agent_completion(operations)
                outcome = {"state": "succeeded", "message": "統合し、タスクを完了しました"}
        except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as failure:
            detail = str(failure)
            cancelled = self.cancel_events[run_id].is_set()
            outcome = {"state": "cancelled" if cancelled else "failed", "message": "実行を中断しました。作成済みのコミットと統合先の状態を確認してください。" if cancelled else f"{error}: {detail}" if error else detail}
            aborted = self.workflow.abandon_agent_merge(merge)
            if result is not None:
                save_diagnostic(result, diagnostic_path)
        finally:
            output_path.unlink(missing_ok=True)
        task = read_task(self.tasks_root, task_id)
        created = [commit for commit in task["commit_hashes"] if commit not in previous_hashes]
        if outcome["state"] == "needs_input":
            append_chat(self.tasks_root, task_id, "result", "assistant", f"{outcome['message']}\n\n完了はまだ終わっていません。このチャットに回答してください。回答すると続きから実行します。", provider=run["provider"], model=run["model"])
            with self.lock:
                self.runs[run_id].update(outcome)
                self.active_tasks.discard(task_id)
                self.merge_targets.pop(merge["destination"], None)
                self.cancel_events.pop(run_id, None)
            return
        lines = [outcome["message"] if outcome["state"] != "failed" else f"コミット・統合に失敗しました: {outcome['message']}"]
        if operations["commit"]:
            lines.append(f"作成したコミット: {', '.join(commit[:10] for commit in created) or 'なし'}")
        if outcome["state"] == "succeeded":
            lines.append(f"マージ: {merge['branch']} → {merge['destination']} ({task['merge_hash'][:10]})")
        if merge.get("cleanup") == "gui":
            lines.append("Agentが作業ブランチを削除しなかったため、GUIが削除しました。")
        if aborted:
            lines.append(f"途中のマージを中止し、{merge['destination']} を元に戻しました。")
        if diagnostic_path.exists():
            lines.append(f"診断ファイル: {diagnostic_path}")
        if reply:
            lines.append(f"\nAgentの報告:\n{reply}")
        append_chat(self.tasks_root, task_id, "result", "system", "\n".join(lines))
        set_run_cancelled(self.tasks_root, task_id, outcome["state"] == "cancelled")
        with self.lock:
            self.runs[run_id].update(outcome)
            self.active_tasks.discard(task_id)
            self.merge_targets.pop(merge["destination"], None)
            self.cancel_events.pop(run_id, None)

    def _commit_response(self, provider: str, result: subprocess.CompletedProcess, output_path: Path, diagnostic_path: Path) -> dict:
        """Return the Agent's completion report from CLI output as message, needs_user_input, and question; fields default to empty or False when unreadable."""
        try:
            response = json.loads(output_path.read_text(encoding="utf-8")) if provider == "codex" else read_claude_response(result, diagnostic_path)
        except (OSError, RuntimeError, ValueError):
            response = {}
        response = response if isinstance(response, dict) else {}
        return {"message": str(response.get("message", "")), "needs_user_input": response.get("needs_user_input") is True, "question": str(response.get("question", ""))}

    def _guard(self, worker, run_id: str, *args: object) -> None:
        """Run worker for run ID and, if it raises unexpectedly, mark the run failed and release its task; return nothing."""
        try:
            worker(run_id, *args)
        except Exception as error:
            with self.lock:
                run = self.runs[run_id]
                run.update(state="failed", message=f"予期しないエラーで停止しました: {error}")
                self.active_tasks.discard(run["task_id"])
                for target, task_id in list(self.merge_targets.items()):
                    if task_id == run["task_id"]:
                        del self.merge_targets[target]
                self.cancel_events.pop(run_id, None)
            if read_task(self.tasks_root, run["task_id"])["implementation"]["state"] == "running":
                set_implementation(self.tasks_root, run["task_id"], "failed", validation_outcome="inconclusive", needs_followup=True)
            raise

    def cancel_run(self, run_id: str) -> dict:
        """Request cancellation of a running CLI by run ID; return its run record."""
        with self.lock:
            run = self.runs.get(run_id)
            if run is None or run["state"] != "running" or not run["cancelable"]:
                raise ValueError("実行中のAgentが見つかりません")
            run["cancel_requested"] = True
            self.cancel_events[run_id].set()
            return {**run, "activity": [dict(item) for item in run["activity"]]}

    def _completion_prompt(self, task_id: str, operations: dict, source_path: Path, target_path: Path, instruction: str) -> str:
        """Build a commit-and-merge prompt from task ID, operations, and checkout paths; return its text."""
        task = read_task(self.tasks_root, task_id)
        commit, merge = operations["commit"], operations["merge"]
        steps = []
        if commit:
            steps.append(
                f"作業ブランチ {merge['branch']} の作業場所 {source_path} で、現在の未コミット変更だけを意味のある単位で一件以上の通常のGitコミットに分けてください。"
                "小さな変更なら一件にまとめて構いません。別の目的の変更は分け、必要なら同じファイルの変更も部分的にステージしてください。"
                "コミットメッセージは簡潔な日本語にし、各コミット本文の末尾に次の行を正確に入れてください。"
                f"\nTask-Operation: {commit['id']}\n対象ファイル: {json.dumps(commit['expected_paths'], ensure_ascii=False)}\n"
                "全ての変更をコミットし、作業ツリーを空にしてください。"
            )
        dirty = operations.get("target_dirty") or {"tracked": [], "untracked": []}
        moved = self.workflow.git.worktrees / "_completion-moved" / merge["id"][:8]
        steps.append(
            f"統合先 {merge['destination']} の作業場所 {target_path}（現在のディレクトリ）の状態を `git status` で確認してください。"
            f"起動時点の記録: 未追跡ファイル {json.dumps(dirty['untracked'], ensure_ascii=False)}、追跡中の未コミット変更 {json.dumps(dirty['tracked'], ensure_ascii=False)}。"
            "統合前に統合先の作業ツリーをきれいにする必要があります。判断は次のとおりです。\n"
            f"- 未追跡ファイルはユーザーの持ち物かもしれません。削除せず、`{moved}/` へ元の相対パスのまま移動して退避し、移動先を報告してください。"
            "ただしマージで同名のファイルが入る場合は、上書きせず退避してからマージしてください。\n"
            "- 追跡中の未コミット変更（編集済みファイル）は、stash、reset、checkout、コミットなどで勝手に動かさず、マージを始めずに質問してください。\n"
            "- 原因が分からない、またはこれ以外で判断に迷う状態も、何も変更せずに質問してください。\n"
            "質問するときは、JSON の needs_user_input を true、question に「何が起きたか・選択肢・あなたのおすすめ」を日本語の文章で簡潔に書いてください。"
            "質問する場合は、それまでにマージを始めていれば `git merge --abort` で元に戻してください。"
        )
        steps.append(
            f"統合先 {merge['destination']} の作業場所 {target_path}（現在のディレクトリ）で次のコマンドを実行してください。\n"
            f"{shlex.join(['git', 'merge', '--no-ff', '-m', merge['message'], '-m', 'Task-Operation: ' + merge['id'], merge['branch']])}\n"
            "競合した場合は、競合したファイルだけを両方の変更の意図を保って解決し、それ以外のファイルは変更しないでください。"
            "解決後は `git add` してから `git commit --no-edit` でマージを完了し、メッセージの Task-Operation 行を残してください。"
            "確実に解決できない場合は `git merge --abort` で元に戻し、needs_user_input を true にして、どのファイルで何が競合したかと解決案を question に書いてください。"
        )
        steps.append(
            f"マージが成功した場合だけ、{target_path} で `git worktree remove {source_path}` と `git branch -d {merge['branch']}` を実行し、作業ブランチを削除してください。"
        )
        return (
            "日本語で作業してください。ユーザーがこのタスクの変更全体を確認し、完了と統合を承認しました。次の手順を順に行ってください。\n"
            + "".join(f"{number}. {step}\n" for number, step in enumerate(steps, 1))
            + "コード、計画、レポートを変更せず、対象外のファイルをコミットしないでください。"
            "rebase、reset、amend、cherry-pick、push、強制オプション、指示にないブランチの操作は行わないでください。"
            "終わったらJSONで結果を短く返してください。質問がなければ needs_user_input は false、question は空文字にしてください。\n"
            f"最新の検証の結果: {task['implementation'].get('validation_outcome') or '不明'}（supported は仮説を支持）、追加の検証が必要: {'はい' if task['implementation'].get('needs_followup') else 'いいえ'}。"
            "結果が仮説を支持していない、または追加の検証が必要と記録されていて、ユーザーの方針でそれを承知の上と分からない場合は、統合せずに質問してください。\n"
            f"最初に {SKILL_ROOT / 'SKILL.md'} を読んでください。\n"
            f"タスク: {task['title']}\n"
            + (f"ユーザーのコミット・統合の方針:\n{instruction}\n" if instruction else "")
        )

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

    def _start(self, task_id: str, kind: str, phase: str, message: str, additional_instruction: str = "", images: list[str] | None = None, files: list[str] | None = None) -> dict:
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
            if not self.workflow and self.active_tasks:
                raise ValueError("Git作業領域のないタスクは同時実行できません")
            if self.related_active(task_id):
                raise ValueError("このタスクまたは親子タスクでAgentを実行中です")
            if kind == "implement" and not read_task(self.tasks_root, task_id)["can_implement"]:
                raise ValueError("Agent has not approved the saved plan for implementation")
            if writes_code and self.workflow:
                self.workflow.claim_code(task_id)
            run_id = uuid4().hex
            run = {"id": run_id, "task_id": task_id, "kind": kind, "phase": phase, "state": "running", "message": "", "model": task["model"], "provider": task["provider"], "previous_implementation": dict(task["implementation"]), "additional_instruction": additional_instruction.strip(), "images": images or [], "files": files or [], "activity": [], "cancel_requested": False, "cancelable": True}
            self.runs[run_id] = run
            self.cancel_events[run_id] = threading.Event()
            self.active_tasks.add(task_id)
            set_run_cancelled(self.tasks_root, task_id, False)
        if kind == "chat":
            append_chat(self.tasks_root, task_id, phase, "user", message, images=images or [], **({"files": files} if files else {}))
            if phase == "plan":
                set_readiness(self.tasks_root, task_id, False, "", "Agentが要件を再確認しています")
        if writes_code:
            changed_files = task["implementation"].get("changed_files", []) if phase == "result" else []
            set_implementation(self.tasks_root, task_id, "running", changed_files, task["provider"], task["model"])
            write_text(report_draft_path(self.tasks_root, task_id), "")
        elif kind == "summary":
            write_text(report_draft_path(self.tasks_root, task_id), "")
        threading.Thread(target=self._guard, args=(self._work, run_id), daemon=True).start()
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
            code_root = self.workflow.code_path(task_id) if self.workflow else self.project_root
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
                if kind == "chat":
                    image_names = list(run["images"])
                    if phase == "result":
                        recent = [entry["name"] for entry in list_evidence(self.tasks_root, task_id) if entry["kind"] == "image"][-4:]
                        image_names += [name for name in recent if name not in image_names]
                    if image_names:
                        command += ["--image", *(str(task_path / "evidence" / name) for name in image_names)]
                command += ["--sandbox", self.codex_sandbox]
                if self.codex_sandbox == "workspace-write":
                    command += ["--config", "sandbox_workspace_write.network_access=true"]
                command += ["-"]
            else:
                directories = [task_path] if writes_code else [code_root, *([self.tasks_root] if kind == "summary" else [])]
                command = claude_command(self.cli_paths["claude"], run["model"], schema_path, [*directories, SKILL_ROOT], self.codex_sandbox)
            result = run_streamed(command, prompt, code_root if writes_code else task_path, run["provider"], lambda activity: self._record_activity(run_id, activity), self.cancel_events[run_id], self.process_env)
            with self.lock:
                self.runs[run_id]["cancelable"] = False
                cancelled = self.cancel_events[run_id].is_set()
            if cancelled:
                raise RuntimeError("Agentの実行を中断しました")
            if run["provider"] == "codex":
                answer = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else ""
                if result.returncode != 0:
                    raise RuntimeError((cli_error(result.stdout) or result.stderr or "codex CLI failed")[-1200:])
                if not answer:
                    raise RuntimeError(cli_error(result.stdout) or "Agent CLI returned no response")
                response = json.loads(answer)
            else:
                response = read_claude_response(result, task_path / "diagnostics" / f"claude-{run_id}.json", cli_error(result.stdout))
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
        except (OSError, RuntimeError, ValueError, KeyError, subprocess.CalledProcessError) as error:
            cancelled = self.cancel_events[run_id].is_set()
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
                    else "Agentの実行を中断しました。作業中の変更を確認してください。" if cancelled
                    else f"実装または検証に失敗しました: {error}"
                )
                append_chat(self.tasks_root, task_id, "result", "system", message)
            else:
                if phase == "plan" and previous_plan is not None:
                    plan = read_task(self.tasks_root, task_id)["plan"]
                    set_readiness(self.tasks_root, task_id, False, plan, "Agentの計画更新を確認してください", previous_plan)
                append_chat(self.tasks_root, task_id, phase, "system", "Agentの実行を中断しました。" if cancelled else f"Agentの実行に失敗しました: {error}")
            outcome = {"state": "cancelled" if cancelled else "failed", "message": message if isinstance(error, PlanChangeNeeded) else "Agentの実行を中断しました。作業中の変更を確認してください。" if cancelled else str(error)}
        finally:
            output_path.unlink(missing_ok=True)
        set_run_cancelled(self.tasks_root, task_id, outcome["state"] == "cancelled")
        with self.lock:
            self.runs[run_id].update(outcome)
            self.active_tasks.discard(task_id)
            self.cancel_events.pop(run_id, None)

    def _chat_prompt(self, task_id: str, phase: str) -> str:
        """Build a task-local prompt from a task ID and phase; return text."""
        task = read_task(self.tasks_root, task_id)
        history = read_chat(self.tasks_root, task_id, phase)[-12:]
        conversation = "\n".join(
            f"{item['role']}: {prompt_excerpt(item['content'], 1500)}"
            + (f"\n添付画像: {', '.join(str(task_dir(self.tasks_root, task_id) / 'evidence' / name) for name in item['images'])}" if item.get("images") else "")
            + (f"\n添付ファイル: {', '.join(str(task_dir(self.tasks_root, task_id) / 'attachments' / name) for name in item['files'])}" if item.get("files") else "")
            for item in history
        )
        evidence = list_evidence(self.tasks_root, task_id) if phase == "result" else []
        recent_evidence = evidence[-20:]
        if phase == "plan":
            instructions = "01 計画チャットです。開発コードは変更せず、計画と実装可否を整理してください。"
            references = "references/01-plan.md と assets/plan-template.html"
        else:
            instructions = (
                "02 検証結果チャットです。依頼が検証だけならコードを変更せず、修正が必要ならコードを直して再検証してください。"
                "コードを変更した場合は、JSONのcommit_messageに簡潔な日本語のコミットメッセージ案を書いてください。"
                "新しい結果が得られた場合は、今回分だけの独立したレポートを report-draft.html に書いてください。"
                "過去のレポートは編集しないでください。結果がまだない場合はレポートを書かず、チャットで理由を説明してください。"
                f"{evidence_rule()}"
            )
            references = "references/02-validation.md と assets/result-template.html"
        return (
            "日本語で応答してください。研究開発タスクの相談相手です。指定タスクの記録を直接更新してください。"
            f"{instructions}最初に {SKILL_ROOT / 'SKILL.md'} と {references} を読んでください。"
            f"{task['plan_file'] if phase == 'plan' else 'report-draft.html'} を必要に応じて更新し、指定のJSON Schemaで短く返してください。"
            + (f"この既存タスクの計画はMarkdown形式です。{task['plan_file']} はMarkdownのまま更新し、HTMLへ変換しないでください。" if phase == "plan" and task["plan_format"] == "markdown" else "")
            + f"{html_document_rule()}"
            "ほかのタスクは変更しないでください。Gitコミットとマージは行わないでください。\n"
            f"タスク: {task['title']}\n画面: {phase}\n"
            f"タスク保存先: {task_dir(self.tasks_root, task_id)}\n開発コードの場所: {self.workflow.code_path(task_id) if self.workflow else self.project_root}\n"
            f"保存済みの計画:\n{prompt_excerpt(task['plan'], 10000)}\n最新のレポート抜粋:\n{prompt_excerpt(task['result'])}\n"
            f"レポート全文: {task_dir(self.tasks_root, task_id) / task['reports'][-1]['file'] if task['reports'] else 'なし'}\n"
            f"検証資料フォルダ: {task_dir(self.tasks_root, task_id) / 'evidence'}\n"
            f"検証資料: 全{len(evidence)}件、最近の{len(recent_evidence)}件: {json.dumps(recent_evidence, ensure_ascii=False)}。必要なファイルだけ開いてください。\n"
            "最新のユーザー発言に添付画像があれば、その画像を実際に確認してから回答してください。Claude CodeではReadツールで画像ファイルを開いてください。添付ファイルがあれば、形式に合う方法（テキストは直接読む、PDF・表計算・圧縮ファイルなどは読み取り用のコマンドやライブラリ）で中身を確認してから回答してください。添付ファイルは編集しないでください。\n"
            f"この画面の会話:\n{conversation}"
        )

    def _implementation_prompt(self, task_id: str, additional_instruction: str = "") -> str:
        """Build an implementation prompt from task ID and instruction; return text."""
        task = read_task(self.tasks_root, task_id)
        return (
            "日本語で応答してください。以下の承認済み計画に従い、このリポジトリで実際にコードを変更してください。"
            "コード変更後、そのまま計画の確かめ方を実行してください。今回の結果はタスクのreport-draft.html、"
            "検証用スクリプトはvalidation/、画像・動画・測定データはevidence/に保存してください。"
            f"{evidence_rule()}"
            "生成した画像は自分で開いて体裁を確認し、内容の解釈をreport-draft.htmlに書いてください。"
            f"{html_document_rule()}"
            "JSONのcommit_messageに今回の変更を表す簡潔な日本語のコミットメッセージ案を書いてください。"
            "この実行はタスクの完了判断ではありません。Gitコミットやマージは行わないでください。"
            "計画を変更する必要が生じた場合は作業を止め、理由を返答してください。"
            "検証で仮説が支持されない場合も観測結果を残し、指定のJSON Schemaで結論を返してください。\n"
            f"最初に {SKILL_ROOT / 'SKILL.md'} と references/02-validation.md、assets/result-template.html を読んでください。\n"
            f"タスク: {task['title']}\nタスク保存先: {task_dir(self.tasks_root, task_id)}\n計画:\n{task['plan']}"
            + (f"\n今回の実行への追加指示（計画は変更しない）:\n{additional_instruction}" if additional_instruction else "")
        )

    def _summary_prompt(self, task_id: str, additional_instruction: str = "") -> str:
        """Build a read-only summary prompt from task ID and instruction; return text."""
        task = read_task(self.tasks_root, task_id)
        children = [read_task(self.tasks_root, child["id"]) for child in self.workflow.children(task_id)]
        details = "\n".join(f"{child['title']} / report={child['reports'][-1]['number'] if child['reports'] else 'なし'} / commit={child['merge_hash'] or child['commit_hash']} / result={prompt_excerpt(child['result'], 1500)} / 保存先={task_dir(self.tasks_root, child['id'])}" for child in children)
        return (
            "日本語で子タスクの結果を総括してください。コードとGitは変更しません。"
            "子タスクの資料リンクを親タスクの資料としてコピーせず、子タスク名とレポート番号で根拠を示してください。"
            f"最初に {SKILL_ROOT / 'SKILL.md'} と references/02-validation.md を読んでください。"
            f"総括を {report_draft_path(self.tasks_root, task_id)} に保存し、検証用JSON Schemaで返してください。{html_document_rule()}"
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
        draft = report_draft_path(self.tasks_root, task_id)
        if not draft.read_text(encoding="utf-8").strip() and draft.with_suffix(".md").exists():
            raise ValueError(f"レポートは {draft.name} にHTMLで書いてください")
        return bool(draft.read_text(encoding="utf-8").strip())

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
