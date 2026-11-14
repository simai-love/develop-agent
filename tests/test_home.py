"""Check home data, plan approval, memos, and the scheduled night and morning jobs."""

import json
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, time, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import server
from app.agent import AgentManager
from app.daily_report import changed_on, report_day, run_daily_report
from app.home import HomeProject, project_home, save_agent_settings
from app.memos import create_memo, delete_memo, list_memos, update_memo
from app.nightly import approved_targets, memo_targets, run_nightly
from app.plan_approval import approve_plan, record_auto_run, revoke_plan_approval
from app.record_index import JAPAN_TIME
from app.repositories import RepositoryRegistry
from app.scheduler import Scheduler, due_jobs, initial_state, latest_slot, read_state
from app.storage import append_chat, create_task, list_tasks, mark_task_completed, read_task, save_metadata, set_readiness, task_dir, update_task
from app.task_status import display_status
from app.task_workflow import TaskWorkflowService


def jst(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Return an aware Japan-time datetime from calendar fields."""
    return datetime(year, month, day, hour, minute, tzinfo=JAPAN_TIME)


def ready_task(root: Path, title: str, plan: str = "# 計画\n実装する") -> dict:
    """Create a task under root whose plan the Agent judged ready; return it."""
    task = create_task(root, title)
    update_task(root, task["id"], {"plan": plan})
    return set_readiness(root, task["id"], True, plan, "実装可能")


class FakeAgent:
    """Record started runs and report each as running once, then finished."""

    def __init__(self, root: Path, plan_ready: bool = True):
        """Store the task root and whether plan chats become ready; return an idle fake."""
        self.root = root
        self.plan_ready = plan_ready
        self.lock = threading.RLock()
        self.active_tasks: set[str] = set()
        self.calls: list[tuple] = []
        self.polls: dict[str, int] = {}

    def _run(self, kind: str, task_id: str, detail: str) -> dict:
        """Record a run of kind for task_id and return its record."""
        self.calls.append((kind, task_id, detail))
        run_id = f"run-{len(self.calls)}"
        self.polls[run_id] = 0
        return {"id": run_id, "state": "running"}

    def start_implementation(self, task_id: str, additional_instruction: str = "") -> dict:
        """Record an implementation run and return it."""
        return self._run("implement", task_id, additional_instruction)

    def start_chat(self, task_id: str, phase: str, message: str, images: object = None) -> dict:
        """Simulate a plan or result chat for task_id and return its run."""
        if phase == "plan":
            plan = "# 計画\nメモから作成"
            update_task(self.root, task_id, {"plan": plan})
            if self.plan_ready:
                set_readiness(self.root, task_id, True, plan, "実装可能")
            else:
                append_chat(self.root, task_id, "plan", "assistant", "質問があります", questions=[{"prompt": "対象は？", "options": ["A", "B"]}])
        return self._run(phase, task_id, message)

    def get_run(self, run_id: str) -> dict:
        """Return running on the first poll and succeeded afterwards."""
        self.polls[run_id] += 1
        return {"id": run_id, "state": "running" if self.polls[run_id] == 1 else "succeeded", "message": "完了"}


class FakeWorkflow:
    """Create root tasks without Git for night job tests."""

    def __init__(self, root: Path):
        """Store the task root and return the fake service."""
        self.root = root

    def create_root_task(self, title: str) -> dict:
        """Create and return a root task record with title."""
        return create_task(self.root, title)

    def children(self, task_id: str) -> list[dict]:
        """Return no children for any task_id."""
        return []


class PlanApprovalTest(unittest.TestCase):
    """Check approval storage, invalidation, and the displayed task stages."""

    def test_approval_requires_ready_plan_and_follows_its_hash(self):
        """Approve only a ready plan and drop approval when the plan changes."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            draft = create_task(root, "下書き")
            with self.assertRaises(ValueError):
                approve_plan(root, draft["id"])
            task = ready_task(root, "承認")
            self.assertEqual(display_status(task, [], False)["label"], "承認待ち")
            approved = approve_plan(root, task["id"], "2026-11-14T10:00:00+09:00")
            self.assertTrue(approved["plan_approved"])
            self.assertEqual(approved["plan_approval"]["approved_at"], "2026-11-14T10:00:00+09:00")
            self.assertEqual(display_status(approved, [], False)["label"], "承認済み")
            self.assertNotIn("plan_approved", json.loads((task_dir(root, task["id"]) / "task.json").read_text(encoding="utf-8")))
            changed = update_task(root, task["id"], {"plan": "# 計画\n別の方針"})
            self.assertFalse(changed["plan_approved"])
            self.assertIsNotNone(changed["plan_approval"])
            self.assertEqual(display_status(changed, [], False)["label"], "計画中")
            again = approve_plan(root, ready_task(root, "取り消し")["id"])
            self.assertFalse(revoke_plan_approval(root, again["id"])["plan_approved"])

    def test_display_status_covers_each_stage(self):
        """Map planning, questions, verifying, review, and completion to their labels."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = create_task(root, "段階")
            self.assertEqual(display_status(task, [], False)["label"], "未着手")
            self.assertEqual(display_status(task, [{"role": "user", "content": "相談"}], False)["label"], "計画中")
            self.assertEqual(display_status(task, [{"role": "assistant", "questions": [{"prompt": "?"}]}], False)["label"], "質問あり")
            self.assertEqual(display_status(task, [], True)["label"], "実行中")
            self.assertEqual(display_status({**task, "status": "verifying"}, [], False)["label"], "検証中")
            self.assertEqual(display_status({**task, "commit_pending": True}, [], False)["label"], "確認待ち")
            self.assertEqual(display_status({**task, "status": "completed"}, [], False)["label"], "完了")


class SchedulerTimingTest(unittest.TestCase):
    """Check scheduled slots, missed-run catch up, and first start handling."""

    def test_latest_slot_and_due_jobs(self):
        """Return the latest 0:00 and 6:00 slots and only jobs newer than the state."""
        at = jst(2026, 11, 14, 5, 30)
        self.assertEqual(latest_slot(at, 0), jst(2026, 11, 14))
        self.assertEqual(latest_slot(at, 6), jst(2026, 11, 13, 6))
        self.assertEqual(latest_slot(at.astimezone(timezone.utc), 0), jst(2026, 11, 14))
        state = initial_state(at)
        self.assertEqual(due_jobs(state, at), [])
        self.assertEqual(due_jobs(state, jst(2026, 11, 14, 6)), [("daily_report", jst(2026, 11, 14, 6))])
        stale = {"nightly": jst(2026, 11, 10).isoformat(), "daily_report": jst(2026, 11, 10, 6).isoformat()}
        self.assertEqual(due_jobs(stale, jst(2026, 11, 14, 7)), [("nightly", jst(2026, 11, 14)), ("daily_report", jst(2026, 11, 14, 6))])

    def test_run_due_initializes_then_catches_up_once(self):
        """Skip slots before the first start, then run each missed job once and save it."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = HomeProject("sample", "Sample", root, root, None, None)
            clock = [jst(2026, 11, 10, 12)]
            calls = []
            scheduler = Scheduler(lambda: [project], clock=lambda: clock[0], poll=0)
            scheduler.jobs = {"nightly": lambda project, slot, poll: calls.append(("nightly", slot)), "daily_report": lambda project, slot, poll: calls.append(("daily_report", slot))}
            self.assertEqual(scheduler.run_due(project), [])
            self.assertEqual(read_state(root)["nightly"], jst(2026, 11, 10).isoformat())
            clock[0] = jst(2026, 11, 14, 9)
            self.assertEqual(scheduler.run_due(project), ["nightly", "daily_report"])
            self.assertEqual(calls, [("nightly", jst(2026, 11, 14)), ("daily_report", jst(2026, 11, 14, 6))])
            self.assertEqual(scheduler.run_due(project), [])
            scheduler.jobs["nightly"] = lambda project, slot, poll: 1 / 0
            clock[0] = jst(2026, 11, 15, 1)
            self.assertEqual(scheduler.run_due(project), ["nightly"])
            self.assertEqual(read_state(root)["nightly"], jst(2026, 11, 15).isoformat())


class NightlyTest(unittest.TestCase):
    """Check night target selection, sequential runs, and their record."""

    def test_targets_exclude_unapproved_done_active_and_already_run(self):
        """Select only approved, idle, unfinished plans not yet run automatically."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = approve_plan(root, ready_task(root, "対象")["id"])
            ready_task(root, "未承認")
            active = approve_plan(root, ready_task(root, "実行中")["id"])
            done = approve_plan(root, ready_task(root, "完了")["id"])
            mark_task_completed(root, done["id"], None, "legacy")
            ran = approve_plan(root, ready_task(root, "実行済み")["id"])
            record_auto_run(root, ran["id"], ran["plan_approval"]["plan_hash"])
            tasks = [read_task(root, task["id"]) for task in list_tasks(root)]
            self.assertEqual([task["id"] for task in approved_targets(tasks, {active["id"]})], [target["id"]])
            memos = [{"allow_agent": True, "task_id": None}, {"allow_agent": False, "task_id": None}, {"allow_agent": True, "task_id": "abc"}]
            self.assertEqual(memo_targets(memos), memos[:1])

    def test_night_run_implements_approved_tasks_and_memos_in_order(self):
        """Run approved tasks, then plan, approve, and implement permitted memos; record them."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = approve_plan(root, ready_task(root, "承認済み")["id"])
            memo = create_memo(root, "メモの依頼", "本文", True)
            create_memo(root, "許可なし", "本文", False)
            save_agent_settings(root, "claude", "sonnet")
            agent = FakeAgent(root)
            project = HomeProject("sample", "Sample", root, root, FakeWorkflow(root), agent)
            entries = run_nightly(project, jst(2026, 11, 14), poll=0)
            memo_task_id = list_memos(root)[0]["task_id"]
            self.assertEqual([call[:2] for call in agent.calls], [("implement", task["id"]), ("plan", memo_task_id), ("implement", memo_task_id)])
            self.assertIn("本文", agent.calls[1][2])
            self.assertEqual([entry["state"] for entry in entries], ["succeeded", "succeeded", "succeeded"])
            created = read_task(root, memo_task_id)
            self.assertEqual((created["provider"], created["model"]), ("claude", "sonnet"))
            self.assertEqual(created["plan_approval"]["source"], "memo")
            self.assertEqual(created["auto_run_plan_hash"], created["plan_approval"]["plan_hash"])
            self.assertEqual(read_task(root, task["id"])["auto_run_plan_hash"], task["plan_approval"]["plan_hash"])
            record = json.loads((root / ".home" / "nightly" / "2026-11-14.json").read_text(encoding="utf-8"))
            self.assertEqual(len(record["entries"]), 3)
            self.assertEqual(run_nightly(project, jst(2026, 11, 15), poll=0), [])
            self.assertFalse((root / ".home" / "nightly" / "2026-11-15.json").exists())
            self.assertEqual(memo["id"], list_memos(root)[0]["id"])

    def test_memo_with_questions_stops_at_planning(self):
        """Leave a memo task in planning when the Agent still asks questions."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create_memo(root, "曖昧なメモ", "", True)
            agent = FakeAgent(root, plan_ready=False)
            entries = run_nightly(HomeProject("sample", "Sample", root, root, FakeWorkflow(root), agent), jst(2026, 11, 14), poll=0)
            self.assertEqual([call[0] for call in agent.calls], ["plan"])
            self.assertEqual(entries[0]["state"], "stopped")
            self.assertEqual(entries[0]["questions"], ["対象は？"])
            task = read_task(root, list_memos(root)[0]["task_id"])
            self.assertIsNone(task["plan_approval"])
            self.assertEqual(display_status(task, [{"role": "assistant", "questions": [{"prompt": "対象は？"}]}], False)["label"], "質問あり")

    def test_implemented_task_runs_an_additional_experiment(self):
        """Use the result chat for an approved plan that already has an implementation."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = approve_plan(root, ready_task(root, "検証中")["id"])
            record = read_task(root, task["id"])
            record.update(status="verifying", implementation={"state": "succeeded", "changed_files": ["a.py"]})
            save_metadata(root, record)
            agent = FakeAgent(root)
            entries = run_nightly(HomeProject("sample", "Sample", root, root, FakeWorkflow(root), agent), jst(2026, 11, 14), poll=0)
            self.assertEqual(agent.calls[0][:2], ("result", task["id"]))
            self.assertEqual(entries[0]["run"], "experiment")


class DailyReportTest(unittest.TestCase):
    """Check report targets and the read-only report CLI call."""

    def test_report_targets_previous_day_only(self):
        """Select tasks updated within the previous Japan-time day."""
        slot = jst(2026, 11, 14, 6)
        self.assertEqual(str(report_day(slot)), "2026-11-13")
        tasks = [{"updated_at": jst(2026, 11, 13, 0).isoformat()}, {"updated_at": jst(2026, 11, 13, 23, 59).isoformat()}, {"updated_at": jst(2026, 11, 14).isoformat()}, {"updated_at": "2026-11-12T14:59:00+00:00"}]
        self.assertEqual(changed_on(tasks, report_day(slot)), tasks[:2])

    def test_report_is_written_only_for_changed_projects(self):
        """Skip a quiet project and save Agent HTML without scripts for an active one."""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "tasks"
            root.mkdir()
            log = base / "calls.log"
            cli = base / "fake-codex"
            cli.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "args = sys.argv[1:]\n"
                f"open({str(log)!r}, 'a').write(json.dumps(args) + '\\n')\n"
                "prompt = sys.stdin.read()\n"
                "out = args[args.index('--output-last-message') + 1]\n"
                "html = '<!doctype html><h1>日報</h1><script>alert(1)</script><svg></svg>'\n"
                "json.dump({'summary': '要約', 'html': html + ('対象あり' if '日報テスト' in prompt else '')}, open(out, 'w'))\n",
                encoding="utf-8",
            )
            cli.chmod(0o755)
            project = HomeProject("sample", "Sample", base, root, None, AgentManager(root, base, cli_path=str(cli)))
            today = datetime.now(JAPAN_TIME).date()
            quiet_slot = datetime.combine(today + timedelta(days=3), time(6), JAPAN_TIME)
            self.assertIsNone(run_daily_report(project, quiet_slot))
            self.assertFalse(log.exists())
            create_task(root, "日報テスト")
            slot = datetime.combine(today + timedelta(days=1), time(6), JAPAN_TIME)
            path = run_daily_report(project, slot)
            html = path.read_text(encoding="utf-8")
            self.assertEqual(path.name, f"{today:%Y-%m-%d}.html")
            self.assertIn("対象あり", html)
            self.assertNotIn("<script", html)
            args = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(args[args.index("--sandbox") + 1], "read-only")
            self.assertEqual(args[args.index("--model") + 1], "gpt-6-sol")
            night = root / ".home" / "nightly" / f"{today + timedelta(days=4):%Y-%m-%d}.json"
            night.parent.mkdir(parents=True, exist_ok=True)
            night.write_text(json.dumps({"date": night.stem, "entries": []}), encoding="utf-8")
            self.assertIsNotNone(run_daily_report(project, datetime.combine(today + timedelta(days=4), time(6), JAPAN_TIME)))


