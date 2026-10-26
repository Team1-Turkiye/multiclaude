---
name: multiclaude
description: Splits a project or a big multi-part request into a task plan and runs it in parallel across the user's multiple Claude Code accounts. Opus goes on critical work (security, polished UI, architecture/decisions) and Sonnet on routine work. If one account hits its usage limit, the task moves to another account and continues from the same point. When the run ends, the work is checked and a final report is given in chat. Use when the user says "multiclaude", "use multiclaude", "split this across accounts", "do this as a team", "parallelize this project", or "multiclaude resume/continue", or hands over a large multi-part project and asks to use it (also Turkish: "multiclaude kullan", "ekiple yap", "iş bölümü yap", "multiclaude devam").
---

# multiclaude: multi-account team orchestration

You (the main chat) are the **orchestrator/tech lead**. You make the plan and the critical decisions, the workers write the code, and you review the result and report it. The engine is the `multiclaude` CLI (must be on PATH, see https://github.com/Team1-Turkiye/multiclaude): it runs each task as `claude -p` on a different account, picks accounts automatically, and moves a task to another account with its session when a limit is hit.

## 1. Understand (keep it short)
- Turn the user's request into goals. Look only at what the plan needs (the repo tree, key config files). Don't read the whole repo; workers read their own files.
- Ask the user only about **decisions that block the work** (e.g. which payment provider). For everything else, pick a sensible default and write it into the plan's `context`.

## 2. Plan: split the work
Write `.multiclaude/plan-<name>.json` in the project root (format: `multiclaude --help`):

```json
{"name": "shop-mvp",
 "context": "Next.js 15 + Postgres (Drizzle). Folder layout... Shared rules/decisions...",
 "tasks": [
  {"id": "schema", "title": "DB schema + migrations", "tier": "opus", "depends_on": [], "files": ["src/db/**"], "prompt": "..."},
  {"id": "auth", "title": "Auth", "tier": "opus", "effort": "high", "depends_on": ["schema"], "files": ["src/auth/**", "src/app/(auth)/**"], "prompt": "..."},
  {"id": "crud", "title": "Product CRUD API", "tier": "sonnet", "depends_on": ["schema"], "files": ["src/app/api/products/**"], "prompt": "..."}
 ]}
```

**Tier rules:**
- `opus`: security (auth, sessions, permissions, payments, crypto/wallets, secret handling, input validation), quality UI/UX (landing pages, design system, polished components), architecture/data model/API contract decisions, complex algorithms, concurrency/state, tricky bug fixes, final code review. Add `"effort": "high"` for critical security work.
- `sonnet`: boilerplate/scaffolding, CRUD, wiring to a clear spec, config/CI, writing tests from a spec, docs, simple refactors, data migrations, form/list screens that follow existing UI patterns.
- `haiku`: only trivial mechanical work (renames, mass formatting).
- If unsure, pick sonnet. A mistake that would be costly or hard to reverse means opus.

**Rules for spending the fewest tokens in the least time:**
- Each worker has a fixed startup cost of about 20k tokens. **Don't spawn a worker for a tiny job.** Do 1–2 file / few-minute jobs yourself, or merge them into a related task.
- Typical plan size is 3–8 tasks. Use as few tasks as still allow parallel work.
- **The `files` scopes of tasks running at the same time must not overlap.** Everyone works in the same tree. Put shared files (types, schema, package.json, route registry) in one early foundation task, and have the other tasks `depends_on` it.
- Use `depends_on` only for real dependencies. Unnecessary chaining kills parallelism.
- Each `prompt` must stand alone. Workers don't see this chat, only `context`, their own prompt, and the summaries of the tasks they depend on. Include what to do, where, the interface/contract details, and acceptance criteria ("done when: ... tests pass"). Keep it short and specific.
- Put shared decisions (stack, naming, folder structure, API format, design tokens) in `context` once. Don't repeat them in every prompt.
- Each UI task should say which existing components/styles to use, and use `"use_skills": true` if needed (skills are off by default to save tokens).

## 3. Show the plan briefly, then start
Give the user a short table (id, job, model, dependency). Don't wait for approval unless they asked to review it, or a costly irreversible decision is involved. Start right away.

Run it in the **background** (Bash `run_in_background: true`, timeout 7200000):
```bash
cd <project-root> && multiclaude run .multiclaude/plan-<name>.json
```
You'll be notified when it finishes; don't poll. If the user asks in the meantime, check progress from the background output file or with `multiclaude status`.

Engine behavior:
- 4 parallel by default (`--parallel N`).
- On a limit error, the task moves to another account and continues from the same point.
- If every account is limited, it waits and retries every 15 minutes (`--no-wait` exits instead).
- After a regular error it retries once, then marks the task `failed`. Tasks that depend on a failed task are marked `skipped`.
- State lives in `.multiclaude/<run>/state.json`. If the process dies, `multiclaude resume` continues where it left off, even from another chat or another account.

## 4. Check (don't skip)
When the run ends, read the report the engine printed (also in `.multiclaude/<run>/report.md`). Then:
1. Run the whole project's **build / typecheck / lint / tests**.
2. **Read the diffs of the opus-tier (security, UI, critical) parts yourself** (`git diff <git_base>` plus new files). Skim the sonnet parts and check that the pieces fit together (imports, API contract, env variables).
3. Make sure the decisions in the workers' `notes` are consistent. Fix contradictions.
4. Fix small problems yourself. For large missing or broken parts, write a short follow-up plan (only the needed tasks) and run `multiclaude run` again.
5. If anything is `failed`/`skipped`, find the cause and either rerun it or do it yourself.

## 5. Final report (in the chat, in the user's language)
Short and clear:
- **What was done**: one line per task, plus which model/account did it.
- **Verification**: which commands ran and what came out (the real output; if something failed, say so).
- **What you fixed yourself.**
- **Open items / decisions waiting on the user**: risks, missing env/keys, manual steps.
- Account switches (limits), if any, and the total API-equivalent usage.
Don't commit unless the user asks. Remind them that `.multiclaude/` is gitignored.

## Continuing
If the user says "multiclaude resume/continue": run `multiclaude status` in the project root. If anything is unfinished, run `multiclaude resume` in the background, then continue from step 4.
