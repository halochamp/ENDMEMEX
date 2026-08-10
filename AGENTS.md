# AGENTS.md

Quick rules for agents working in ENDMEMEX as either a standalone Public
repository or the `ENDMEMEX/` directory in the Main monorepo.

- Read [README](README.md) before any write. The detailed workflow is in
  [ENDMEMEX User Manual](ENDMEMEX_USER_MANUAL.md); keep this file short.
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

From the standalone repository, invoke commands as `python3 endeavor_db.py`.
From the Main monorepo, use `python3 ENDMEMEX/endeavor_db.py` instead. Both
layouts intentionally expose the same command behavior.
