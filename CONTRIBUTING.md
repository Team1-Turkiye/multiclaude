# Contributing to multiclaude

Thanks for helping out! You can open an issue, share a plan recipe, improve the docs, translate, or send code. Turkish and English are both fine in issues and PRs.

## Dev setup

```bash
git clone https://github.com/Team1-Turkiye/multiclaude.git
cd multiclaude
python3 -m unittest discover -s tests -v
```

You **don't need Claude accounts** to work on the engine. [`tests/fake_claude.py`](tests/fake_claude.py) stands in for the `claude` CLI. It writes session transcripts and can simulate usage limits (`FAKE_LIMITED_ACCOUNTS`) and task failures (`FAKE_FAIL_TASKS`). The tests point the engine at it with `MULTICLAUDE_CLAUDE_BIN` and use throwaway account dirs through `MULTICLAUDE_ACCOUNTS`.

To try your branch for real:

```bash
./install.sh                                 # links the CLI and skill to this checkout
cd /some/scratch/repo && multiclaude run /path/to/multiclaude/examples/plan.json
```

## Project layout

| Path | What it is |
|---|---|
| `multiclaude.py` | The engine: plan validation, DAG scheduler, account picking, failover and resume, reports. One file, stdlib only. |
| `skill/SKILL.md` | The Claude Code skill. It teaches the main chat how to plan, run, verify and report. |
| `tests/` | End-to-end tests against the fake CLI |
| `examples/` | Example plans |
| `install.sh` | Links the CLI and skill |

## Design principles

Please keep these in mind. They are the reasons multiclaude is useful.

1. **Zero dependencies.** `multiclaude.py` must run with only the Python 3.10+ standard library and the `claude` CLI.
2. **Tokens are the budget.** Anything added to the worker prompt, system prompt or report costs tokens on every task. Keep worker reports small and structured.
3. **State is durable.** Any state needed to continue a run lives in `state.json` and is saved after every transition. A run must always be resumable, even after `kill -9`.
4. **The lead verifies.** The engine runs tasks. Judgement (planning, review, fixes) belongs to the main chat, through the skill.
5. **No surprises in the user's repo.** Workers don't commit, rewrite history or touch files outside their scope.

## Making changes

- **Engine behaviour changes need a test** in `tests/test_multiclaude.py`. Extend `fake_claude.py` if you need to simulate something new.
- **If you change flags, plan fields or behaviour, update the docs**: `README.md` and `README.tr.md`, plus `skill/SKILL.md` if the lead should act differently.
- **Keep PRs focused.** One change per PR is easiest to review.
- **Never include personal data** (e-mails, tokens, local paths) in code, tests, logs or screenshots.

## Good first contributions

- **Plan recipes.** Add well-tuned example plans under `examples/` (Next.js app, FastAPI service, Solana program, mobile app…).
- **Smarter limit handling.** Parse the reset time from limit messages instead of a fixed cooldown.
- **Live progress.** Add a `multiclaude watch` command or streaming progress (`--output-format stream-json`).
- **Per-account preferences.** For example "use account X only for sonnet", or weights by plan size.
- **Worktree mode.** Optionally isolate workers in git worktrees and have the lead merge them.
- **Windows support.** `fcntl` is POSIX-only today.
- **Translations** of the README and skill trigger phrases.

Look for issues labelled `good first issue` and `help wanted`.

## Reporting bugs

Use the bug report template. Include `claude --version`, your OS, and the relevant log or `report.md` lines. **Remove account e-mails first.** Report security issues privately, as described in [SECURITY.md](SECURITY.md).

## Code of conduct

This project follows the [Code of Conduct](CODE_OF_CONDUCT.md). Be kind and constructive.
