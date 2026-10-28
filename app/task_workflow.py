"""Coordinate task branches, reviews, and legacy project migration."""

import json
import shutil
import threading
from pathlib import Path
from uuid import uuid4

from app.git.workspace import GitWorkspace, task_branch_name
from app.projects import list_projects
from app.record_index import remove_record_link
from app.storage import create_task, delete_task as remove_records, list_tasks, mark_task_completed, now, plan_hash, read_task, save_metadata, task_dir, task_subtree, update_child_proposal, write_text


class TaskWorkflowService:
    """Apply approved transitions to task-owned branches and records."""

    def __init__(self, repo: Path, tasks_root: Path, projects_root: Path):
        """Store repository and record paths; return an idle service."""
        self.repo = repo
        self.tasks_root = tasks_root
        self.projects_root = projects_root
        self.git = GitWorkspace(repo)
        self.lock = threading.RLock()
        self.agent = None

    def children(self, task_id: str) -> list[dict]:
        """Return direct children of task_id in creation order."""
        return [task for task in list_tasks(self.tasks_root) if task["parent_id"] == task_id]

    def branch_for(self, task_id: str) -> str:
        """Return the branch owned by task_id."""
        task = read_task(self.tasks_root, task_id)
        if not task["branch"]:
            raise ValueError("このタスクに作業ブランチがありません")
        return task["branch"]

    def code_path(self, task_id: str) -> Path:
        """Return the worktree for task_id."""
        return self.git.ensure(self.branch_for(task_id))

    def create_root_task(self, title: str) -> dict:
        """Create a root task and a branch from dev; return its record."""
        with self.lock:
            task_id = uuid4().hex[:12]
            branch = task_branch_name(title, task_id)
            self.git.ensure(branch, "dev")
            try:
                task = create_task(self.tasks_root, title, project_id=task_id, task_id=task_id)
                task["branch"] = branch
                save_metadata(self.tasks_root, task)
                return read_task(self.tasks_root, task_id)
            except Exception:
                remove_record_link(self.tasks_root, task_id)
                shutil.rmtree(task_dir(self.tasks_root, task_id), ignore_errors=True)
                self.git.remove(branch)
                raise

    def create_child(self, parent_id: str, title: str, initial_plan: str = "", proposal_id: str | None = None) -> dict:
        """Create a child branch and record below parent_id; return the child."""
        with self.lock:
            parent = read_task(self.tasks_root, parent_id)
            if parent["status"] == "completed":
                raise ValueError("完了済みタスクには子タスクを追加できません")
            if self.agent and self.agent.related_active(parent_id):
                raise ValueError("親子タスクでAgentを実行中です")
            proposal = None
            if proposal_id:
                proposal = next((item for item in parent["child_proposals"] if item["id"] == proposal_id), None)
                if not proposal or proposal["status"] != "pending" or proposal["plan_hash"] != plan_hash(parent["plan"]):
                    raise ValueError("分割案が古くなりました。計画を確認してください")
            if not self.git.clean(self.code_path(parent_id)):
                raise ValueError("親タスクの未コミット変更を整理してください")
            if parent["code_owner"]:
                self.release_code(parent_id)
            child_id = uuid4().hex[:12]
            branch = task_branch_name(title, child_id)
            self.git.ensure(branch, parent["branch"])
            try:
                child = create_task(self.tasks_root, title, parent_id, parent["root_id"], initial_plan, child_id)
                child["root_id"] = parent["root_id"]
                child["branch"] = branch
                save_metadata(self.tasks_root, child)
                if proposal:
                    update_child_proposal(self.tasks_root, parent_id, proposal_id, "approved", child_id)
                return read_task(self.tasks_root, child_id)
            except Exception:
                remove_record_link(self.tasks_root, child_id)
                shutil.rmtree(task_dir(self.tasks_root, child_id), ignore_errors=True)
                self.git.remove(branch)
                raise

    def reject_proposal(self, task_id: str, proposal_id: str) -> dict:
        """Reject proposal_id for task_id and return the task."""
        task = read_task(self.tasks_root, task_id)
        item = next((value for value in task["child_proposals"] if value["id"] == proposal_id), None)
        if not item or item["status"] != "pending":
            raise ValueError("有効な分割案が見つかりません")
        return update_child_proposal(self.tasks_root, task_id, proposal_id, "rejected")

    def prepare_parallel_children(self, parent_id: str, task_ids: list[str]) -> None:
        """Validate sibling branches before parallel runs; return nothing."""
        if not isinstance(task_ids, list) or len(task_ids) < 2 or len(set(task_ids)) != len(task_ids):
            raise ValueError("並列実行する子タスクを二つ以上選択してください")
        children = {child["id"]: child for child in self.children(parent_id)}
        if any(task_id not in children for task_id in task_ids):
            raise ValueError("同じ親の子タスクだけ選択してください")
        for task_id in task_ids:
            task = read_task(self.tasks_root, task_id)
            if task["status"] == "completed" or not task["can_implement"] or self.children(task_id) or task["implementation"]["state"] != "not_started":
                raise ValueError("計画済みの未着手の葉タスクだけ選択してください")
            if not self.git.clean(self.code_path(task_id)):
                raise ValueError("子タスクの作業ブランチに未コミット変更があります")

    def claim_code(self, task_id: str) -> None:
        """Reserve task_id's clean branch for its Agent run; return nothing."""
        with self.lock:
            task = read_task(self.tasks_root, task_id)
            if task["status"] == "completed":
                raise ValueError("完了済みの変更は編集できません")
            if not task["code_owner"] and not self.git.clean(self.code_path(task_id)):
                raise ValueError("作業ブランチの未コミット変更を整理してください")
            task["code_owner"] = True
            save_metadata(self.tasks_root, task)

    def release_code(self, task_id: str) -> None:
        """Release task_id's branch reservation; return nothing."""
        task = read_task(self.tasks_root, task_id)
        task["code_owner"] = False
        save_metadata(self.tasks_root, task)

    def task_diff(self, task_id: str, path: str | None = None) -> dict:
        """Return task changed files and optional patch for path."""
        task = read_task(self.tasks_root, task_id)
        worktree = None
        if task["merge_hash"]:
            revisions = (f"{task['merge_hash']}^1", task["merge_hash"])
        elif task["commit_hashes"] and task["status"] == "completed":
            revisions = (f"{task['commit_hashes'][0]}^", task["commit_hash"])
        elif task.get("commit_pending") and task["commit_hashes"]:
            worktree = self.code_path(task_id)
            revisions = (f"{task['commit_hashes'][0]}^",)
        elif task["code_owner"]:
            worktree = self.code_path(task_id)
            revisions = ("HEAD",)
        else:
            target = read_task(self.tasks_root, task["parent_id"])["branch"] if task["parent_id"] else "dev"
            revisions = (f"{target}...{task['branch']}",) if task["branch"] else ()
        files = self.git.worktree_diff_files(worktree, revisions[0]) if worktree and task.get("commit_pending") and task["commit_hashes"] else self.git.changed(worktree) if worktree else self.git.diff_files(*revisions) if revisions else []
        result = {"files": files}
        if path is not None:
            if path not in files:
                raise ValueError("このタスクの変更ファイルではありません")
            untracked = worktree is not None and path in self.git.untracked(worktree)
            result["patch"] = self.git.diff_text(path, *revisions, cwd=worktree, untracked=untracked)
        return result

    def summary_basis(self, task_id: str) -> dict:
        """Return child completion IDs and branch HEAD for task_id."""
        return {"children": {child["id"]: child["merge_hash"] or child["commit_hash"] for child in self.children(task_id)}, "head": self.git.head(self.branch_for(task_id))}

    def completion_preview(self, task_id: str) -> dict:
        """Return the current approved completion action for task_id."""
        task = read_task(self.tasks_root, task_id)
        if task["status"] == "completed":
            raise ValueError("タスクは完了済みです")
        children = self.children(task_id)
        reports = task["reports"]
        if not reports:
            raise ValueError("検証レポートを確認してください")
        target = read_task(self.tasks_root, task["parent_id"])["branch"] if task["parent_id"] else "dev"
        if children:
            if any(child["status"] != "completed" for child in children):
                raise ValueError("全ての子タスクの完了が必要です")
            if reports[-1].get("kind") != "summary" or reports[-1].get("summary_basis") != self.summary_basis(task_id):
                raise ValueError("最新の子タスクを含む総括レポートが必要です")
            return self._merge_preview(task, target)
        paths = self.git.changed(self.code_path(task_id))
        if task["commit_hash"] and not task.get("commit_pending") and not paths:
            return self._merge_preview(task, target)
        if task["implementation"]["state"] != "succeeded":
            raise ValueError("実装と検証が完了していません")
        if not paths:
            if task.get("legacy_completed"):
                return {"kind": "legacy", "branch": task["branch"], "head": self.git.head(task["branch"]), "paths": [], "report_number": reports[-1]["number"]}
            raise ValueError("コミットする変更がありません")
        return {"kind": "commit", "branch": task["branch"], "head": self.git.head(task["branch"]), "paths": paths, "snapshot": self.git.worktree_tree(self.code_path(task_id)), "report_number": reports[-1]["number"]}

    def _merge_preview(self, task: dict, target: str) -> dict:
        """Return a merge preview for task and target branch."""
        return {"kind": "merge", "branch": task["branch"], "target": target, "head": self.git.head(task["branch"]), "target_head": self.git.head(target), "paths": self.git.diff_paths(target, task["branch"]), "report_number": task["reports"][-1]["number"], "message": f"{task['title']}を統合"}

    def record_operation(self, task_id: str, operation: dict) -> dict:
        """Persist a Git operation under task_id and return it."""
        path = task_dir(self.tasks_root, task_id) / "operations" / f"{operation['id']}.json"
        write_text(path, json.dumps(operation, ensure_ascii=False, indent=2) + "\n")
        return operation

    def begin_operation(self, task_id: str, kind: str, branch: str, destination: str | None = None) -> dict:
        """Create a pending operation for task_id and return it."""
        return self.record_operation(task_id, {"id": uuid4().hex, "kind": kind, "target_id": task_id, "branch": branch, "destination": destination, "state": "started", "git_hash": None, "created_at": now()})

    def merge_target(self, task_id: str) -> str:
        """Return the branch that task_id merges into: its parent's branch or dev."""
        task = read_task(self.tasks_root, task_id)
        return read_task(self.tasks_root, task["parent_id"])["branch"] if task["parent_id"] else "dev"

    def begin_agent_completion(self, task_id: str, expected: dict) -> dict:
        """Record approved commit and merge operations for an Agent completion run; return both."""
        with self.lock:
            preview = self.completion_preview(task_id)
            task = read_task(self.tasks_root, task_id)
            commit = None
            if preview["kind"] == "commit":
                if any(expected.get(key) != preview[key] for key in ("kind", "head", "paths", "report_number", "snapshot")):
                    raise ValueError("確認後にタスクの変更が変わりました。再確認してください")
                if not task["code_owner"]:
                    raise ValueError("このタスクの作業ブランチが予約されていません")
            elif preview["kind"] == "merge":
                if any(expected.get(key) != preview[key] for key in ("kind", "head", "report_number", "target_head")):
                    raise ValueError("確認後にタスクの状態が変わりました。再確認してください")
            else:
                raise ValueError("このタスクはAgentで統合できません")
            target = self.merge_target(task_id)
            if not self.git.clean(self.git.ensure(target)):
                raise ValueError("統合先の作業ツリーの変更を整理してください")
            merge = self.begin_operation(task_id, "agent_merge", preview["branch"], target)
            if preview["kind"] == "commit":
                commit = self.begin_operation(task_id, "agent_commit", preview["branch"])
                commit.update(base_head=preview["head"], expected_tree=preview["snapshot"], expected_paths=preview["paths"], merge_operation=merge["id"])
                self.record_operation(task_id, commit)
            merge.update(target_head=self.git.head(target), source_head=None if commit else preview["head"], message=f"{task['title']}を統合")
            self.record_operation(task_id, merge)
            return {"commit": commit, "merge": merge}

    def finish_agent_completion(self, operations: dict) -> dict:
        """Verify an Agent run's commits and merge from operations; return the completed task."""
        with self.lock:
            if operations["commit"]:
                self.reconcile_agent_commit(operations["commit"], operations["merge"])
            return self.reconcile_agent_merge(operations["merge"])

    def merged_source(self, merge: dict) -> str | None:
        """Return the source commit of the Agent merge recorded by merge, or None before it exists."""
        head = self.git.head(merge["destination"])
        parents = self.git.git("rev-list", "--parents", "-n", "1", head).stdout.split()
        message = self.git.git("log", "-1", "--format=%B", head).stdout
        if len(parents) == 3 and parents[1] == merge["target_head"] and f"Task-Operation: {merge['id']}" in message:
            return parents[2]
        return None

    def reconcile_agent_commit(self, operation: dict, merge: dict | None = None) -> dict:
        """Validate Agent commits from operation and record them; return the task."""
        with self.lock:
            task_id = operation["target_id"]
            task = read_task(self.tasks_root, task_id)
            branch = operation["branch"]
            source = self.git.head(branch) if self.git.has_branch(branch) else self.merged_source(merge) if merge else None
            if not source:
                raise ValueError("作業ブランチが見つかりません。Git履歴を確認してください")
            hashes = self.git.commits_since(source, operation["base_head"])
            if not hashes:
                raise ValueError("Agentはコミットを作成しませんでした")
            for commit in hashes:
                parents = self.git.git("rev-list", "--parents", "-n", "1", commit).stdout.split()
                message = self.git.git("log", "-1", "--format=%B", commit).stdout
                changed = set(self.git.diff_files(f"{commit}^", commit))
                if len(parents) != 2 or f"Task-Operation: {operation['id']}" not in message or not changed or not changed.issubset(operation["expected_paths"]):
                    raise ValueError("Agentのコミットに対象外の変更があります。Git履歴を確認してください")
            for commit in hashes:
                if commit not in task["commit_hashes"]:
                    task["commit_hashes"].append(commit)
            task["commit_hash"] = task["commit_hashes"][-1]
            task["commit_pending"] = True
            save_metadata(self.tasks_root, task)
            operation.update(git_hash=hashes[-1], state="partial")
            self.record_operation(task_id, operation)
            final_tree = self.git.git("rev-parse", f"{source}^{{tree}}").stdout.strip()
            path = self.git.path(branch)
            if final_tree != operation["expected_tree"] or (self.git.has_branch(branch) and path.exists() and not self.git.clean(path)):
                raise ValueError("一部のコミットだけが作成されました。残りの変更を確認してください")
            task["commit_pending"] = False
            save_metadata(self.tasks_root, task)
            self.release_code(task_id)
            operation["state"] = "complete"
            self.record_operation(task_id, operation)
            return read_task(self.tasks_root, task_id)

    def reconcile_agent_merge(self, operation: dict) -> dict:
        """Verify the Agent's merge from operation, finish branch cleanup, and return the completed task."""
        with self.lock:
            task_id = operation["target_id"]
            task = read_task(self.tasks_root, task_id)
            destination, base = operation["destination"], operation["target_head"]
            head = self.git.head(destination)
            if head == base:
                raise ValueError("Agentはマージを作成しませんでした")
            source = operation["source_head"] or task["commit_hash"]
            if self.merged_source(operation) != source:
                raise ValueError("統合先に想定外のコミットがあります。Git履歴を確認してください")
            expected_tree = self.git.merge_tree(base, source)
            if expected_tree and self.git.git("rev-parse", f"{head}^{{tree}}").stdout.strip() != expected_tree:
                raise ValueError("Agentのマージ結果がGitの自動マージと一致しません。Git履歴を確認してください")
            if not expected_tree:
                fork = self.git.git("merge-base", base, source).stdout.strip()
                if not set(self.git.diff_files(base, head)).issubset(self.git.diff_files(fork, source)):
                    raise ValueError("Agentの競合解決にタスク外の変更があります。Git履歴を確認してください")
            if not self.git.clean(self.git.path(destination)):
                raise ValueError("統合先に未コミットの変更が残っています")
            operation.update(state="git_done", git_hash=head)
            self.record_operation(task_id, operation)
            result = mark_task_completed(self.tasks_root, task_id, head, "merge")
            if self.git.has_branch(operation["branch"]):
                self.git.remove(operation["branch"], destination)
                operation["cleanup"] = "gui"
            self.git.git("worktree", "prune")
            operation["state"] = "complete"
            self.record_operation(task_id, operation)
            return result

    def abandon_agent_merge(self, operation: dict) -> bool:
        """Abort an unfinished Agent merge and mark operation failed; return whether a merge was aborted."""
        with self.lock:
            aborted = self.git.abort_merge(operation["destination"], operation["branch"])
            if self.git.head(operation["destination"]) == operation["target_head"]:
                operation["state"] = "failed"
                self.record_operation(operation["target_id"], operation)
            return aborted

    def complete_task(self, task_id: str, expected: dict) -> dict:
        """Complete a legacy task without new Git history and return its record."""
        with self.lock:
            if self.agent and self.agent.related_active(task_id):
                raise ValueError("このタスクまたは親子タスクでAgentを実行中です")
            preview = self.completion_preview(task_id)
            if preview["kind"] != "legacy" or any(expected.get(key) != preview[key] for key in ("kind", "head", "report_number")):
                raise ValueError("確認後にタスクの状態が変わりました。再確認してください")
            return mark_task_completed(self.tasks_root, task_id, None, "legacy")

    def recover_operations(self) -> None:
        """Finish task metadata after Agent commits or merges interrupted by a restart; return nothing."""
        with self.lock:
            for task in list_tasks(self.tasks_root):
                directory = task_dir(self.tasks_root, task["id"]) / "operations"
                operations = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))] if directory.exists() else []
                by_id = {operation["id"]: operation for operation in operations}
                pending = [operation for operation in operations if operation["state"] not in {"complete", "failed"}]
                for operation in sorted(pending, key=lambda item: item["kind"] != "agent_commit"):
                    try:
                        if operation["kind"] == "agent_commit":
                            self.reconcile_agent_commit(operation, by_id.get(operation.get("merge_operation")))
                        elif operation["kind"] == "agent_merge":
                            self.reconcile_agent_merge(operation)
                        elif self.git.head(operation["destination"]) == operation["git_hash"] or f"Task-Operation: {operation['id']}" in self.git.git("log", "-1", "--format=%B", operation["destination"]).stdout:
                            self._finish_recorded_merge(operation)
                    except ValueError:
                        if operation["kind"] == "agent_merge":
                            self.abandon_agent_merge(operation)

    def _finish_recorded_merge(self, operation: dict) -> None:
        """Finish metadata and branch cleanup for a merge made by an older GUI; return nothing."""
        task_id, destination = operation["target_id"], operation["destination"]
        if read_task(self.tasks_root, task_id)["status"] != "completed":
            mark_task_completed(self.tasks_root, task_id, self.git.head(destination), "merge")
        if self.git.has_branch(operation["branch"]):
            self.git.remove(operation["branch"], destination)
        operation["state"] = "complete"
        self.record_operation(task_id, operation)

    def delete_task(self, task_id: str) -> list[str]:
        """Delete an uncommitted task subtree and its branches; return IDs."""
        with self.lock:
            ids = task_subtree(self.tasks_root, task_id)
            tasks = [read_task(self.tasks_root, item) for item in ids]
            if self.agent and self.agent.active_tasks.intersection(ids):
                raise ValueError("Agent実行中のタスクは削除できません")
            if any(task["commit_hash"] or task["merge_hash"] or task["pre_split_commit"] for task in tasks):
                raise ValueError("Git履歴があるタスクは削除できません")
            for task in tasks:
                if task["branch"] and not self.git.clean(self.code_path(task["id"])):
                    raise ValueError("未コミットの変更があるタスクは削除できません")
            for task in tasks:
                if task["branch"]:
                    self.git.remove(task["branch"])
            return remove_records(self.tasks_root, task_id)

    def migrate_projects(self) -> None:
        """Move legacy project state into root task records without deleting backups."""
        with self.lock:
            for project in list_projects(self.projects_root):
                roots = [task for task in list_tasks(self.tasks_root) if task["project_id"] == project["id"] and task["parent_id"] is None]
                if not roots:
                    continue
                for record in roots:
                    root = read_task(self.tasks_root, record["id"])
                    if root.get("task_migrated"):
                        continue
                    owner_branch = next((name for name, owner in project.get("branch_owners", {}).items() if owner == root["id"]), None)
                    if owner_branch and root["branch"] and owner_branch != root["branch"] and self.git.head(owner_branch) != self.git.head(root["branch"]):
                        raise ValueError(f"タスク {root['id']} に移行前の異なるブランチ履歴があります")
                    branch = owner_branch or root["branch"] or task_branch_name(root["title"], root["id"])
                    shared_owner = project.get("branch_owners", {}).get(branch)
                    if shared_owner and shared_owner != root["id"]:
                        parent_branch = task_branch_name(root["title"] + "-parent", root["id"])
                        self.git.ensure(parent_branch, branch)
                        branch = parent_branch
                    if root["status"] != "completed" and not root["branch"]:
                        self.git.ensure(branch, project["branch"])
                    root["branch"] = branch
                    save_metadata(self.tasks_root, root)
                    for item_id in reversed(task_subtree(self.tasks_root, root["id"])):
                        task = read_task(self.tasks_root, item_id)
                        owned_branch = next((name for name, owner in project.get("branch_owners", {}).items() if owner == item_id), None)
                        if owned_branch and task["branch"] and owned_branch != task["branch"] and self.git.head(owned_branch) != self.git.head(task["branch"]):
                            raise ValueError(f"タスク {item_id} に移行前の異なるブランチ履歴があります")
                        if task["status"] != "completed" and not task["branch"]:
                            task["branch"] = owned_branch or task_branch_name(task["title"], task["id"])
                            if not owned_branch:
                                source = read_task(self.tasks_root, task["parent_id"])["branch"]
                                self.git.ensure(task["branch"], source)
                        task["root_id"] = root["id"]
                        task["project_id"] = root["id"]
                        if project.get("branch_owners", {}).get(task["branch"]) == item_id:
                            task["code_owner"] = True
                        save_metadata(self.tasks_root, task)
                    root = read_task(self.tasks_root, root["id"])
                    root["task_migrated"] = True
                    root["legacy_completed"] = bool(project.get("legacy"))
                    save_metadata(self.tasks_root, root)
