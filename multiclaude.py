#!/usr/bin/env python3
"""multiclaude: run a project plan (a task DAG) in parallel across multiple
Claude Code accounts. Each task runs on its own model tier (opus/sonnet/haiku).
If an account hits its usage limit, the task's session is moved to another
account and continues FROM THE SAME POINT.

Commands:
  multiclaude run plan.json        run a plan
  multiclaude resume [run_dir]     continue the latest (or given) unfinished run
  multiclaude status [run_dir]     print the run report
  multiclaude accounts             list discovered accounts

Plan format (JSON):
{
  "name": "short-name",
  "context": "Shared project context given to every task (keep it short)",
  "tasks": [
    {"id": "db", "title": "...", "tier": "opus|sonnet|haiku", "effort": "high (optional)",
     "depends_on": [], "files": ["src/db/**"], "prompt": "Clear, self-contained instructions",
     "use_skills": false}
  ]
}

Accounts are separate CLAUDE_CONFIG_DIRs (default: ~/.claude and ~/.claude_*),
override with MULTICLAUDE_ACCOUNTS=dir1,dir2,...
"""
from __future__ import annotations

import argparse
import fcntl
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

CLAUDE_BIN = os.environ.get("MULTICLAUDE_CLAUDE_BIN", "claude")
MODELS = {"opus": "opus", "sonnet": "sonnet", "haiku": "haiku"}
LIMIT_RE = re.compile(r"usage limit|rate limit|limit reached|hit your limit|quota|"
                      r"requires usage credits|out of (extra )?usage|429|overloaded", re.I)
LIMIT_COOLDOWN = int(os.environ.get("MULTICLAUDE_COOLDOWN", 15 * 60))  # a limited account is retried after this long
WAIT_POLL = int(os.environ.get("MULTICLAUDE_WAIT_POLL", 5 * 60))  # poll interval when every account is limited

WORKER_RULES = """You are one worker in a parallel team of AI engineers sharing ONE working tree.
Other workers are editing other files at the same time.
- Only create/modify files inside your assigned scope. If you truly must touch something outside it, keep it minimal and mention it in `notes`.
- Never run git commit/reset/checkout/stash/clean/rebase or anything that rewrites the shared tree. Never delete files you did not create.
- Do not start long-running servers/watchers. Run quick, relevant checks (typecheck/tests/build for your part) when cheap.
- Be token-efficient: don't re-read files needlessly, no long explanations, no unrequested extras.
- If a decision is genuinely ambiguous, pick the most sensible default and record it in `notes`.
- Your final answer is a structured report for the orchestrator: concise and factual."""

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "partial", "failed"]},
        "summary": {"type": "string", "description": "What was done, max 4 sentences"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "verification": {"type": "string", "description": "What you ran/checked and the result"},
        "notes": {"type": "string", "description": "Decisions made, risks, follow-ups for the orchestrator. Empty if none."},
    },
    "required": ["status", "summary", "files_changed", "verification", "notes"],
}

lock = threading.RLock()


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------- accounts

def discover_accounts() -> list[dict]:
    env = os.environ.get("MULTICLAUDE_ACCOUNTS")
    dirs = ([os.path.expanduser(d) for d in env.split(",")] if env
            else sorted(glob.glob(os.path.expanduser("~/.claude"))
                        + glob.glob(os.path.expanduser("~/.claude_*"))))
    accounts = []
    for d in dirs:
        cfg = Path(d) / ".claude.json"
        if not Path(d).is_dir() or not cfg.exists():
            continue
        try:
            email = json.loads(cfg.read_text()).get("oauthAccount", {}).get("emailAddress")
        except Exception:
            email = None
        if email:
            accounts.append({"dir": d, "email": email, "running": 0, "started": 0, "limited_until": 0})
    if not accounts:
        sys.exit("No logged-in accounts found (~/.claude, ~/.claude_* or MULTICLAUDE_ACCOUNTS).")
    return accounts


def session_file(account_dir: str, cwd: str, sid: str) -> Path | None:
    slug = re.sub(r"[^a-zA-Z0-9]", "-", os.path.realpath(cwd))
    p = Path(account_dir) / "projects" / slug / f"{sid}.jsonl"
    if p.exists():
        return p
    hits = glob.glob(str(Path(account_dir) / "projects" / "*" / f"{sid}.jsonl"))
    return Path(hits[0]) if hits else None


def migrate_session(sid: str, src_dir: str, dst_dir: str, cwd: str) -> bool:
    """Copy a session transcript from one account to another (cross-account resume)."""
    src = session_file(src_dir, cwd, sid)
    if not src:
        return False
    slug = re.sub(r"[^a-zA-Z0-9]", "-", os.path.realpath(cwd))
    dst = Path(dst_dir) / "projects" / slug / f"{sid}.jsonl"
    if src.resolve() != dst.resolve():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return True


