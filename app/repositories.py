"""Load locally registered Git repositories for the shared task GUI."""

import json
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from app.agent import AgentManager
from app.task_workflow import TaskWorkflowService


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

    def __init__(self, config_path: Path, cli_path: str | None):
        """Store a config path and shared CLI path; return an empty registry."""
        self.config_path = config_path
        self.cli_path = cli_path
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

    def get(self, repository_id: str) -> RepositoryContext:
        """Resolve repository_id and return its initialized task services."""
        with self.lock:
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
            tasks_root = root / "tasks"
            projects_root = root / "projects"
            workflow = TaskWorkflowService(root, tasks_root, projects_root)
            sandbox = os.environ.get("DEVELOP_AGENT_CODEX_SANDBOX", "danger-full-access" if entry.get("gpu") and Path("/dev/dxg").exists() else "workspace-write")
            process_env = self._gpu_environment() if entry.get("gpu") and Path("/dev/dxg").exists() else None
            agent = AgentManager(tasks_root, root, cli_path=self.cli_path, workflow=workflow, codex_sandbox=sandbox, process_env=process_env)
            workflow.agent = agent
            context = RepositoryContext(entry["name"], root, tasks_root, workflow, agent)
            self.contexts[repository_id] = context
            return context

    @staticmethod
    def _gpu_environment() -> dict[str, str]:
        """Read the WSL GPU tools and return suitable child process variables."""
        environment = os.environ.copy()
        sdk = Path("/opt/nvidia/hpc_sdk/Linux_x86_64/26.9")
        if os.access(sdk / "compilers/bin/nvfortran", os.X_OK):
            environment["PATH"] = f"{sdk / 'compilers/bin'}:{environment.get('PATH', '')}"
            environment["LD_LIBRARY_PATH"] = f"{sdk / 'cuda/13.3/targets/x86_64-linux/lib'}:/usr/lib/wsl/lib:{environment.get('LD_LIBRARY_PATH', '')}"
        return environment
