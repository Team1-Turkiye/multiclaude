#!/usr/bin/env python3
"""Stand-in for the `claude` CLI used by the test suite.

Reads the prompt from stdin, writes `<task-id>.txt` into the cwd, stores a fake
session transcript under $CLAUDE_CONFIG_DIR/projects/<slug>/ and prints a
`--output-format json` style result.

Behaviour is controlled by env vars:
  FAKE_LIMITED_ACCOUNTS  comma-separated config dirs that answer with a usage-limit error
  FAKE_FAIL_TASKS        comma-separated task ids that always fail with a non-limit error
  FAKE_LOG               file to append one JSON line per invocation to
"""
import json
import os
import re
import sys
import uuid
from pathlib import Path

args = sys.argv[1:]
cfg = os.environ["CLAUDE_CONFIG_DIR"]
cwd = os.getcwd()
prompt = sys.stdin.read()
resume = args[args.index("--resume") + 1] if "--resume" in args else None
model = args[args.index("--model") + 1]
slug = re.sub(r"[^a-zA-Z0-9]", "-", os.path.realpath(cwd))
proj = Path(cfg) / "projects" / slug


def log(event):
    if os.environ.get("FAKE_LOG"):
        with open(os.environ["FAKE_LOG"], "a") as f:
            f.write(json.dumps({"account": cfg, "model": model, "resume": resume, **event}) + "\n")


if resume:
    transcript = proj / f"{resume}.jsonl"
    if not transcript.exists():
        log({"event": "missing-session"})
        print(json.dumps({"is_error": True, "result": f"No conversation found with session ID: {resume}"}))
        sys.exit(1)
    task_id = json.loads(transcript.read_text().splitlines()[0])["task"]
    sid = resume
else:
    m = re.search(r"# Your task \(([^)]+)\)", prompt)
    task_id = m.group(1)
    sid = str(uuid.uuid4())
    proj.mkdir(parents=True, exist_ok=True)
    (proj / f"{sid}.jsonl").write_text(json.dumps({"task": task_id, "prompt": prompt}) + "\n")

if task_id in os.environ.get("FAKE_FAIL_TASKS", "").split(","):
    log({"event": "error", "task": task_id})
    print(json.dumps({"is_error": True, "session_id": sid, "result": "Tool crashed"}))
    sys.exit(1)

limited = [d for d in os.environ.get("FAKE_LIMITED_ACCOUNTS", "").split(",") if d]
if cfg in limited:
    log({"event": "limit", "task": task_id})
    print(json.dumps({"is_error": True, "session_id": sid, "api_error_status": 429,
                      "result": "Claude AI usage limit reached", "total_cost_usd": 0.01}))
    sys.exit(1)

Path(cwd, f"{task_id}.txt").write_text(f"done by {cfg}\n")
log({"event": "done", "task": task_id})
print(json.dumps({
    "is_error": False, "session_id": sid, "total_cost_usd": 0.02, "result": "ok",
    "structured_output": {"status": "done", "summary": f"{task_id} finished", "files_changed": [f"{task_id}.txt"],
                          "verification": "fake", "notes": ""},
}))
