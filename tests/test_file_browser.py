"""Check read-only file browsing paths, listings, content, and HTTP API."""

import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app import server
from app.file_browser import list_directory, read_content, resolve_path
from app.repositories import RepositoryRegistry


class FileBrowserTest(unittest.TestCase):
    """Exercise path resolution, listing, and content reading in a temporary root."""

    def setUp(self):
        """Create a sample tree with an outside file; return nothing."""
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.root = base / "root"
        (self.root / ".git").mkdir(parents=True)
        (self.root / ".git" / "config").write_text("secret", encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
        (self.root / "README.md").write_text("# Title\n", encoding="utf-8")
        (self.root / "b.txt").write_text("b", encoding="utf-8")
        (self.root / "node_modules").mkdir()
        (base / "outside.txt").write_text("outside", encoding="utf-8")
        os.symlink(base / "outside.txt", self.root / "escape.txt")
        os.symlink(self.root / "src" / "main.py", self.root / "inside-link.py")
        os.symlink(self.root / ".git", self.root / "git-link")

    def tearDown(self):
        """Remove the sample tree and return nothing."""
        self.temporary.cleanup()

    def test_rejects_paths_outside_root_and_git(self):
        """Reject traversal, absolute, symlinked escapes, and .git; return nothing."""
        for path in ("../outside.txt", "src/../../outside.txt", "/etc/passwd", "escape.txt", ".git/config", "src/.git", "git-link/config"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                resolve_path(self.root, path)
        self.assertEqual(resolve_path(self.root, "inside-link.py"), (self.root / "src" / "main.py").resolve())
        with self.assertRaises(FileNotFoundError):
            resolve_path(self.root, "missing.txt")

    def test_lists_directories_first_without_git_or_escapes(self):
        """List visible entries sorted with directories first; return nothing."""
        listing = list_directory(self.root, "")
        names = [entry["name"] for entry in listing["entries"]]
        self.assertEqual(names, ["node_modules", "src", "b.txt", "inside-link.py", "README.md"])
        self.assertFalse(listing["truncated"])
        child = list_directory(self.root, "src")
        self.assertEqual(child["entries"][0]["path"], "src/main.py")
        self.assertEqual(child["entries"][0]["size"], len("print('hi')\n"))
        with self.assertRaises(NotADirectoryError):
            list_directory(self.root, "b.txt")

    def test_reads_text_truncates_and_detects_binary(self):
        """Return text, truncated text, binary, and image kinds; return nothing."""
        self.assertEqual(read_content(self.root, "README.md")["text"], "# Title\n")
        (self.root / "large.txt").write_text("あ" * 100, encoding="utf-8")
        large = read_content(self.root, "large.txt", limit=100)
        self.assertTrue(large["truncated"])
        self.assertEqual(large["kind"], "text")
        self.assertTrue(large["text"].startswith("あ" * 33))
        (self.root / "data.bin").write_bytes(b"\x00\x01\x02")
        self.assertEqual(read_content(self.root, "data.bin")["kind"], "binary")
        (self.root / "image.png").write_bytes(b"\x89PNG\r\n")
        self.assertEqual(read_content(self.root, "image.png")["kind"], "image")
        with self.assertRaises(IsADirectoryError):
            read_content(self.root, "src")


class FileApiTest(unittest.TestCase):
    """Check file API routes for a registered repository and a task worktree."""

    def test_file_api_reads_repository_and_task_worktree(self):
        """Serve listings, content, and safe raw files over HTTP; return nothing."""
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "sample"
            repo.mkdir()
            for command in (["git", "init", "-q", "-b", "dev"], ["git", "config", "user.name", "Test"], ["git", "config", "user.email", "test@example.com"]):
                subprocess.run(command, cwd=repo, check=True)
            (repo / ".gitignore").write_text("tasks/\nprojects/\n", encoding="utf-8")
            (repo / "page.html").write_text("<script>alert(1)</script>", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "初期化"], cwd=repo, check=True)
            config = Path(temporary) / "repositories.json"
            config.write_text(json.dumps([{"id": "sample", "name": "Sample", "path": str(repo)}]), encoding="utf-8")
            previous = server.REGISTRY
            server.REGISTRY = RepositoryRegistry(config, None, Path(temporary) / "central-tasks", Path(temporary))
            http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            threading.Thread(target=http.serve_forever, daemon=True).start()
            base = f"http://127.0.0.1:{http.server_port}"

            def get(endpoint, **query):
                """Fetch endpoint with query and return the response object."""
                return urlopen(f"{base}/api/files{endpoint}?{urlencode({'repo': 'sample', **query})}")

            try:
                with get("") as response:
                    listing = json.load(response)
                self.assertEqual(listing["root_kind"], "repository")
                self.assertEqual(listing["root"], str(repo.resolve()))
                self.assertNotIn(".git", [entry["name"] for entry in listing["entries"]])
                with get("/raw", path="page.html") as response:
                    self.assertTrue(response.headers["Content-Type"].startswith("text/plain"))
                    self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                    self.assertIn("sandbox", response.headers["Content-Security-Policy"])
                    self.assertEqual(response.read(), b"<script>alert(1)</script>")
                with get("/raw", path="page.html", download="1") as response:
                    self.assertIn("attachment", response.headers["Content-Disposition"])
                for path, code in (("../repositories.json", 400), (".git/config", 400), ("missing.txt", 404)):
                    with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                        get("/content", path=path)
                    self.assertEqual(error.exception.code, code)
                request = Request(base + "/api/tasks?repo=sample", data=json.dumps({"title": "ファイル参照"}).encode(), headers={"Content-Type": "application/json"}, method="POST")
                with urlopen(request) as response:
                    task = json.load(response)
                worktree = server.REGISTRY.get("sample").workflow.git.path(task["branch"])
                (worktree / "task-only.txt").write_text("worktree\n", encoding="utf-8")
                with get("", task=task["id"]) as response:
                    task_listing = json.load(response)
                self.assertEqual(task_listing["root_kind"], "worktree")
                self.assertEqual(task_listing["branch"], task["branch"])
                self.assertIn("task-only.txt", [entry["name"] for entry in task_listing["entries"]])
                with get("/content", task=task["id"], path="task-only.txt") as response:
                    self.assertEqual(json.load(response)["text"], "worktree\n")
                with self.assertRaises(HTTPError) as error:
                    get("/content", path="task-only.txt")
                self.assertEqual(error.exception.code, 404)
            finally:
                http.shutdown()
                http.server_close()
                server.REGISTRY = previous


if __name__ == "__main__":
    unittest.main()
