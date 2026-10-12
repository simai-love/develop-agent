"""Coordinate project records, task completion, and Git history."""

import json
import shutil
import threading
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from app.git.workspace import GitWorkspace, task_branch_name
from app.projects import list_projects, new_project, project_dir, read_project, save_project
from app.storage import create_task, delete_task as remove_task_records, list_tasks, mark_task_completed, now, plan_hash, read_task, save_metadata, task_dir, task_subtree, update_child_proposal, write_text


class WorkflowService:
    """Apply user-approved task and project transitions."""

    def __init__(self, repo: Path, tasks_root: Path, projects_root: Path):
        """Store the repository and record roots and initialize a transition lock."""
        self.repo = repo
        self.tasks_root = tasks_root
        self.projects_root = projects_root
        self.git = GitWorkspace(repo)
        self.lock = threading.RLock()
        self.agent = None

    def children(self, task_id: str) -> list[dict]:
        """Return direct children of task_id in creation order."""
        return [task for task in list_tasks(self.tasks_root) if task["parent_id"] == task_id]

    def project_tasks(self, project_id: str) -> list[dict]:
        """Return all tasks assigned to project_id."""
        return [task for task in list_tasks(self.tasks_root) if task["project_id"] == project_id]

    def branch_for(self, task_id: str) -> str:
        """Return the task's own or nearest ancestor's working branch."""
        task = read_task(self.tasks_root, task_id)
        while task["parent_id"] and not task["branch"]:
            task = read_task(self.tasks_root, task["parent_id"])
        return task["branch"] or read_project(self.projects_root, task["project_id"])["branch"]

    def code_path(self, task_id: str) -> Path:
        """Return the code worktree for task_id."""
        return self.git.ensure(self.branch_for(task_id))

    def task_diff(self, task_id: str, path: str | None = None) -> dict:
        """Return task changed paths and an optional per-file patch for task_id."""
        task = read_task(self.tasks_root, task_id)
        worktree = None
        if task["merge_hash"]:
            revisions = (f"{task['merge_hash']}^1", task["merge_hash"])
        elif self.children(task_id):
            branch = self.branch_for(task_id)
            parent = task["parent_id"]
            target = self.branch_for(parent) if parent else read_project(self.projects_root, task["project_id"])["branch"]
            revisions = (f"{target}...{branch}",)
        elif task["commit_hash"] and task["status"] == "completed":
            revisions = (f"{task['commit_hash']}^", task["commit_hash"])
        elif task["status"] == "completed":
            revisions = ()
        else:
            branch = self.branch_for(task_id)
            project = read_project(self.projects_root, task["project_id"])
            if project.get("branch_owners", {}).get(branch) != task_id:
                revisions = ()
            else:
                worktree = self.git.ensure(branch)
                revisions = ("HEAD",)
        if worktree:
            files = self.git.changed(worktree)
        elif revisions:
            files = self.git.diff_files(*revisions)
        else:
            files = []
        result = {"files": files}
        if path is not None:
            if path not in files:
                raise ValueError("このタスクの変更ファイルではありません")
            untracked = worktree is not None and path in self.git.untracked(worktree)
            result["patch"] = self.git.diff_text(path, *revisions, cwd=worktree, untracked=untracked)
        return result

    def claim_code(self, task_id: str) -> None:
        """Reserve a branch for task_id until its changes are committed."""
        with self.lock:
            task = read_task(self.tasks_root, task_id)
            if task["status"] == "completed":
                raise ValueError("完了済みタスクではコードを変更できません")
            project = read_project(self.projects_root, task["project_id"])
            if project.get("needs_review"):
                raise ValueError("移行前のGit変更を確認してください")
            branch = self.branch_for(task_id)
            owners = project.setdefault("branch_owners", {})
            owner = owners.get(branch)
            if owner and owner != task_id:
                raise ValueError("このブランチには別タスクの未コミット変更があります")
            if not owner and not self.git.clean(self.git.ensure(branch)):
                raise ValueError("作業ブランチの未コミット変更を整理してください")
            owners[branch] = task_id
            project["updated_at"] = now()
            save_project(self.projects_root, project)

    def release_code(self, task_id: str) -> None:
        """Release the branch reservation held by task_id."""
        task = read_task(self.tasks_root, task_id)
        project = read_project(self.projects_root, task["project_id"])
        branch = self.branch_for(task_id)
        if project.setdefault("branch_owners", {}).get(branch) == task_id:
            del project["branch_owners"][branch]
            project["updated_at"] = now()
            save_project(self.projects_root, project)

    def record_operation(self, project_id: str, operation: dict) -> dict:
        """Persist a Git transition operation and return its record."""
        path = project_dir(self.projects_root, project_id) / "operations" / f"{operation['id']}.json"
        write_text(path, json.dumps(operation, ensure_ascii=False, indent=2) + "\n")
        return operation

    def begin_operation(self, project_id: str, kind: str, target_id: str, branch: str, destination: str | None = None) -> dict:
        """Create a pending Git operation and return its record."""
        return self.record_operation(project_id, {"id": uuid4().hex, "kind": kind, "target_id": target_id, "branch": branch, "destination": destination, "state": "started", "git_hash": None, "created_at": now()})

    def recover_operations(self) -> None:
        """Finish metadata updates for Git operations committed before a crash."""
        with self.lock:
            for project in list_projects(self.projects_root):
                directory = project_dir(self.projects_root, project["id"]) / "operations"
                for path in sorted(directory.glob("*.json")) if directory.exists() else []:
                    operation = json.loads(path.read_text(encoding="utf-8"))
                    if operation["state"] == "complete":
                        continue
                    branch = operation["destination"] or operation["branch"]
                    head = self.git.head(branch)
                    message = self.git.git("log", "-1", "--format=%B", branch).stdout
                    if operation["git_hash"] != head and f"Task-Operation: {operation['id']}" not in message:
                        continue
                    operation["git_hash"] = head
                    if operation["kind"] == "project":
                        record = read_project(self.projects_root, operation["target_id"])
                        record.update(status="completed", merge_hash=head, updated_at=now())
                        save_project(self.projects_root, record)
                    else:
                        task = read_task(self.tasks_root, operation["target_id"])
                        if task["status"] != "completed":
                            mark_task_completed(self.tasks_root, task["id"], head, "commit" if operation["kind"] == "commit" else "merge")
                        if operation["kind"] == "commit":
                            self.release_code(task["id"])
                    if operation["kind"] in {"merge", "project"} and self.git.git("show-ref", "--verify", f"refs/heads/{operation['branch']}", check=False).returncode == 0:
                        self.git.remove(operation["branch"], operation["destination"])
                    operation["state"] = "complete"
                    self.record_operation(project["id"], operation)

    def create_project(self, name: str, slug: str, first_task_title: str) -> dict:
        """Create a project branch and first task, returning the project record."""
        name = name.strip()
        first_task_title = first_task_title.strip()
        slug = self.git.validate_slug(slug)
        if not name or not first_task_title:
            raise ValueError("プロジェクト名と最初のタスク名を入力してください")
        with self.lock:
            project_id = uuid4().hex[:12]
            task_id = uuid4().hex[:12]
            branch = f"project/{slug}-{project_id[:6]}"
            dev_path = self.git.path("dev")
            if dev_path.exists() and not self.git.clean(dev_path):
                raise ValueError("dev の未コミット変更を整理してからプロジェクトを作成してください")
            base = self.git.head("dev")
            self.git.ensure(branch, "dev")
            try:
                create_task(self.tasks_root, first_task_title, project_id=project_id, task_id=task_id)
                record = new_project(project_id, name, slug, branch, base, task_id)
                return save_project(self.projects_root, record)
            except Exception:
                shutil.rmtree(task_dir(self.tasks_root, task_id), ignore_errors=True)
                shutil.rmtree(project_dir(self.projects_root, project_id), ignore_errors=True)
                self.git.remove(branch)
                raise

    def create_root_task(self, project_id: str, title: str) -> dict:
        """Add a top-level task to project_id and return its record."""
        with self.lock:
            project = read_project(self.projects_root, project_id)
            if project.get("needs_review"):
                raise ValueError("移行前のGit変更を確認してください")
            if project["status"] == "completed":
                raise ValueError("完了したプロジェクトにはタスクを追加できません")
            if self.agent and self.agent.active_tasks:
                raise ValueError("Agentの実行が終わってからタスクを作成してください")
            return create_task(self.tasks_root, title, project_id=project_id)

    def create_child(self, parent_id: str, title: str, initial_plan: str = "", proposal_id: str | None = None) -> dict:
        """Create a child under parent_id and return its task record."""
        with self.lock:
            parent = read_task(self.tasks_root, parent_id)
            original_parent = deepcopy(parent)
            project = read_project(self.projects_root, parent["project_id"])
            if project.get("needs_review"):
                raise ValueError("移行前のGit変更を確認してください")
            if project["status"] == "completed":
                raise ValueError("完了したプロジェクトにはタスクを追加できません")
            if self.agent and self.agent.active_tasks:
                raise ValueError("Agentの実行が終わってから子タスクを作成してください")
            proposal = None
            if proposal_id:
                proposal = next((item for item in parent["child_proposals"] if item["id"] == proposal_id), None)
                if not proposal:
                    raise ValueError("分割案が見つかりません")
                if proposal["status"] == "approved":
                    return read_task(self.tasks_root, proposal["child_id"])
                if proposal["status"] != "pending" or proposal["plan_hash"] != plan_hash(parent["plan"]):
                    raise ValueError("分割案が古くなりました。計画を確認してください")
            first = not self.children(parent_id)
            branch = None
            if first:
                owner_branch = self.branch_for(parent_id)
                owner_path = self.git.ensure(owner_branch)
                if not self.git.clean(owner_path):
                    raise ValueError("現在の変更を検証して完了した後、子タスクを作成してください")
                project_owner = project.get("branch_owners", {}).get(owner_branch)
                if project_owner and project_owner != parent_id:
                    raise ValueError("別タスクがこのブランチで作業中です")
                if project_owner == parent_id:
                    self.release_code(parent_id)
                branch = task_branch_name(parent["title"], parent_id)
                self.git.ensure(branch, owner_branch)
            child_id = uuid4().hex[:12]
            try:
                child = create_task(self.tasks_root, title, parent_id, parent["project_id"], initial_plan, child_id)
                if first:
                    parent["branch"] = branch
                    if parent["status"] == "completed":
                        parent["pre_split_commit"] = parent["commit_hash"]
                        parent["status"] = "planning"
                        parent["completed_at"] = None
                    parent["updated_at"] = now()
                    save_metadata(self.tasks_root, parent)
                if proposal:
                    update_child_proposal(self.tasks_root, parent_id, proposal_id, "approved", child["id"])
                return child
            except Exception:
                shutil.rmtree(task_dir(self.tasks_root, child_id), ignore_errors=True)
                save_metadata(self.tasks_root, original_parent)
                if branch:
                    self.git.remove(branch)
                raise

    def reject_proposal(self, task_id: str, proposal_id: str) -> dict:
        """Reject one pending proposal and return the updated task."""
        with self.lock:
            task = read_task(self.tasks_root, task_id)
            item = next((value for value in task["child_proposals"] if value["id"] == proposal_id), None)
            if not item or item["status"] != "pending":
                raise ValueError("有効な分割案が見つかりません")
            return update_child_proposal(self.tasks_root, task_id, proposal_id, "rejected")

    def summary_basis(self, task_id: str) -> dict:
        """Return child completion hashes and branch HEAD for a parent task."""
        children = self.children(task_id)
        return {"children": {child["id"]: child["merge_hash"] or child["commit_hash"] for child in children}, "head": self.git.head(self.branch_for(task_id))}

    def completion_preview(self, task_id: str) -> dict:
        """Return the current completion action and Git values for task_id."""
        task = read_task(self.tasks_root, task_id)
        if read_project(self.projects_root, task["project_id"]).get("needs_review"):
            raise ValueError("移行前のGit変更を確認してください")
        if task["status"] == "completed":
            raise ValueError("タスクは完了済みです")
        children = self.children(task_id)
        reports = task["reports"]
        if not reports:
            raise ValueError("検証レポートを確認してください")
        if children:
            if any(child["status"] != "completed" for child in children):
                raise ValueError("全ての子タスクの完了が必要です")
            if reports[-1].get("kind") != "summary" or reports[-1].get("summary_basis") != self.summary_basis(task_id):
                raise ValueError("最新の子タスクを含む総括レポートが必要です")
            branch = task["branch"]
            parent_branch = self.branch_for(task["parent_id"]) if task["parent_id"] else read_project(self.projects_root, task["project_id"])["branch"]
            return {"kind": "merge", "branch": branch, "target": parent_branch, "head": self.git.head(branch), "target_head": self.git.head(parent_branch), "paths": self.git.diff_paths(parent_branch, branch), "report_number": reports[-1]["number"], "message": f"{task['title']}を統合"}
        if task["implementation"]["state"] != "succeeded":
            raise ValueError("実装と検証が完了していません")
        branch = self.branch_for(task_id)
        paths = self.git.changed(self.code_path(task_id))
        if not paths:
            project = read_project(self.projects_root, task["project_id"])
            if project.get("legacy"):
                return {"kind": "legacy", "branch": branch, "head": self.git.head(branch), "paths": [], "report_number": reports[-1]["number"], "message": "移行前の検証記録として完了"}
            raise ValueError("コミットする変更がありません")
        return {"kind": "commit", "branch": branch, "head": self.git.head(branch), "paths": paths, "report_number": reports[-1]["number"], "message": task["implementation"].get("commit_message") or f"{task['title']}を実装・検証"}

    def complete_task(self, task_id: str, expected: dict) -> dict:
        """Commit or merge an approved task and return its completed record."""
        with self.lock:
            if self.agent and self.agent.active_tasks:
                raise ValueError("Agentの実行が終わってから完了してください")
            preview = self.completion_preview(task_id)
            for key in ("kind", "head", "report_number"):
                if expected.get(key) != preview[key]:
                    raise ValueError("確認後にタスクの状態が変わりました。再確認してください")
            if preview["kind"] == "legacy":
                return mark_task_completed(self.tasks_root, task_id, None, "legacy")
            if preview["kind"] == "commit":
                commit_message = expected.get("message")
                if not isinstance(commit_message, str) or not commit_message.strip():
                    raise ValueError("コミットメッセージを入力してください")
                project_id = read_task(self.tasks_root, task_id)["project_id"]
                project = read_project(self.projects_root, project_id)
                if project.get("branch_owners", {}).get(preview["branch"]) != task_id:
                    raise ValueError("このタスクの作業ブランチが予約されていません")
                if expected.get("paths") != preview["paths"]:
                    raise ValueError("確認後に変更ファイルが変わりました")
                operation = self.begin_operation(project_id, "commit", task_id, preview["branch"])
                message = f"{commit_message.strip()}\n\nTask-Operation: {operation['id']}"
                commit = self.git.commit(preview["branch"], preview["paths"], message)
                operation.update(state="git_done", git_hash=commit)
                self.record_operation(project_id, operation)
                result = mark_task_completed(self.tasks_root, task_id, commit, "commit")
                self.release_code(task_id)
                operation["state"] = "complete"
                self.record_operation(project_id, operation)
                return result
            if expected.get("target_head") != preview["target_head"]:
                raise ValueError("マージ先が変更されました。再確認してください")
            project_id = read_task(self.tasks_root, task_id)["project_id"]
            operation = self.begin_operation(project_id, "merge", task_id, preview["branch"], preview["target"])
            message = f"{preview['message']}\n\nTask-Operation: {operation['id']}"
            commit = self.git.merge(preview["branch"], preview["target"], message)
            operation.update(state="git_done", git_hash=commit)
            self.record_operation(project_id, operation)
            result = mark_task_completed(self.tasks_root, task_id, commit, "merge")
            self.git.remove(preview["branch"], preview["target"])
            operation["state"] = "complete"
            self.record_operation(project_id, operation)
            return result

    def project_preview(self, project_id: str) -> dict:
        """Return final merge values for an approved project."""
        project = read_project(self.projects_root, project_id)
        if project.get("needs_review"):
            raise ValueError("移行前のGit変更を確認してください")
        if project["status"] == "completed":
            raise ValueError("プロジェクトは完了済みです")
        tasks = self.project_tasks(project_id)
        if not tasks or any(task["status"] != "completed" for task in tasks):
            raise ValueError("全てのタスクの完了が必要です")
        return {"branch": project["branch"], "head": self.git.head(project["branch"]), "target_head": self.git.head("dev"), "paths": self.git.diff_paths("dev", project["branch"]), "message": f"{project['name']}を統合"}

    def complete_project(self, project_id: str, expected: dict) -> dict:
        """Merge an approved project into dev and return its record."""
        with self.lock:
            if self.agent and self.agent.active_tasks:
                raise ValueError("Agentの実行が終わってから完了してください")
            preview = self.project_preview(project_id)
            if expected.get("head") != preview["head"] or expected.get("target_head") != preview["target_head"]:
                raise ValueError("確認後にブランチが変わりました。再確認してください")
            operation = self.begin_operation(project_id, "project", project_id, preview["branch"], "dev")
            message = f"{preview['message']}\n\nTask-Operation: {operation['id']}"
            commit = self.git.merge(preview["branch"], "dev", message)
            operation.update(state="git_done", git_hash=commit)
            self.record_operation(project_id, operation)
            project = read_project(self.projects_root, project_id)
            project.update(status="completed", merge_hash=commit, updated_at=now())
            save_project(self.projects_root, project)
            self.git.remove(preview["branch"], "dev")
            operation["state"] = "complete"
            self.record_operation(project_id, operation)
            return project

    def delete_task(self, task_id: str) -> list[str]:
        """Delete an uncommitted task subtree and return removed IDs."""
        with self.lock:
            ids = task_subtree(self.tasks_root, task_id)
            tasks = [read_task(self.tasks_root, item) for item in ids]
            if self.agent and self.agent.active_tasks.intersection(ids):
                raise ValueError("Agent実行中のタスクは削除できません")
            if any(task["commit_hash"] or task["merge_hash"] or task["pre_split_commit"] for task in tasks):
                raise ValueError("Git履歴があるタスクは削除できません")
            for task in tasks:
                if task["branch"]:
                    path = self.git.ensure(task["branch"])
                    if not self.git.clean(path):
                        raise ValueError("未コミットの変更があるタスクは削除できません")
            for task in tasks:
                if task["branch"]:
                    self.git.remove(task["branch"])
                if task["project_id"]:
                    self.release_code(task["id"])
            return remove_task_records(self.tasks_root, task_id)

    def delete_project(self, project_id: str) -> None:
        """Delete an uncommitted project and its records; return nothing."""
        with self.lock:
            project = read_project(self.projects_root, project_id)
            tasks = self.project_tasks(project_id)
            if any(task["commit_hash"] or task["merge_hash"] or task["pre_split_commit"] for task in tasks):
                raise ValueError("Git履歴があるプロジェクトは削除できません")
            if self.agent and self.agent.active_tasks.intersection(task["id"] for task in tasks):
                raise ValueError("Agent実行中のプロジェクトは削除できません")
            path = self.git.ensure(project["branch"])
            if not self.git.clean(path):
                raise ValueError("未コミットの変更があるプロジェクトは削除できません")
            for root_task in [task for task in tasks if task["parent_id"] is None]:
                self.delete_task(root_task["id"])
            self.git.remove(project["branch"])
            shutil.rmtree(self.projects_root / project_id)

    def initialize_legacy_branches(self, root_task_id: str) -> None:
        """Create branches for unfinished legacy parents in hierarchy order."""
        queue = [root_task_id]
        while queue:
            task_id = queue.pop(0)
            task = read_task(self.tasks_root, task_id)
            children = self.children(task_id)
            if children and task["status"] != "completed" and not task["branch"]:
                branch = task_branch_name(task["title"], task_id)
                self.git.ensure(branch, self.branch_for(task_id))
                task["branch"] = branch
                save_metadata(self.tasks_root, task)
            queue.extend(child["id"] for child in children)

    def acknowledge_migration(self, project_id: str) -> dict:
        """Accept cleaned legacy Git state and return the updated project."""
        with self.lock:
            project = read_project(self.projects_root, project_id)
            if not project.get("needs_review"):
                return project
            if not self.git.clean(self.repo):
                raise ValueError("元の作業ツリーの未コミット変更を整理してください")
            path = self.git.ensure(project["branch"])
            if not self.git.clean(path) or self.git.head(project["branch"]) != project["base_commit"]:
                raise ValueError("移行先ブランチに変更があります")
            self.git.git("reset", "--hard", "dev", cwd=path)
            project["base_commit"] = self.git.head("dev")
            project["needs_review"] = False
            project["updated_at"] = now()
            save_project(self.projects_root, project)
            self.initialize_legacy_branches(project["first_task_id"])
            return project

    def rename_legacy_branch(self, project_id: str, slug: str) -> dict:
        """Rename an unused legacy project branch from a validated slug."""
        with self.lock:
            project = read_project(self.projects_root, project_id)
            slug = self.git.validate_slug(slug)
            if not project.get("legacy") or project["status"] != "active":
                raise ValueError("移行した未完了プロジェクトだけ変更できます")
            if self.git.head(project["branch"]) != project["base_commit"] or project.get("branch_owners"):
                raise ValueError("作業開始後はブランチ名を変更できません")
            new_branch = f"project/{slug}-{project_id[:6]}"
            if new_branch == project["branch"]:
                return project
            self.git.rename(project["branch"], new_branch)
            project.update(slug=slug, branch=new_branch, updated_at=now())
            return save_project(self.projects_root, project)

    def rename_project(self, project_id: str, name: str) -> dict:
        """Update the display name of project_id and return its record."""
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
            raise ValueError("プロジェクト名は1〜120文字で入力してください")
        with self.lock:
            project = read_project(self.projects_root, project_id)
            project.update(name=name.strip(), updated_at=now())
            return save_project(self.projects_root, project)

    def migrate_legacy(self) -> None:
        """Wrap legacy root tasks in projects while preserving task IDs."""
        with self.lock:
            tasks = list_tasks(self.tasks_root)
            for root_task in [task for task in tasks if not task["parent_id"]]:
                project_id = root_task["project_id"] or root_task["id"]
                record_path = project_dir(self.projects_root, project_id) / "project.json"
                if root_task["project_id"] and record_path.exists() and not read_project(self.projects_root, project_id).get("legacy"):
                    continue
                if all(read_task(self.tasks_root, item)["project_id"] == project_id for item in task_subtree(self.tasks_root, root_task["id"])):
                    continue
                if record_path.exists():
                    project = read_project(self.projects_root, project_id)
                    branch = project["branch"]
                else:
                    slug = f"legacy-{root_task['id'][:6]}"
                    branch = f"project/legacy-{root_task['id']}"
                    project = new_project(project_id, root_task["title"], slug, branch, self.git.head("dev"), root_task["id"])
                    project["legacy"] = True
                    project["needs_review"] = not self.git.clean(self.repo)
                    save_project(self.projects_root, project)
                self.git.ensure(branch, "dev")
                queue = [root_task["id"]]
                while queue:
                    current = read_task(self.tasks_root, queue.pop())
                    current["project_id"] = project_id
                    current["legacy_completed"] = current["status"] == "completed"
                    save_metadata(self.tasks_root, current)
                    queue.extend(task["id"] for task in tasks if task["parent_id"] == current["id"])
                if not read_project(self.projects_root, project_id).get("needs_review"):
                    self.initialize_legacy_branches(root_task["id"])
