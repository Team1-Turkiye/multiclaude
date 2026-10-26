"""End-to-end tests for the multiclaude engine, using tests/fake_claude.py instead of the real CLI.

Run with:  python3 -m unittest discover -s tests -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "multiclaude.py"
FAKE = Path(__file__).resolve().parent / "fake_claude.py"


class MulticlaudeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.accounts = []
        for i in range(3):
            d = self.tmp / f"account{i}"
            d.mkdir()
            (d / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": f"user{i}@example.com"}}))
            self.accounts.append(str(d))
        self.log = self.tmp / "calls.jsonl"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args, limited=(), fail=(), check=True):
        env = {**os.environ,
               "MULTICLAUDE_CLAUDE_BIN": str(FAKE),
               "MULTICLAUDE_ACCOUNTS": ",".join(self.accounts),
               "FAKE_LOG": str(self.log),
               "FAKE_LIMITED_ACCOUNTS": ",".join(limited),
               "FAKE_FAIL_TASKS": ",".join(fail)}
        p = subprocess.run([sys.executable, str(ENGINE), *args], cwd=self.project, env=env,
                           capture_output=True, text=True, timeout=120)
        if check and p.returncode != 0:
            self.fail(f"exit {p.returncode}\nstdout:\n{p.stdout}\nstderr:\n{p.stderr}")
        return p

    def write_plan(self, tasks, name="t"):
        path = self.tmp / "plan.json"
        path.write_text(json.dumps({"name": name, "context": "test project", "tasks": tasks}))
        return str(path)

    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def state(self):
        runs = sorted((self.project / ".multiclaude").glob("2*"))
        return json.loads((runs[-1] / "state.json").read_text())

    # ------------------------------------------------------------------

    def test_runs_dag_in_dependency_order(self):
        plan = self.write_plan([
            {"id": "core", "tier": "opus", "prompt": "core"},
            {"id": "tests", "tier": "sonnet", "depends_on": ["core"], "prompt": "tests"},
            {"id": "docs", "tier": "haiku", "prompt": "docs"},
        ])
        out = self.run_cli("run", plan).stdout
        self.assertTrue(all((self.project / f"{t}.txt").exists() for t in ("core", "tests", "docs")))
        order = [c["task"] for c in self.calls() if c["event"] == "done"]
        self.assertLess(order.index("core"), order.index("tests"))
        models = {c["task"]: c["model"] for c in self.calls()}
        self.assertEqual(models, {"core": "opus", "tests": "sonnet", "docs": "haiku"})
        self.assertIn("multiclaude report", out)
        self.assertTrue(all(t["status"] == "done" for t in self.state()["tasks"].values()))

    def test_limit_moves_session_to_another_account_and_resumes(self):
        plan = self.write_plan([{"id": "solo", "tier": "sonnet", "prompt": "x"}])
        self.run_cli("run", plan, limited=[self.accounts[0]])
        calls = self.calls()
        self.assertEqual(calls[0]["event"], "limit")
        self.assertEqual(calls[0]["account"], self.accounts[0])
        self.assertEqual(calls[-1]["event"], "done")
        self.assertNotEqual(calls[-1]["account"], self.accounts[0])
        self.assertIsNotNone(calls[-1]["resume"], "second attempt should resume the same session")
        self.assertEqual(self.state()["tasks"]["solo"]["status"], "done")

    def test_all_limited_no_wait_then_resume(self):
        plan = self.write_plan([{"id": "a", "tier": "sonnet", "prompt": "x"}])
        self.run_cli("run", plan, "--no-wait", limited=self.accounts)
        self.assertEqual(self.state()["tasks"]["a"]["status"], "pending")
        self.run_cli("resume")
        self.assertEqual(self.state()["tasks"]["a"]["status"], "done")
        self.assertIsNotNone(self.calls()[-1]["resume"])

    def test_failed_task_skips_dependents(self):
        plan = self.write_plan([
            {"id": "base", "tier": "sonnet", "prompt": "x"},
            {"id": "child", "tier": "sonnet", "depends_on": ["base"], "prompt": "y"},
            {"id": "other", "tier": "sonnet", "prompt": "z"},
        ])
        self.run_cli("run", plan, fail=["base"])
        tasks = self.state()["tasks"]
        self.assertEqual(tasks["base"]["status"], "failed")
        self.assertEqual(tasks["child"]["status"], "skipped")
        self.assertEqual(tasks["other"]["status"], "done")
        self.assertEqual(len([c for c in self.calls() if c["task"] == "base"]), 2, "retried once")

    def test_rejects_invalid_plans(self):
        for tasks in ([{"id": "a", "prompt": "x"}, {"id": "a", "prompt": "y"}],
                      [{"id": "a", "depends_on": ["nope"], "prompt": "x"}],
                      [{"id": "a", "tier": "gpt", "prompt": "x"}]):
            p = self.run_cli("run", self.write_plan(tasks), check=False)
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("Plan error", p.stderr)


if __name__ == "__main__":
    unittest.main()
