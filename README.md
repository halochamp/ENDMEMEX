# ENDEAVOR Memory

Local SQLite knowledge, durable-record, and resumable checkpoint store for
Claude Code and Codex.

## How to use these docs

This README is the short entry point. Read it for orientation, hard safety
rules, session startup, and the mandatory pending-work procedure.

Do **not** read the full manual by default. Find the task in the index below,
then open only that section of
[`ENDMEMEX_USER_MANUAL.md`](ENDMEMEX_USER_MANUAL.md). The manual owns exact
commands, flags, payloads, examples, and design rationale.

For a command-only cheat sheet without opening either document:

```bash
python3 endeavor_db.py agent-help
```

## Urgent rules — read before writing

1. Keep a writable `endeavor_memory.sqlite3` local to one host.
2. Do not place a writable database in iCloud, Dropbox, or another filesystem
   sync service. SQLite WAL supports concurrent processes only on the same
   local database.
3. For remote mutations, use the authenticated `write_gateway.py` service;
   never attempt distributed SQLite through a shared folder.
4. Prefer connected `endmemex` MCP tools; their live schemas and initialization
   instructions are authoritative. `[error]` is failure, null handoffs are
   normal, and a `stale=true` result requires opening the cited Markdown source.
5. Checkpoint after every material phase or verification, not only at the end,
   and immediately tell the user after every successful memory write.
6. Never store tokens, passwords, private keys, secret environment values, or
   large raw logs in checkpoints or records.
7. Before claiming MiniLM/FastAPI is missing or changing/restarting embedding
   components, run `python3 endeavor_db.py embed-diagnose`.
8. Presence writes are opt-in. Use them only when the user asks agents to
   announce or coordinate concurrent work; read-only presence checks are safe.

## What it stores

- Searchable sections from tracked Markdown with source paths, headings, line
  provenance, hashes, structured metadata, and lexical/optional semantic
  retrieval.
- Shared sessions and checkpoints containing completed work, current state,
  next steps, blockers, changed files, commands, and verification.
- Permanent SQLite-native audit, fix, verification, decision, and knowledge
  records connected by typed lifecycle relations.
- Activity history, checkpoint timelines, and opt-in agent presence.

Markdown project-memory, audit, training, and bug-report files remain the
human-readable source of truth. SQLite is the searchable continuity layer;
SQLite-native records and their typed relations are authoritative for their
own lifecycle.

## Start a project session

Run from the repository root. On a fresh clone, initialize once and install
the tracked pre-commit hook:

```bash
python3 endeavor_db.py init
python3 endeavor_db.py install-hooks
```

Before non-trivial work, bootstrap the selected project once:

```bash
python3 endeavor_db.py bootstrap --project <PROJECT> --json
```

`bootstrap` returns the resumable handoff (null is a normal empty state), runs
best-effort embedding backfill, and reports tracked-document freshness and hook
state. Continue the returned session when relevant. Otherwise, the first
`checkpoint --project <PROJECT> --goal "..."` can auto-start a session.