# ---------------------------------------------------------------- run state

class Run:
    def __init__(self, run_dir: Path):
        self.dir = run_dir
        self.state_path = run_dir / "state.json"
        self.state = json.loads(self.state_path.read_text())
        self.cwd = self.state["cwd"]

    @classmethod
    def create(cls, plan: dict, cwd: Path) -> "Run":
        ids = [t["id"] for t in plan["tasks"]]
        if len(ids) != len(set(ids)):
            sys.exit("Plan error: duplicate task id")
        for t in plan["tasks"]:
            for d in t.get("depends_on", []):
                if d not in ids:
                    sys.exit(f"Plan error: {t['id']} -> unknown dependency {d}")
            if t.get("tier", "sonnet") not in MODELS:
                sys.exit(f"Plan error: {t['id']} invalid tier {t.get('tier')}")
        base = cwd / ".multiclaude"
        base.mkdir(exist_ok=True)
        (base / ".gitignore").write_text("*\n")
        name = re.sub(r"[^a-zA-Z0-9_-]", "-", plan.get("name", "run"))[:40]
        run_dir = base / f"{datetime.now():%Y%m%d-%H%M%S}-{name}"
        run_dir.mkdir()
        git_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True)
        state = {
            "name": plan.get("name", "run"), "context": plan.get("context", ""), "cwd": str(cwd),
            "created": datetime.now().isoformat(timespec="seconds"),
            "git_base": git_head.stdout.strip() if git_head.returncode == 0 else None,
            "tasks": {t["id"]: {**t, "tier": t.get("tier", "sonnet"), "depends_on": t.get("depends_on", []),
                                "status": "pending", "attempts": [], "session_id": None,
                                "account": None, "report": None, "cost": 0.0, "seconds": 0.0}
                      for t in plan["tasks"]},
        }
        (run_dir / "plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False))
        (run_dir / "state.json").write_text(json.dumps(state, indent=2, ensure_ascii=False))
        return cls(run_dir)

    def save(self) -> None:
        with lock:
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=2, ensure_ascii=False))
            tmp.replace(self.state_path)

    @property
    def tasks(self) -> dict:
        return self.state["tasks"]


# ---------------------------------------------------------------- worker

def build_prompt(run: Run, t: dict) -> str:
    parts = []
    if run.state["context"]:
        parts += ["# Project context", run.state["context"], ""]
    parts += [f"# Your task ({t['id']}): {t.get('title', '')}", t["prompt"], ""]
    if t.get("files"):
        parts += ["# Your scope (files/dirs you own)", "\n".join(f"- {f}" for f in t["files"]), ""]
    deps = [run.tasks[d] for d in t["depends_on"]]
    if deps:
        parts.append("# Already completed by teammates (you can build on this)")
        for d in deps:
            r = d.get("report") or {}
            parts.append(f"- [{d['id']}] {r.get('summary', '')} Files: {', '.join(r.get('files_changed', []))}"
                         + (f" Notes: {r['notes']}" if r.get("notes") else ""))
    return "\n".join(parts)


def call_claude(account: dict, t: dict, prompt: str, cwd: str, resume_sid: str | None, timeout: int) -> dict:
    cmd = [CLAUDE_BIN, "-p", "--model", MODELS[t["tier"]], "--output-format", "json",
           "--permission-mode", "bypassPermissions",
           # token savings: no MCP servers, skill listing, or user plugins/hooks
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--setting-sources", "project,local",
           "--append-system-prompt", WORKER_RULES,
           "--json-schema", json.dumps(REPORT_SCHEMA)]
    if not t.get("use_skills"):
        cmd.append("--disable-slash-commands")
    if t.get("effort"):
        cmd += ["--effort", t["effort"]]
    if resume_sid:
        cmd += ["--resume", resume_sid]
    env = {**os.environ, "CLAUDE_CONFIG_DIR": account["dir"], "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
           "PYTHONDONTWRITEBYTECODE": "1"}
    for k in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(k, None)
    try:
        p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=cwd, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"is_error": True, "result": f"timeout ({timeout}s)", "_timeout": True}
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {"is_error": True, "result": (p.stdout + "\n" + p.stderr).strip()[-1500:]}


def is_limit(data: dict) -> bool:
    return bool(data.get("is_error")) and (data.get("api_error_status") == 429
                                           or bool(LIMIT_RE.search(str(data.get("result", "")))))