class MemoTest(unittest.TestCase):
    """Check memo validation and storage below .home."""

    def test_memo_crud_is_ignored_by_task_listing(self):
        """Create, edit, and delete memos while task listing ignores .home."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            memo = create_memo(root, " タイトル ", "本文")
            self.assertEqual(memo["title"], "タイトル")
            self.assertFalse(memo["allow_agent"])
            self.assertTrue(update_memo(root, memo["id"], {"allow_agent": True})["allow_agent"])
            with self.assertRaises(ValueError):
                update_memo(root, memo["id"], {"task_id": "abc"})
            with self.assertRaises(ValueError):
                create_memo(root, " ")
            self.assertEqual(list_tasks(root), [])
            delete_memo(root, memo["id"])
            self.assertEqual(list_memos(root), [])


class HomeApiTest(unittest.TestCase):
    """Check the home, memo, Agent setting, report, and approval routes."""

    def test_home_routes(self):
        """Serve home data and change memos, Agent settings, and approval through HTTP."""
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "dev"], cwd=repo, check=True)
            subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-q", "--allow-empty", "-m", "初期化"], cwd=repo, check=True)
            tasks_root = Path(temporary) / "tasks"
            workflow = TaskWorkflowService(repo, tasks_root, repo / "projects")
            agent = AgentManager(tasks_root, repo, workflow=workflow)
            workflow.agent = agent
            old = (server.PROJECT_ROOT, server.TASKS_ROOT, server.WORKFLOW, server.AGENT, server.REGISTRY)
            server.PROJECT_ROOT, server.TASKS_ROOT, server.WORKFLOW, server.AGENT = repo, tasks_root, workflow, agent
            server.REGISTRY = RepositoryRegistry(Path(temporary) / "none.json", None, tasks_root, Path(temporary) / "empty")
            http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            thread = threading.Thread(target=http.serve_forever, daemon=True)
            thread.start()

            def call(method: str, path: str, body: object = None) -> object:
                """Send one JSON request and return the decoded response."""
                data = json.dumps(body).encode() if body is not None else None
                with urlopen(Request(f"http://127.0.0.1:{http.server_port}{path}", data=data, headers={"Content-Type": "application/json"}, method=method)) as response:
                    return json.load(response)

            try:
                task = ready_task(tasks_root, "API承認")
                memo = call("POST", "/api/home/memos", {"title": "メモ", "body": "本文", "allow_agent": True})
                self.assertEqual(call("PATCH", f"/api/home/memos/{memo['id']}", {"title": "変更"})["title"], "変更")
                self.assertEqual(call("PATCH", "/api/home/agent", {"provider": "claude", "model": "opus"}), {"provider": "claude", "model": "opus"})
                approved = call("POST", f"/api/tasks/{task['id']}/plan-approval")
                self.assertTrue(approved["plan_approved"])
                self.assertEqual(approved["display_status"]["label"], "承認済み")
                home = call("GET", "/api/home")
                project = home["projects"][0]
                self.assertEqual(project["id"], "develop-agent")
                self.assertEqual(project["tasks"][0]["display_status"]["label"], "承認済み")
                self.assertTrue(project["tasks"][0]["plan_approved"])
                self.assertEqual(project["memos"][0]["title"], "変更")
                self.assertEqual(project["agent"]["provider"], "claude")
                self.assertEqual(project, project_home(HomeProject("develop-agent", "Develop Agent", repo, tasks_root, workflow, agent)))
                reports = tasks_root / ".home" / "daily-reports"
                reports.mkdir(parents=True)
                (reports / "2026-11-13.html").write_text("<h1>日報</h1>", encoding="utf-8")
                self.assertEqual(call("GET", "/api/home")["projects"][0]["reports"], ["2026-11-13"])
                self.assertEqual(call("GET", "/api/home/reports/2026-11-13")["html"], "<h1>日報</h1>")
                with self.assertRaises(HTTPError) as error:
                    call("GET", "/api/home/reports/..%2Fsecret")
                self.assertEqual(error.exception.code, 404)
                self.assertFalse(call("DELETE", f"/api/tasks/{task['id']}/plan-approval")["plan_approved"])
                self.assertEqual(call("DELETE", f"/api/home/memos/{memo['id']}"), {"deleted_id": memo["id"]})
                with urlopen(f"http://127.0.0.1:{http.server_port}/home.mjs") as response:
                    self.assertIn("showHome", response.read().decode("utf-8"))
            finally:
                http.shutdown()
                http.server_close()
                thread.join(timeout=2)
                server.PROJECT_ROOT, server.TASKS_ROOT, server.WORKFLOW, server.AGENT, server.REGISTRY = old


if __name__ == "__main__":
    unittest.main()
