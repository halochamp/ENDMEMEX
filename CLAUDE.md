# ENDMEMEX project rules

- This public repository is self-contained; do not require a private parent repository for normal development.
- Read [`AGENT.md`](AGENT.md) and [`AGENT_PROCEDURE.md`](AGENT_PROCEDURE.md) before substantial work; use [`ENDMEMEX_USER_MANUAL.md`](ENDMEMEX_USER_MANUAL.md) for authoritative detailed commands/semantics.
- Before any ENDMEMEX write, read the README urgent rules and use the documented project/session lifecycle.
- Never store secrets, credentials, personal data, or large raw logs in ENDMEMEX records/checkpoints.
- Do not treat stale retrieval results as current truth without opening/validating the cited source.
- Checkpoint material phases, verification, handoffs, and commits; never mark unverified work `completed`.
- After changing tracked knowledge Markdown, run the documented `sync_tracked.py` flow.
- For a document that will be deleted after archival, use the documented durable `record-add --type knowledge` retirement workflow; do not use `ingest` as a substitute.
- Use `embed-diagnose` before changing dependencies/services in response to an embedding failure.
- Keep writable SQLite ownership local to one host; do not use filesystem-sync folders as distributed SQLite. Use the authenticated write gateway for remote mutation.
- Worker delegation is user-gated; reviewers/advisors are read-only. Presence writes are opt-in.
- Do not commit databases, WAL/journal files, logs, caches, sidecars, presence/runtime state, agent-run artifacts, or secrets.
- Standard regression: `python3 -m unittest discover -s developer -p 'test_*.py'` then `python3 endeavor_db.py doctor`; retrieval/ranking changes also run `python3 endeavor_db.py evaluate --semantic off --json`.