# ---------------------------------------------------------------- scheduler

class Scheduler:
    def __init__(self, run: Run, accounts: list[dict], parallel: int, timeout: int, wait: bool):
        self.run, self.accounts, self.parallel, self.timeout, self.wait = run, accounts, parallel, timeout, wait
        self.threads: list[threading.Thread] = []

    def pick_account(self, prefer_not: str | None = None) -> dict | None:
        now = time.time()
        free = [a for a in self.accounts if a["limited_until"] <= now]
        if not free:
            return None
        free.sort(key=lambda a: (a["dir"] == prefer_not, a["running"], a["started"]))
        return free[0]

    def ready_tasks(self) -> list[dict]:
        out = []
        for t in self.run.tasks.values():
            if t["status"] != "pending":
                continue
            dep_status = [self.run.tasks[d]["status"] for d in t["depends_on"]]
            if any(s in ("failed", "skipped") for s in dep_status):
                t["status"] = "skipped"
                t["report"] = {"summary": "A task it depends on failed."}
                self.run.save()
                log(f"⤼ {t['id']} skipped (dependency failed)")
            elif all(s == "done" for s in dep_status):
                out.append(t)
        # start opus/critical work first (usually takes longer)
        out.sort(key=lambda t: t["tier"] != "opus")
        return out

    def execute(self, t: dict, account: dict) -> None:
        run = self.run
        resume_sid = t.get("session_id")
        if resume_sid and t.get("account") and t["account"] != account["dir"]:
            if migrate_session(resume_sid, t["account"], account["dir"], run.cwd):
                log(f"⇄ {t['id']} session moved {Path(t['account']).name} → {Path(account['dir']).name}")
            else:
                resume_sid = None
        prompt = ("Your previous session was interrupted (usage limit / error). Files you already wrote are "
                  "still on disk. Check where you left off and FINISH the task, then give the final report."
                  if resume_sid else build_prompt(run, t))
        t["account"] = account["dir"]
        model_label = t["tier"] + (f"/{t['effort']}" if t.get("effort") else "")
        log(f"▶ {t['id']} [{model_label}] @ {account['email']}" + (" (resume)" if resume_sid else ""))
        t0 = time.time()
        data = call_claude(account, t, prompt, run.cwd, resume_sid, self.timeout)
        dt = time.time() - t0
        with lock:
            account["running"] -= 1
            t["seconds"] += dt
            t["cost"] += data.get("total_cost_usd") or 0.0
            if data.get("session_id"):
                t["session_id"] = data["session_id"]
            t["attempts"].append({"account": account["email"], "seconds": round(dt),
                                  "error": str(data.get("result"))[:300] if data.get("is_error") else None})
            if is_limit(data):
                account["limited_until"] = time.time() + LIMIT_COOLDOWN
                t["status"] = "pending"
                log(f"⛔ {account['email']} hit its limit → {t['id']} will continue on another account")
            elif data.get("is_error") or not data.get("structured_output"):
                errors = sum(1 for a in t["attempts"] if a["error"] and not LIMIT_RE.search(a["error"]))
                if errors < 2:
                    t["status"] = "pending"
                    log(f"⚠ {t['id']} error, retrying: {str(data.get('result'))[:150]}")
                else:
                    t["status"] = "failed"
                    t["report"] = {"status": "failed", "summary": str(data.get("result"))[:500],
                                   "files_changed": [], "verification": "", "notes": ""}
                    log(f"✗ {t['id']} failed")
            else:
                rep = data["structured_output"]
                t["report"] = rep
                t["status"] = "done" if rep.get("status") != "failed" else "failed"
                log(f"{'✓' if t['status'] == 'done' else '✗'} {t['id']} {rep.get('status')} ({t['seconds']:.0f}s)")
            run.save()

    def loop(self) -> None:
        for t in self.run.tasks.values():        # recover tasks left "running" by a previous process
            if t["status"] == "running":
                t["status"] = "pending"
        self.run.save()
        while True:
            with lock:
                self.threads = [th for th in self.threads if th.is_alive()]
                active = len(self.threads)
                ready = self.ready_tasks()
                pending = [t for t in self.run.tasks.values() if t["status"] in ("pending", "running")]
            if not pending and active == 0:
                return
            started = False
            for t in ready:
                if active >= self.parallel:
                    break
                acc = self.pick_account(prefer_not=t.get("account") if t["attempts"] and
                                        t["attempts"][-1]["error"] else None)
                if not acc:
                    break
                with lock:
                    t["status"] = "running"
                    acc["running"] += 1
                    acc["started"] += 1
                    self.run.save()
                th = threading.Thread(target=self.execute, args=(t, acc), daemon=True)
                th.start()
                self.threads.append(th)
                active += 1
                started = True
            if ready and active == 0 and not started:
                # every account is limited
                if not self.wait:
                    log("⛔ All accounts are limited. Continue later with `multiclaude resume`.")
                    return
                soonest = min(a["limited_until"] for a in self.accounts) - time.time()
                log(f"⏳ All accounts are limited, retrying in {max(soonest, 0) / 60:.0f} min…")
                time.sleep(max(min(soonest, WAIT_POLL), 30))
                continue
            time.sleep(2)


