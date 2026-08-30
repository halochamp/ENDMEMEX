# AGENTS.md

Quick rules for agents working in ENDMEMEX as either a standalone public
repository or an `ENDMEMEX/` directory inside a parent monorepo.

- Read [`CLAUDE.md`](CLAUDE.md), [`AGENT.md`](AGENT.md), and [README](README.md) before substantial work. The agent workflow is in [`AGENT_PROCEDURE.md`](AGENT_PROCEDURE.md), while exact command/lifecycle semantics remain authoritative in the [ENDMEMEX User Manual](ENDMEMEX_USER_MANUAL.md); keep this file short.
- Before non-trivial work, bootstrap the selected project once and query
  existing knowledge before rediscovering prior decisions.
- Keep a writable ENDMEMEX database local to one host. For a remote mutation,
  use the authenticated write gateway; never use a filesystem-sync folder as
  distributed SQLite.
- Checkpoint material phases, verification, handoffs, and commits. Tell the
  user immediately after every successful memory write.
- After changing tracked knowledge Markdown, run `sync_tracked.py <path>`.
- For pending work, follow [README §Inspecting all pending work](README.md#inspecting-all-pending-work--mandatory-procedure)
  exactly; do not replace it with a Git-only inspection.
- Use `embed-diagnose` before attributing an embedding failure to packages or a
  stopped companion. Do not commit databases, logs, caches, sidecars, secrets,
  or agent-run artifacts.

The commands in this repository assume the standalone root and use
`python3 endeavor_db.py`. If this directory is embedded in a parent monorepo,
run the same scripts using paths relative to that parent workspace.