For a one-command, read-only preflight before starting a project session, run
`readiness --project <PROJECT>`; it reports the local host, database health,
embedding/ANN state, tracked-document freshness, and ordered next actions. See
[Readiness preflight](ENDMEMEX_USER_MANUAL.md#readiness-preflight-readiness).

Before rediscovering prior work or making a high-impact decision, query memory:

```bash
python3 endeavor_db.py query "<question>" --project <PROJECT> --json --compact
```

After changing a tracked Markdown memory document, refresh its index:

```bash
python3 sync_tracked.py <path>...
```

For a single persistent document that is not yet tracked, use `ingest`. For a
document that will be deleted, do **not** ingest it; follow
[Retiring a Document into Memory](ENDMEMEX_USER_MANUAL.md#retiring-a-document-into-memory-archive-and-delete).

## Task index — open only the section you need

| Need | Read this section |
|---|---|
| Find a CLI command or matching MCP tool | [Quick Reference](ENDMEMEX_USER_MANUAL.md#quick-reference) |
| Understand ingest, seed, activity digest, or live activity follow | [Human-Readable Activity Digest](ENDMEMEX_USER_MANUAL.md#human-readable-activity-digest) |
| Search knowledge, use filters, compact output, or resolve stale results | [Query Knowledge](ENDMEMEX_USER_MANUAL.md#query-knowledge) |
| Load a wider bounded briefing after bootstrap | [Session Briefing (`pack`)](ENDMEMEX_USER_MANUAL.md#session-briefing-pack) |
| Diagnose, warm, cool, backfill, or evaluate MiniLM embeddings | [Semantic Search](ENDMEMEX_USER_MANUAL.md#semantic-search-optional-minilm-companion) |
| Get one read-only project preflight with ordered next actions | [Readiness preflight](ENDMEMEX_USER_MANUAL.md#readiness-preflight-readiness) |
| Create/search/link audit, fix, verification, decision, or knowledge records | [SQLite-Native Records and References](ENDMEMEX_USER_MANUAL.md#sqlite-native-records-and-references) |
| Archive a document before deleting its source file | [Retiring a Document into Memory](ENDMEMEX_USER_MANUAL.md#retiring-a-document-into-memory-archive-and-delete) |
| Send or read cross-agent mailbox notes | [Agent-to-Agent Messaging](ENDMEMEX_USER_MANUAL.md#agent-to-agent-messaging-mailbox-convention-no-new-code) |
| Start/resume/close sessions or write checkpoints | [Shared Session and Checkpoint Workflow](ENDMEMEX_USER_MANUAL.md#shared-session-and-checkpoint-workflow) |
| Inspect who did what checkpoint by checkpoint | [Checkpoint Timeline](ENDMEMEX_USER_MANUAL.md#checkpoint-timeline-who-did-what-read-only) |
| Write a good checkpoint or understand retention/pinning | [Checkpoint Quality Rules](ENDMEMEX_USER_MANUAL.md#checkpoint-quality-rules) |
| Announce or inspect active agent ownership | [Agent Presence](ENDMEMEX_USER_MANUAL.md#agent-presence-whos-working-right-now) |
| Check cross-machine freshness signals | [Sync Freshness Signal](ENDMEMEX_USER_MANUAL.md#sync-freshness-signal-informational-not-a-lock) |
| Submit remote writes or consume completion events | [Remote Write Gateway and Durable Events](ENDMEMEX_USER_MANUAL.md#remote-write-gateway-and-durable-events) |
| Run `VACUUM`/optimization safely | [Database Maintenance](ENDMEMEX_USER_MANUAL.md#database-maintenance) |
| Use or register memory MCP tools | [MCP Server](ENDMEMEX_USER_MANUAL.md#mcp-server) |
| Start/status/cancel managed agent runs | [Agent MCP Server](ENDMEMEX_USER_MANUAL.md#agent-mcp-server) |
| Delegate through `agent_delegate.py` fallback | [Cross-Agent Delegation](ENDMEMEX_USER_MANUAL.md#cross-agent-delegation-agent_delegatepy) |
| Select a verified explicit model name | [Model names](ENDMEMEX_USER_MANUAL.md#model-names-verified-2026-07-24) |

### Inspecting all pending work — mandatory procedure

When the user asks about pending work, unfinished work, a backlog, handoffs,
or what to continue, do not begin with Git inspection, broad file searches,
or a project-specific `bootstrap`/`pack`. Use this exact order:

1. Run `pending --all-projects --json` to discover all projects' active
   presence, resumable/blocked sessions, and lifecycle-aware actionable
   records.
2. Run `handoff --all-paused --json` to obtain the authoritative resume queue.
   Treat each paused checkpoint's goal, blockers, and `next_steps` as the
   work-continuation source of truth.
3. Keep `resumable_sessions`, `actionable_records`, `active_presence`,
   `blocked_*`, and `deferred_records` separate in the report. Presence is
   ownership information, not a task; `last_known_presence` is only a
   cross-machine hint. Raw `open_records` is not a resume queue and may be
   historical evidence superseded by lifecycle relations.
4. If one or more paused sessions exist, present them and ask the user which
   one to resume. Never select, prioritize, or resume one silently. If none
   exist, report that the resume queue is empty and ask whether the user wants
   to triage a named actionable record or start new work.
5. After the user selects a task, run `bootstrap --project <PROJECT> --session
   <SELECTED_SESSION_ID> --json`. Omit `--session` only for unambiguous new
   work. Use `pack --project <PROJECT> --session <SELECTED_SESSION_ID> --json`
   only when the selected handoff needs wider context. Carry the same session
   ID through later checkpoints.

Actionable records are current unresolved durable records. Keep blocked and
deferred records separate. Active work remains owned by its current agent
unless the user explicitly transfers it.

## Developer documentation and tests

- [`developer/DESIGN.md`](developer/DESIGN.md) — canonical architecture,
  schema, write/search paths, scaling, and failure modes.
- `developer/eval_queries.json` — retrieval regression cases.
- `developer/test_endeavor_db.py` — fast hermetic regression suite.

After changing retrieval, ranking, tokenization, or ENDMEMEX behavior:

```bash
python3 ENDMEMEX/endeavor_db.py evaluate --semantic off --json
python3 -m unittest discover -s ENDMEMEX/developer -p 'test_*.py'
```
