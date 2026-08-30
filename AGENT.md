# Agent guide for ENDMEMEX

Quick rules for Codex, Claude, and other agents operating an ENDMEMEX
workspace. Read [`CLAUDE.md`](CLAUDE.md) for hard constraints and
[`AGENT_PROCEDURE.md`](AGENT_PROCEDURE.md) for the full agent workflow; exact
command/lifecycle semantics remain authoritative in the User Manual.

Read [README §Urgent rules](README.md#urgent-rules--read-before-writing)
before any write. Use the
[README task index](README.md#task-index--open-only-the-section-you-need) to
open only the relevant section of
[ENDMEMEX_USER_MANUAL.md](ENDMEMEX_USER_MANUAL.md); do not load the full manual
or copy its detailed procedures into this file.

For installation and public-release privacy, use
[INSTALL.eng-th.md](INSTALL.eng-th.md).

## Safe workflow

1. Before non-trivial work, bootstrap the selected project once:

   ```bash
   python3 endeavor_db.py bootstrap --project <PROJECT> --json
   ```

   A null handoff is normal. Continue a relevant returned session; use
   [`pack`](ENDMEMEX_USER_MANUAL.md#session-briefing-pack) only when the
   handoff needs wider context.

2. Query existing knowledge before rediscovering prior work or making a
   high-impact decision:

   ```bash
   python3 endeavor_db.py query "<question>" --project <PROJECT> --compact --json
   ```

   Open the cited source before relying on a result marked `stale: true`.

3. Checkpoint after every material phase, verification, handoff, or commit—not
   only at the end. Include completed work, current state, exact next steps,
   blockers, changed files, commands, and verification. Never mark unverified
   work `completed`.

4. After changing tracked knowledge Markdown, run:

   ```bash
   python3 sync_tracked.py <path>...
   ```

   Use `ingest` for a persistent untracked document. Never use `ingest` for a
   source that will be deleted.

5. Immediately tell the user after every successful project-memory write.
   Never store secrets, personal data, or large raw logs in the database.

Exact session, checkpoint, ingest, and sync commands:
[ENDMEMEX User Manual §Shared Session and Checkpoint Workflow](ENDMEMEX_USER_MANUAL.md#shared-session-and-checkpoint-workflow)
and [§Human-Readable Activity Digest](ENDMEMEX_USER_MANUAL.md#human-readable-activity-digest).

## Mandatory special procedures

- Pending work, backlog, handoff, or “what next?”: follow
  [README §Inspecting all pending work](README.md#inspecting-all-pending-work--mandatory-procedure)
  exactly. Do not substitute Git inspection or choose a session silently.
- Archive a document and delete its source: use `record-add --type knowledge`,
  never `ingest`, and verify the archived content before deletion. Follow
  [ENDMEMEX User Manual §Retiring a Document into Memory](ENDMEMEX_USER_MANUAL.md#retiring-a-document-into-memory-archive-and-delete).
- Embedding failure: run `python3 endeavor_db.py embed-diagnose` before
  claiming dependencies or the companion are missing, or changing/restarting
  anything. See [§Semantic Search](ENDMEMEX_USER_MANUAL.md#semantic-search-optional-minilm-companion).

## Records and coordination

- Use typed lifecycle links for audits, fixes, verification, supersession, and
  contradiction. Resolve current truth before relying on old records:
  [SQLite-Native Records](ENDMEMEX_USER_MANUAL.md#sqlite-native-records-and-references).
- Mailbox notes are pull-based knowledge records:
  [Agent-to-Agent Messaging](ENDMEMEX_USER_MANUAL.md#agent-to-agent-messaging-mailbox-convention-no-new-code).
- Presence writes are opt-in only; read-only presence checks are safe:
  [Agent Presence](ENDMEMEX_USER_MANUAL.md#agent-presence-whos-working-right-now).
- Delegation is user-gated. Scope the exact deliverable, files, constraints,
  and definition of done; the parent owns final verification:
  [Agent MCP Server](ENDMEMEX_USER_MANUAL.md#agent-mcp-server) or
  [`agent_delegate.py` fallback](ENDMEMEX_USER_MANUAL.md#cross-agent-delegation-agent_delegatepy).

## Verification and release hygiene

Run targeted tests for the code changed. From the ENDMEMEX directory, the
standard full regression command is:

```bash
python3 -m unittest discover -s developer -p 'test_*.py'
python3 endeavor_db.py doctor
```

For retrieval or ranking changes, also run:

```bash
python3 endeavor_db.py evaluate --semantic off --json
```

Do not commit SQLite databases, logs, caches, sidecar state, secrets, or
agent-run artifacts. Follow
[INSTALL §Keep private data out of Git](INSTALL.eng-th.md#6-keep-private-data-out-of-git)
for the public-release checklist.

## Documentation maintenance

- Keep this file as a quick guide; detailed commands and rationale belong in
  `ENDMEMEX_USER_MANUAL.md`.
- Use relative clickable links with direct section anchors.
- When renaming a file or heading, update all inbound links and verify local
  paths and anchors before finishing.
- Preserve mandatory README headings and procedure order because agents and
  tests treat them as contracts.
