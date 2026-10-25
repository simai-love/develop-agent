"""Manage task branches and their isolated Git worktrees."""

import os
import re
import subprocess
import tempfile
from pathlib import Path


SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def task_branch_name(title: str, task_id: str) -> str:
    """Build a readable task branch from a title and ID; return its Git ref name."""
    slug = re.sub(r"-+", "-", "".join(char.lower() if char.isalnum() else "-" for char in title)).strip("-")
    return f"task/{slug[:40].rstrip('-') or 'untitled'}-{task_id}"


class GitWorkspace:
    """Operate Git branches and worktrees for a repository."""

    def __init__(self, root: Path):
        """Store repository root and return a Git workspace manager."""
        self.root = root
        self.worktrees = root.parent / f"{root.name}-worktrees"

    def git(self, *args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
        """Run Git args in cwd and return its completed process."""
        result = subprocess.run(["git", *args], cwd=cwd or self.root, capture_output=True, text=True)
        if check and result.returncode:
            raise ValueError(result.stderr.strip() or result.stdout.strip() or "Git operation failed")
        return result

    def head(self, branch: str) -> str:
        """Resolve branch and return its current commit hash."""
        return self.git("rev-parse", branch).stdout.strip()

    def validate_slug(self, slug: str) -> str:
        """Validate a user slug and return it for branch construction."""
        if not isinstance(slug, str) or not SLUG.fullmatch(slug) or len(slug) > 60:
            raise ValueError("ブランチ名は小文字の英数字とハイフンで指定してください")
        return slug

    def path(self, branch: str) -> Path:
        """Return the checkout path for branch, including an existing main checkout."""
        lines = self.git("worktree", "list", "--porcelain").stdout.splitlines()
        current = None
        for line in lines + [""]:
            if line.startswith("worktree "):
                current = Path(line.removeprefix("worktree "))
            if line == f"branch refs/heads/{branch}" and current:
                return current
        return self.worktrees / branch.replace("/", "__")

    def ensure(self, branch: str, source: str | None = None) -> Path:
        """Create or locate branch worktree and return its path."""
        path = self.path(branch)
        if path.exists():
            if self.git("symbolic-ref", "--short", "HEAD", cwd=path).stdout.strip() != branch:
                raise ValueError("作業場所のブランチが記録と一致しません")
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = self.git("show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode == 0
        if source and not exists:
            self.git("worktree", "add", "-b", branch, str(path), source)
        else:
            self.git("worktree", "add", str(path), branch)
        return path

    def changed(self, path: Path) -> list[str]:
        """Return changed and untracked paths in a worktree."""
        tracked = subprocess.run(["git", "diff", "--name-only", "HEAD", "-z"], cwd=path, capture_output=True, check=True).stdout
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=path, capture_output=True, check=True).stdout
        return sorted({part.decode("utf-8") for part in (tracked + untracked).split(b"\0") if part})

    def untracked(self, path: Path) -> set[str]:
        """Return untracked file paths in a worktree as a set."""
        result = self.git("ls-files", "--others", "--exclude-standard", "-z", cwd=path)
        return {name for name in result.stdout.split("\0") if name}

    def diff_files(self, *revisions: str) -> list[str]:
        """Return changed paths between Git revisions, preserving unusual names."""
        result = self.git("diff", "--no-ext-diff", "--name-only", "-z", *revisions)
        return sorted(name for name in result.stdout.split("\0") if name)

    def diff_text(self, path: str, *revisions: str, cwd: Path | None = None, untracked: bool = False) -> str:
        """Return a unified patch for path under revisions or an untracked worktree file."""
        options = ("diff", "--no-ext-diff", "--no-textconv", "--no-color")
        if untracked:
            result = self.git(*options, "--no-index", "--", "/dev/null", path, cwd=cwd, check=False)
            if result.returncode not in (0, 1):
                raise ValueError(result.stderr.strip() or "差分を取得できません")
            return result.stdout
        return self.git(*options, *revisions, "--", path, cwd=cwd).stdout

    def clean(self, path: Path) -> bool:
        """Return whether path has no tracked or untracked changes."""
        return not self.changed(path)

    def worktree_tree(self, path: Path) -> str:
        """Return a Git tree for path's tracked and untracked working files."""
        with tempfile.TemporaryDirectory() as temporary:
            environment = {**os.environ, "GIT_INDEX_FILE": str(Path(temporary) / "index")}
            for args in (("read-tree", "HEAD"), ("add", "-A")):
                result = subprocess.run(["git", *args], cwd=path, env=environment, capture_output=True, text=True)
                if result.returncode:
                    raise ValueError(result.stderr.strip() or "作業内容を記録できません")
            result = subprocess.run(["git", "write-tree"], cwd=path, env=environment, capture_output=True, text=True)
            if result.returncode:
                raise ValueError(result.stderr.strip() or "作業内容を記録できません")
            return result.stdout.strip()

    def commits_since(self, branch: str, base: str) -> list[str]:
        """Return first-parent commit hashes after base on branch, oldest first."""
        self.git("merge-base", "--is-ancestor", base, branch)
        return self.git("rev-list", "--reverse", "--first-parent", f"{base}..{branch}").stdout.splitlines()

    def worktree_diff_files(self, path: Path, base: str) -> list[str]:
        """Return tracked and untracked paths differing from base in path."""
        tracked = subprocess.run(["git", "diff", "--name-only", "-z", base], cwd=path, capture_output=True, check=True).stdout
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=path, capture_output=True, check=True).stdout
        return sorted({part.decode("utf-8") for part in (tracked + untracked).split(b"\0") if part})

    def diff_paths(self, source: str, target: str) -> list[str]:
        """Return paths differing between source and target refs."""
        output = subprocess.run(["git", "diff", "--name-only", "-z", f"{source}...{target}"], cwd=self.root, capture_output=True, check=True).stdout
        return [item.decode("utf-8") for item in output.split(b"\0") if item]

    def commit(self, branch: str, paths: list[str], message: str) -> str:
        """Commit only paths on branch with message and return commit hash."""
        path = self.ensure(branch)
        current = self.changed(path)
        if not paths or sorted(paths) != current:
            raise ValueError("確認時からコミット対象の差分が変わりました")
        self.git("add", "--", *paths, cwd=path)
        self.git("commit", "-m", message, cwd=path)
        return self.head(branch)

    def merge(self, source: str, target: str, message: str) -> str:
        """Merge source into target without fast forward and return merge hash."""
        source_path = self.ensure(source)
        target_path = self.ensure(target)
        if not self.clean(source_path) or not self.clean(target_path):
            raise ValueError("マージ前に作業ツリーの変更を整理してください")
        try:
            self.git("merge", "--no-ff", "-m", message, source, cwd=target_path)
        except ValueError:
            self.git("merge", "--abort", cwd=target_path, check=False)
            raise
        return self.head(target)

    def has_branch(self, branch: str) -> bool:
        """Return whether a local branch ref exists."""
        return self.git("show-ref", "--verify", f"refs/heads/{branch}", check=False).returncode == 0

    def abort_merge(self, branch: str, source: str) -> bool:
        """Abort an unfinished merge of source in branch's checkout; return whether one was aborted."""
        path = self.path(branch)
        if not path.exists() or not self.has_branch(source):
            return False
        merging = self.git("rev-parse", "-q", "--verify", "MERGE_HEAD", cwd=path, check=False).stdout.strip()
        if merging != self.head(source):
            return False
        self.git("merge", "--abort", cwd=path)
        return True

    def merge_tree(self, target: str, source: str) -> str | None:
        """Return the tree Git would create by merging source into target, or None on conflicts."""
        result = self.git("merge-tree", "--write-tree", target, source, check=False)
        if result.returncode not in (0, 1):
            raise ValueError(result.stderr.strip() or "マージ結果を計算できません")
        return result.stdout.split()[0] if result.returncode == 0 else None

    def remove(self, branch: str, merged_into: str | None = None) -> None:
        """Remove branch after optional target ancestry check; return nothing."""
        if merged_into:
            self.git("merge-base", "--is-ancestor", branch, merged_into)
        path = self.path(branch)
        if path.exists() and path != self.root:
            self.git("worktree", "remove", str(path))
        self.git("worktree", "prune")
        self.git("branch", "-D", branch)

    def rename(self, old_branch: str, new_branch: str) -> None:
        """Rename an existing branch ref and its checkout without moving files."""
        if not self.clean(self.ensure(old_branch)):
            raise ValueError("ブランチの変更を整理してください")
        self.git("branch", "-m", old_branch, new_branch)
