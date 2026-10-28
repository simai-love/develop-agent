"""Load locally registered Git repositories for the shared task GUI."""

import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.agent import AgentManager
from app.record_index import sync_record_links
from app.storage import write_text
from app.task_workflow import TaskWorkflowService


REPOSITORY_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


@dataclass
class RepositoryContext:
    """Store supplied repository paths and services for task API requests."""

    name: str
    root: Path
    tasks_root: Path
    workflow: TaskWorkflowService
    agent: AgentManager


class RepositoryRegistry:
    """Create a registry from local settings and return repository services."""

    def __init__(self, config_path: Path, cli_path: str | None, tasks_root: Path, discovery_root: Path | None = None):
        """Store config, CLI, task, and discovery paths; return an empty registry."""
        self.config_path = config_path
        self.cli_path = cli_path
        self.tasks_root = tasks_root
        self.discovery_root = discovery_root or Path.home() / "prog"
        self.contexts: dict[str, RepositoryContext] = {}
        self.lock = threading.RLock()

    def entries(self) -> list[dict]:
        """Read the configured paths, names, and IDs; return their entries."""
        if not self.config_path.exists():
            return []
        entries = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise ValueError("リポジトリ設定は配列で指定してください")
        return entries

    def candidates(self) -> list[dict]:
        """Return unregistered Git roots with dev under the discovery path."""
        root = self.discovery_root.expanduser().resolve()
        if not root.is_dir():
            return []
        registered = {Path(item["path"]).expanduser().resolve() for item in self.entries()}
        registered.add(self.tasks_root.parent.resolve())
        candidates = []
        for directory in sorted(root.iterdir(), key=lambda item: item.name.lower()):
            if not directory.is_dir() or directory.resolve() in registered:
                continue
            git_root = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=directory, capture_output=True, text=True)
            if git_root.returncode or Path(git_root.stdout.strip()).resolve() != directory.resolve():
                continue
            dev = subprocess.run(["git", "show-ref", "--verify", "refs/heads/dev"], cwd=directory, capture_output=True)
            if not dev.returncode:
                candidates.append({"name": directory.name.replace("_", " ").replace("-", " ").title(), "path": str(directory.resolve())})
        return candidates

    def get(self, repository_id: str) -> RepositoryContext:
        """Resolve repository_id and return its initialized task services."""
        with self.lock:
            if not REPOSITORY_ID.fullmatch(repository_id) or repository_id == "develop-agent":
                raise ValueError("無効なリポジトリ ID です")
            entry = next((item for item in self.entries() if item.get("id") == repository_id), None)
            if entry is None:
                raise ValueError("登録されていないリポジトリです")
            root = Path(entry["path"]).expanduser().resolve()
            if repository_id in self.contexts:
                context = self.contexts[repository_id]
                if context.root == root:
                    return context
                raise ValueError("リポジトリの場所が変更されました。サーバーを再起動してください")
            result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root, capture_output=True, text=True)
            if result.returncode or Path(result.stdout.strip()).resolve() != root:
                raise ValueError("登録先は Git リポジトリのルートを指定してください")
            tasks_root = self.tasks_root / repository_id
            projects_root = root / "projects"
            sync_record_links(tasks_root, "task.json", "title")
            sync_record_links(projects_root, "project.json", "name")
            workflow = TaskWorkflowService(root, tasks_root, projects_root)
            sandbox = os.environ.get("DEVELOP_AGENT_CODEX_SANDBOX", "danger-full-access" if entry.get("gpu") and Path("/dev/dxg").exists() else "workspace-write")
            process_env = self._gpu_environment() if entry.get("gpu") and Path("/dev/dxg").exists() else None
            agent = AgentManager(tasks_root, root, cli_path=self.cli_path, workflow=workflow, codex_sandbox=sandbox, process_env=process_env)
            workflow.agent = agent
            context = RepositoryContext(entry["name"], root, tasks_root, workflow, agent)
            self.contexts[repository_id] = context
            return context

    def add(self, name: str, path: str, gpu: bool = False) -> dict:
        """Register a Git root from name, path, and GPU flag; return its entry."""
        if not isinstance(name, str) or not name.strip() or not isinstance(path, str) or not path.strip() or not isinstance(gpu, bool):
            raise ValueError("名前とリポジトリの場所を指定してください")
        root = Path(path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("リポジトリの場所が見つかりません")
        if root == self.tasks_root.parent.resolve():
            raise ValueError("このアプリのリポジトリは登録済みです")
        result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root, capture_output=True, text=True)
        if result.returncode or Path(result.stdout.strip()).resolve() != root:
            raise ValueError("Git リポジトリのルートを指定してください")
        dev = subprocess.run(["git", "show-ref", "--verify", "refs/heads/dev"], cwd=root, capture_output=True)
        if dev.returncode:
            raise ValueError("リポジトリに dev ブランチが必要です")
        with self.lock:
            entries = self.entries()
            if any(Path(item["path"]).expanduser().resolve() == root for item in entries):
                raise ValueError("このリポジトリは登録済みです")
            slug = re.sub(r"[^a-z0-9_-]+", "-", root.name.lower()).strip("-_")[:40] or "repo"
            if not slug[0].isalpha():
                slug = f"repo-{slug}"
            repository_id = slug if slug != "develop-agent" and all(item["id"] != slug for item in entries) else f"repo-{uuid4().hex[:8]}"
            entry = {"id": repository_id, "name": name.strip(), "path": str(root), "gpu": gpu}
            write_text(self.config_path, json.dumps([*entries, entry], ensure_ascii=False, indent=2) + "\n")
            return entry

    @staticmethod
    def _gpu_environment() -> dict[str, str]:
        """Read the WSL GPU tools and return suitable child process variables."""
        environment = os.environ.copy()
        environment["PATH"] = f"/usr/lib/wsl/lib:{environment.get('PATH', '')}"
        sdk = Path("/opt/nvidia/hpc_sdk/Linux_x86_64/26.9")
        if os.access(sdk / "compilers/bin/nvfortran", os.X_OK):
            environment["PATH"] = f"{sdk / 'compilers/bin'}:{environment.get('PATH', '')}"
            environment["LD_LIBRARY_PATH"] = f"{sdk / 'cuda/13.3/targets/x86_64-linux/lib'}:/usr/lib/wsl/lib:{environment.get('LD_LIBRARY_PATH', '')}"
        return environment