# ---------------------------------------------------------------- report

def report(run: Run) -> str:
    ts = run.tasks.values()
    total_cost = sum(t["cost"] for t in ts)
    lines = [f"# multiclaude report: {run.state['name']}", f"Run: {run.dir}", "",
             "| task | model | account | status | time | cost* |", "|---|---|---|---|---|---|"]
    for t in ts:
        accs = " → ".join(dict.fromkeys(a["account"] for a in t["attempts"])) or "-"
        lines.append(f"| {t['id']} | {t['tier']}{'/' + t['effort'] if t.get('effort') else ''} | {accs} | "
                     f"{t['status']} | {t['seconds']:.0f}s | ${t['cost']:.2f} |")
    lines += [f"\n*API-equivalent usage, not an actual bill on subscription plans. Total ≈ ${total_cost:.2f}", ""]
    for t in ts:
        r = t.get("report") or {}
        lines.append(f"## {t['id']} — {t.get('title', '')} [{t['status']}]")
        if r.get("summary"):
            lines.append(r["summary"])
        if r.get("files_changed"):
            lines.append("Files: " + ", ".join(r["files_changed"]))
        if r.get("verification"):
            lines.append("Verification: " + r["verification"])
        if r.get("notes"):
            lines.append("Notes: " + r["notes"])
        lines.append("")
    if run.state.get("git_base"):
        stat = subprocess.run(["git", "diff", "--stat", run.state["git_base"]], cwd=run.cwd,
                              capture_output=True, text=True).stdout.strip()
        untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=run.cwd,
                                   capture_output=True, text=True).stdout.split()
        lines += ["## Git changes", "```", stat or "(no tracked changes)", "```"]
        if untracked:
            lines.append(f"New files ({len(untracked)}): " + ", ".join(untracked[:40]))
    unfinished = [t["id"] for t in ts if t["status"] in ("pending", "running")]
    if unfinished:
        lines += ["", f"⚠ Unfinished: {', '.join(unfinished)} → `multiclaude resume {run.dir}`"]
    return "\n".join(lines)


# ---------------------------------------------------------------- cli

def latest_run(cwd: Path) -> Path:
    runs = sorted(p for p in (cwd / ".multiclaude").glob("2*") if (p / "state.json").exists())
    if not runs:
        sys.exit("No multiclaude runs in this directory.")
    return runs[-1]


def execute_run(run: Run, args) -> None:
    lf = open(run.dir / ".lock", "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"This run is already in progress: {run.dir}")
    accounts = discover_accounts()
    log(f"{len(run.tasks)} tasks, {len(accounts)} accounts ({', '.join(a['email'] for a in accounts)}), "
        f"parallel={args.parallel} → {run.dir}")
    Scheduler(run, accounts, args.parallel, args.timeout, not args.no_wait).loop()
    rep = report(run)
    (run.dir / "report.md").write_text(rep + "\n")
    print(rep)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "resume"):
        p = sub.add_parser(name)
        p.add_argument("target", nargs="?", help="plan.json (run) or run directory (resume)")
        p.add_argument("--parallel", type=int, default=int(os.environ.get("MULTICLAUDE_PARALLEL", 4)))
        p.add_argument("--timeout", type=int, default=3600, help="seconds per task")
        p.add_argument("--no-wait", action="store_true", help="exit instead of waiting when all accounts are limited")
    sub.add_parser("status").add_argument("target", nargs="?")
    sub.add_parser("accounts")
    args = ap.parse_args()
    cwd = Path.cwd()

    if args.cmd == "accounts":
        for a in discover_accounts():
            print(f"{a['dir']}\t{a['email']}")
    elif args.cmd == "run":
        if not args.target:
            ap.error("plan.json required")
        execute_run(Run.create(json.loads(Path(args.target).read_text()), cwd), args)
    elif args.cmd == "resume":
        execute_run(Run(Path(args.target) if args.target else latest_run(cwd)), args)
    elif args.cmd == "status":
        print(report(Run(Path(args.target) if args.target else latest_run(cwd))))


if __name__ == "__main__":
    main()
