# Security Policy

## Reporting a vulnerability

**Please don't open a public issue for security problems.**

Report them privately through GitHub: go to the repository's **Security** tab, then **Report a vulnerability**. Include the steps to reproduce, the impact, and the affected version or commit if you can. We'll acknowledge the report as soon as possible and keep you updated while we work on a fix.

## Scope

Things we especially want to hear about:

- Ways a plan, a task prompt or repository content could make workers escape their intended scope or run unintended commands
- Leaks of account credentials, session transcripts or personal data through reports, logs or the files the engine writes
- Problems with how session transcripts are copied between account config dirs

Remember that workers run with `bypassPermissions` by design (see the Safety section of the README). Reports about that design choice are welcome as discussions or feature requests.
