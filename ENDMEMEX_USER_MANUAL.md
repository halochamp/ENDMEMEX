# ENDEAVOR Memory — User Manual

Detailed command reference for `ENDMEMEX`. Start with
[`README.md`](README.md) for orientation, safety rules, and quick start —
this file covers everything else: exact commands, flags, and design
rationale for each feature.

## Table of Contents

- [Quick Reference](#quick-reference)
- [Readiness preflight (readiness)](#readiness-preflight-readiness)
- [Human-Readable Activity Digest](#human-readable-activity-digest)
- [Query Knowledge](#query-knowledge)
- [Session Briefing (pack)](#session-briefing-pack)
- [Semantic Search (optional MiniLM companion)](#semantic-search-optional-minilm-companion)
- [SQLite-Native Records and References](#sqlite-native-records-and-references)
- [Retiring a Document into Memory (archive-and-delete)](#retiring-a-document-into-memory-archive-and-delete)
- [Agent-to-Agent Messaging](#agent-to-agent-messaging-mailbox-convention-no-new-code)
- [Shared Session and Checkpoint Workflow](#shared-session-and-checkpoint-workflow)
- [Checkpoint Quality Rules](#checkpoint-quality-rules)
- [Agent Presence](#agent-presence-whos-working-right-now)
- [Sync Freshness Signal](#sync-freshness-signal-informational-not-a-lock)
- [Database Maintenance](#database-maintenance)
- [MCP Server](#mcp-server)
- [Agent MCP Server](#agent-mcp-server)
- [Cross-Agent Delegation](#cross-agent-delegation-agent_delegatepy)

## Quick Reference

Every `python3 endeavor_db.py <command>` subcommand (43 total),
grouped by workflow. `<command> --help` always has the exact flags; this
table is for finding which command/section you need. Prefer the `endmemex`
MCP tools when connected — the last column names the matching tool where one
exists; commands with no MCP entry are CLI/human-only (usually because they
touch the filesystem, are destructive, or are meant for a human to read).

| Command | Purpose | MCP tool | Details |
|---|---|---|---|
| `init` | Create or migrate the database | — | [README §Start a project session](README.md#start-a-project-session) |
| `agent-help` | Print a CLI cheat sheet, no DB access | — | — |
| `bootstrap` | One-call session start: handoff + embedding backfill + doc freshness + hooks | `endeavor_memory_bootstrap` | [§Session Briefing](#session-briefing-pack) |
| `readiness` | One read-only preflight: machine role + DB + embeddings + ANN + docs + ordered next actions | `endeavor_memory_readiness` | [§Readiness preflight](#readiness-preflight-readiness) |
| `pack` | Wider session briefing: handoff + open records + knowledge + activity, budget-bounded | `endeavor_memory_pack` | [§Session Briefing](#session-briefing-pack) |
| `pending` | Lifecycle-aware pending work (presence + resumable/blocked sessions + open records) | `endeavor_memory_pending` | [README §Inspecting all pending work](README.md#inspecting-all-pending-work--mandatory-procedure) |
| `seed` | Ingest/refresh the bundled ENDMEMEX guides | — | [§Activity Digest](#human-readable-activity-digest) |
| `ingest` | Ingest or refresh one Markdown document | — | [§Activity Digest](#human-readable-activity-digest) |
| `activity` | Write/print the human-readable `ACTIVITY.md` digest, or `--follow` it live | — | [§Activity Digest](#human-readable-activity-digest) |
| `query` | Search Markdown knowledge + current durable records (lexical + optional semantic) | `endeavor_memory_query` | [§Query Knowledge](#query-knowledge) |
| `evaluate` | Compare Markdown-only and production unified retrieval (`--pipeline`) | — | [§Semantic Search](#semantic-search-optional-minilm-companion) |
| `feedback` | Record whether a query result was useful | `endeavor_memory_feedback` | [§Semantic Search](#semantic-search-optional-minilm-companion) |
| `embed-status` | Embedding coverage + companion diagnostics (never spawns) | — | [§Semantic Search](#semantic-search-optional-minilm-companion) |
| `embed-diagnose` | Interpreter/dependency/socket diagnosis (never spawns) — required first step before any embedding fix | — | [§Semantic Search](#semantic-search-optional-minilm-companion) |
| `embed-warm` | Start the MiniLM companion now, optionally `--keep-alive` | — | [§Semantic Search](#semantic-search-optional-minilm-companion) |
| `embed-cool` | Return a `--keep-alive` companion to its normal 1-hour idle timeout | — | [§Semantic Search](#semantic-search-optional-minilm-companion) |
| `embed-backfill` | Spawn the companion if needed and embed any missing/stale rows | — | [§Semantic Search](#semantic-search-optional-minilm-companion) |
| `ann-status` / `ann-build` | Inspect or build the optional per-machine HNSW sidecar | — | [§Semantic Search](#semantic-search-optional-minilm-companion) |
| `record-add` | Add a durable SQLite-native audit/fix/verification/decision/knowledge record | `endeavor_memory_record_add` | [§SQLite-Native Records](#sqlite-native-records-and-references) |
| `record-update` | Correct/enrich truth fields or independent action state | `endeavor_memory_record_update` | [§SQLite-Native Records](#sqlite-native-records-and-references) |
| `record-link` | Link two **already-existing** records after the fact | `endeavor_memory_record_link` | [§SQLite-Native Records](#sqlite-native-records-and-references) |
| `record-show` | Show a record plus its lifecycle graph (current successor, conflicts) | `endeavor_memory_record_show` | [§SQLite-Native Records](#sqlite-native-records-and-references) |
| `record-search` | Full-text search durable records, optionally lifecycle-resolved | `endeavor_memory_record_search` | [§SQLite-Native Records](#sqlite-native-records-and-references) |
| `session-start` | Start a shared work session | — | [§Checkpoint Workflow](#shared-session-and-checkpoint-workflow) |
| `checkpoint` | Record resumable session state (can also auto-start the session) | `endeavor_memory_checkpoint` | [§Checkpoint Workflow](#shared-session-and-checkpoint-workflow) |
| `pin-checkpoint` / `unpin-checkpoint` | Exempt/restore a checkpoint from sliding-window pruning | `endeavor_memory_pin_checkpoint` | [§Checkpoint Quality Rules](#checkpoint-quality-rules) |
| `handoff` | Read the latest resumable checkpoint (single session, or `--all-paused` queue) | `endeavor_memory_handoff` | [§Checkpoint Workflow](#shared-session-and-checkpoint-workflow) |
| `timeline` | Read-only checkpoint-by-checkpoint history across sessions (who did what) | `endeavor_memory_timeline` | [§Checkpoint Timeline](#checkpoint-timeline-who-did-what-read-only) |
| `session-close` | Mark a session `completed` or `blocked` | `endeavor_memory_session_close` | [§Checkpoint Workflow](#shared-session-and-checkpoint-workflow) |
| `event-poll` / `event-ack` | Consume durable completion events | `endeavor_memory_event_poll` / `endeavor_memory_event_ack` | [§Remote Write Gateway](#remote-write-gateway-and-durable-events) |
| `presence-start` / `-heartbeat` / `-stop` | Announce/refresh/clear live "who's working on what" (**opt-in**, see below) | `endeavor_presence_start` / `_heartbeat` / `_stop` | [§Agent Presence](#agent-presence-whos-working-right-now) |
| `presence` | List active presence rows (this machine live + other machines last-known) | `endeavor_presence_list` | [§Agent Presence](#agent-presence-whos-working-right-now) |
| `sync-status` | Last-known write time per machine (informational, not a lock) | `endeavor_sync_status` | [§Sync Freshness Signal](#sync-freshness-signal-informational-not-a-lock) |
| `maintenance` | `VACUUM` + `PRAGMA optimize`, manual-only, `--yes` required | — | [§Database Maintenance](#database-maintenance) |
| `stats` | Database counts, checkpoint caps, pin warning threshold | — | [§Checkpoint Quality Rules](#checkpoint-quality-rules) |
| `doctor` | Integrity check: SQLite, FTS identity, embeddings, relation lifecycles | — | [§Semantic Search](#semantic-search-optional-minilm-companion) |
| `install-hooks` | Copy the tracked pre-commit hook into `.git/hooks` — run once per fresh clone | — | see below |

**`install-hooks`** — a fresh `git clone` has no `.git/hooks/pre-commit`, since
hooks live outside version control by git's own design. Run this once per
clone so editing a tracked `.md` file automatically re-syncs it into
ENDMEMEX on commit (advisory-only — it never blocks the commit if the sync
fails):

```bash
python3 endeavor_db.py install-hooks
```

`doctor`/`bootstrap` report drift (`installed`/`differs`/`missing`) if the
live copy no longer matches the tracked source (`hooks/pre-commit`)
after a hook-script change — re-run `install-hooks` to pick it up; a running
git process does not auto-update its own hooks the same way a running server
doesn't auto-reload edited code.

### MCP tools (23, `endmemex` server)

Every tool name below is the exact string an MCP client sees (with the
`mcp__endmemex__` prefix Claude Code adds). Write tools mutate the database
local to the MCP server process; do not point two hosts at one writable SQLite
file.

| Tool | R/W | Gated | Maps to |
|---|---|---|---|
| `endeavor_memory_query` | read | no | `query` |
| `endeavor_memory_readiness` | read | no | `readiness` |
| `endeavor_memory_pack` | read | no | `pack` |
| `endeavor_memory_pending` | read | no | `pending` |
| `endeavor_memory_handoff` | read | no | `handoff` |
| `endeavor_memory_timeline` | read | no | `timeline` |
| `endeavor_memory_record_show` | read | no | `record-show` |
| `endeavor_memory_record_search` | read | no | `record-search` |
| `endeavor_presence_list` | read | no | `presence` |
| `endeavor_sync_status` | read | no | `sync-status` |
| `endeavor_memory_bootstrap` | write | no | `bootstrap` |
| `endeavor_memory_checkpoint` | write | no | `checkpoint` |
| `endeavor_memory_pin_checkpoint` | write | no | `pin-checkpoint`/`unpin-checkpoint` |
| `endeavor_memory_record_add` | write | no | `record-add` |
| `endeavor_memory_record_update` | write | no | `record-update` |
| `endeavor_memory_record_link` | write | no | `record-link` |
| `endeavor_memory_session_close` | write | no | `session-close` |
| `endeavor_memory_feedback` | write | no | `feedback` |
| `endeavor_memory_event_poll` | read | no | `event-poll` |
| `endeavor_memory_event_ack` | write, idempotent | no | `event-ack` |
| `endeavor_presence_start` | write | **opt-in** | `presence-start` |
| `endeavor_presence_heartbeat` | write | **opt-in** | `presence-heartbeat` |
| `endeavor_presence_stop` | write | **opt-in** | `presence-stop` |

No MCP tool exists for `session-start`, `seed`, `ingest`, `activity`, the `embed-*` family,
`maintenance`, `stats`, `doctor`, or `install-hooks` — use the CLI for those.

## Readiness preflight (readiness)

Use this before starting or resuming a project when one answer needs to tell
you whether ENDMEMEX is usable and exactly what to do next:

```bash
python3 endeavor_db.py readiness --project <PROJECT>
```

It is read-only. It never runs `bootstrap`, starts or backfills
the embedding companion, builds ANN, changes hooks, or prunes documents.
`--json` returns the complete report for automation or the matching
`endeavor_memory_readiness` MCP tool.

The report has `overall` (`ready`, `attention`, or `blocked`) plus:

- `machine`: local host identity and whether the local process permits writes.
- `database`: schema, SQLite integrity, foreign keys, FTS identity, record
  lifecycle, and hook state.
- `embedding`: coverage for Markdown knowledge and native record chunks.
- `ann`: whether exact semantic scanning remains sufficient (at most 20,000
  embedded knowledge rows) and, above that limit, whether the local sidecar is
  fresh.
- `documents`: freshness counts scoped to `--project`. Orphaned documents
  only produce a hash-pinned review proposal; this command never deletes them.
- `next_actions`: ordered `P0`, `P1`, or `OK` actions with a command where one
  is safe to offer.

`attention` and `blocked` still exit successfully when the report itself was
produced; consume `overall` and `next_actions` rather than treating an
actionable health finding as an MCP transport error.

If the database is missing or cannot be opened, readiness still returns a
`blocked` report and does not create a file. It offers the explicit `init`
command when initialization is the safe next action.

## Human-Readable Activity Digest

`ACTIVITY.md` is a generated, gitignored digest of the latest
50 write actions (checkpoints with their summaries, session starts/closes,
audit/fix records with titles, document ingests), newest first, in local
time. It refreshes automatically after every write command, so a human can
follow what both agents have been doing without touching SQLite:

```bash
python3 endeavor_db.py activity            # regenerate (default 50)
python3 endeavor_db.py activity --limit 200
python3 endeavor_db.py activity --stdout   # print instead of write
```

The refresh is best-effort — a failed export never fails the write that
triggered it. Do not edit the file; it is overwritten on the next write.

### Watching the other agent's activity live

Codex and Claude Code don't message each other directly or run as
always-on listeners — coordination is asynchronous through this database,
and normally a human decides when to switch from one agent's session to the
other's. `activity --follow` shortens the gap between "the other agent wrote
something" and "a human notices" without changing that model: it's a
read-only poll loop a human runs in a spare terminal, not agent-to-agent
messaging, an autonomous trigger, or a new daemon.

```bash
python3 endeavor_db.py activity --follow                       # all projects
python3 endeavor_db.py activity --follow --project ENDMEMEX
python3 endeavor_db.py activity --follow --interval 1          # default 3s
```

New rows print as `TIMESTAMP · agent · action · project — description`
(Ctrl-C to stop); nothing is written. Latency is bounded by `--interval` only
for processes reading the same local database. Across hosts, use an explicit
transport such as the authenticated write gateway; never synchronize a
writable SQLite file through a shared filesystem.

`seed` imports or refreshes these authoritative sources and is idempotent:

- `README.md`
- `ENDMEMEX_USER_MANUAL.md`
- `AGENT.md`

The source hash is checked before ingest. Changed documents replace their old
knowledge rows; unchanged documents are not duplicated.

To keep the broader active workspace knowledge current, run:

```bash
python3 sync_tracked.py
```

This discovers eligible human-authored, Git-tracked Markdown, including project
memories, bug reports, audits, plans, test reports, and skill references. It
deliberately excludes generated folders, third-party notices, and full
prompt-baseline snapshots.
Every source is hash-checked, so re-running it is safe and only changed
documents are re-indexed. Pass one or more repository-relative paths to sync
a specific eligible document.

**Tracking Markdown outside `ROOT`:** `sync_tracked.py`'s
`EXTERNAL_TRACKED_ROOTS` list names folders to also index that are not
inside this Git repository at all (e.g. a sibling standalone-fork checkout
directory holding several independent repos as immediate subfolders).
Unlike the discovery above, an external root is walked by plain filesystem
glob rather than `git ls-files` -- there is no requirement that the root
itself be a single Git repository -- so it also picks up files a fresh
clone or an in-progress, not-yet-committed checkout would have. Each
discovered file's `--project` label is its path's first component under
that external root (`<root>/sample-project/x.md` -> project
`sample-project`); a file directly at the external root's top level, with no
subfolder, is labeled with the root folder's own name instead (spaces become
underscores). Add or remove
entries by editing the tuple directly (repo-relative source, no config
file); every other command (`--check`, `--propose-prune`, `readiness`,
`doctor`) already treats these the same as any other tracked document once
discovered, since the stored `source_path` falls back to an absolute path
string whenever the file isn't under `ROOT` -- the same fallback
`display_path()`/`ingest_markdown()` already use for any out-of-tree source.
A project label reused by both the in-repo tree and an external root (e.g.
`ENDMEMEX` existing both here and in a public fork) is intentional, not a
collision to fix: a query scoped to that project returns matches from both.

After changing a tracked document, verify its source hash without writing to
SQLite:

```bash
python3 sync_tracked.py sample_app/developer/PROJECT_MEMORY.md
python3 sync_tracked.py --check --json sample_app/developer/PROJECT_MEMORY.md
```

`--check` opens the database read-only and reports `current`, `stale`,
`missing`, `orphaned`, and `metadata_mismatch`. It exits non-zero for any
non-current category, so an interrupted multi-file sync is observable and
recoverable. A path-scoped check intentionally does not report unrelated
indexed documents as orphaned.

If a prior broad import included documents that no longer match this curated
set, generate a hash-pinned proposal, review it, then apply that exact file.
Apply aborts if a target changed, disappeared, or became tracked again. It
deletes only SQLite index rows, never source files:

```bash
python3 sync_tracked.py --propose-prune /tmp/endmemex-prune.json
# review entries, content_hash, project, and knowledge_rows
python3 sync_tracked.py --apply-prune /tmp/endmemex-prune.json
```

The legacy direct `--prune` remains for compatibility, but the reviewed
proposal workflow is the safe default for ordinary maintenance.

## Query Knowledge

```bash
python3 endeavor_db.py query "วิธีแก้ bug" --limit 5
python3 endeavor_db.py query "delegate agent" --project ENDMEMEX
python3 endeavor_db.py query "silent failure" --category agent_training --json
python3 endeavor_db.py query "V2-RI01" --bug-id V2-RI01 --status resolved
python3 endeavor_db.py query "orySaver"  # substring match
```

Normal `query` searches both Markdown-derived `knowledge` and the current
heads of SQLite-native `memory_records`; `source_kind` identifies which store
produced each result. Search fuses exact/all-term, broad/any-term,
English-stemmed, and trigram substring FTS5 passes with reciprocal-rank
fusion, plus an optional MiniLM semantic pass (see below). A small
intent/category boost favors reusable training guidance over one-off
incidents, metadata filters narrow known bugs/modules, and parent-heading
diversity prevents one long section monopolizing results. Each result
includes `match_reasons`, source paths, and line numbers; agents should open
the cited source before making a high-risk decision.

Source line ranges account for blank lines after Markdown headings. Module
filters treat `%` and `_` literally, while bug-ID filters are exact; these
rules prevent metadata rescue from silently broadening a scoped query.

A full `--json` result row carries ~29 fields (FTS diagnostics, lifecycle
flags, raw metadata) that a browsing agent doesn't read. Add `--compact` to
trim each result to the ~8 fields actually used — `id`, `title`, `project`,
`category`, `excerpt`, `location` (source path merged with its line range or
heading), `match_reasons`, `rank` — plus `bug_id`/`status`/`stale` only when
present:

```bash
python3 endeavor_db.py query "silent failure" --compact --json
```

Markdown staleness checking is on by default. It flags results whose indexed hash no
longer matches the live source file — a sign the excerpt has drifted since
the last `sync_tracked.py` run and the cited source should be opened directly
rather than trusted as-is. It reads each distinct source file once per query;
use `--no-check-stale` only for a measured latency-sensitive call. SQLite-native records are never
stale by construction and are left unmarked:

```bash
python3 endeavor_db.py query "prompt cache" --check-stale --json
```

## Session Briefing (pack)

`bootstrap` gives the latest handoff; `pack` widens that into a fuller
session-start briefing in one call — handoff, open SQLite-native records
(`status = 'open'`, a raw filter, not lifecycle-resolved — use `record-show`/
`record-search --current-only` for a precise current-truth read), the most
recently updated `knowledge` chunks for the project, and the last 10 activity
log entries. The complete serialized response is bounded by the character
budget (default 6,000), including handoff and actionable records, so no
section can grow around the limit:

```bash
python3 endeavor_db.py pack --project DEMO_APP --json
python3 endeavor_db.py pack --project DEMO_APP --budget 3000 --json
python3 endeavor_db.py pack --project DEMO_APP --session <SESSION_ID> --json
```

The response's `truncated` field is `true` when any eligible section was cut
short. `budget_omitted_counts` reports omissions separately for session,
checkpoint, actionable/open records, warnings, knowledge, and activity.

## Semantic Search (optional MiniLM companion)

`endeavor_db.py` stays standard-library only and never imports
sentence-transformers/torch/numpy. A separate lazy-loaded process,
`embed_server.py` (127.0.0.1:8770), computes MiniLM embeddings
(`paraphrase-multilingual-MiniLM-L12-v2`, 384-dim, stored as float16 BLOBs —
~768 bytes/row) and this file talks to it over stdlib `urllib`. If the companion
can't be reached, semantic search is silently skipped and every command still
works with lexical FTS only.

The companion warms on first use and exits itself (`os._exit(0)`) after 1
hour idle to release RAM. `ingest` warms it by default (best-effort —
lexical ingest always succeeds regardless); a plain `query` never spawns it
(a cold start costs ~11s, which must not tax the common case):

Startup is serialized with a local advisory lock, so concurrent Codex and
Claude Code requests reuse one companion instead of racing to bind port 8770.
The launcher probes the CLI interpreter, active Conda environment, and local
`.venv` for a compatible Python. Set
`ENDEAVOR_EMBED_PYTHON=/path/to/python` to select one explicitly. Model loading
is cache-only: it never downloads during a
query. If the Python packages or cached MiniLM model are absent, startup fails
fast and search continues with lexical FTS only.
Restricted Codex tool sandboxes may deny Python localhost sockets and make
`embed-status` report cold despite a ready server; a `PermissionError` on
`127.0.0.1` must be verified with an unsandboxed invocation before diagnosing
missing packages or a failed companion.
`companion_warm` means the health endpoint reports `ready`, not merely that a
process is listening, and the reported model name and vector dimension must
match the configured contract. Malformed or stale embedding rows are treated
as pending and skipped safely by semantic search. If startup fails,
`embed-backfill` returns a short actionable reason from `embed_server.log`;
lexical search remains available.

Exact semantic retrieval scans all scoped vectors while the embedded index is
at most 20,000 rows, preserving zero-keyword-overlap paraphrase recall. Above
that threshold, a fresh optional HNSW sidecar searches the full embedding set
and returns the approximately highest-scoring neighborhood; ENDMEMEX then
exact-reranks at most 200 candidates. This prevents a 100k-row query from
unpacking every vector while preserving zero-keyword-overlap recall. If
`hnswlib` is absent or the per-machine sidecar is missing/stale, semantic
scoring safely falls back to bounded lexical candidates; lexical FTS remains
the complete retrieval path.
Sidecar freshness includes a monotonic SQLite vector-generation counter, so a
same-second embedding rewrite cannot leave an old ANN index marked fresh.
For native records the fallback also inspects at most 20 embedding chunks per
candidate record, so one oversized record cannot defeat the global bound.
For SQLite-native lifecycle records, a semantic match on historical wording
is resolved to and ranked under the current component head, then deduplicated;
resolved or superseded evidence is not returned as stale truth or discarded.

```bash
python3 endeavor_db.py query "some question" --semantic auto  # default: use it only if already warm
python3 endeavor_db.py query "some question" --semantic on    # spawn/wait if needed
python3 endeavor_db.py query "some question" --semantic off   # lexical only

python3 endeavor_db.py embed-status              # coverage + whether the companion is warm (never spawns)
python3 endeavor_db.py ann-status                # dependency/index/freshness (never builds)
python3 endeavor_db.py ann-build                 # after installing hnswlib in this Python env
python3 endeavor_db.py embed-diagnose            # interpreter/dependency/socket evidence (never spawns)
python3 endeavor_db.py embed-backfill             # spawn if needed, embed any missing/stale rows
python3 endeavor_db.py ingest <path> --project X --no-embed  # skip the embedding pass for this ingest

python3 endeavor_db.py embed-warm                 # start the companion now (1-hour idle exit still applies)
python3 endeavor_db.py embed-warm --keep-alive     # start it and suspend the idle-exit watchdog
python3 endeavor_db.py embed-cool                 # return a --keep-alive companion to normal idle timeout
```

`embed-warm`/`embed-cool` are a manual override of the normal on-demand warm.
Use `embed-warm` before a batch of many `--semantic on` queries so the ~11s
cold-start happens once up front instead of on the first query; a plain
`embed-warm` (no flag) still exits after the usual 1-hour idle window like any
other warm. `--keep-alive` holds the companion in RAM past that window —
useful for a long working session that will keep hitting semantic search —
but it stays resident until either `embed-cool` restores the normal timeout
or the process is stopped by hand; nothing else releases it automatically, so
pair `--keep-alive` with a deliberate `embed-cool` at the end of the session
rather than leaving RAM held indefinitely.

`embed-diagnose` is the required first step before installing dependencies,
changing Python, or restarting the companion. It reports the CLI interpreter
separately from every companion candidate, whether each candidate has the
required modules, the raw localhost error, model/cache policy, a classified
`diagnosis`, and an exact `next_action`. Never infer missing packages or a
stopped server from `companion_warm: false` alone. In particular,
`localhost_permission_denied` means the probe must be rerun with localhost
permission; it does not authorize any package or process change.

Rows are keyed by a content hash (`sha256(model_name + ":" + content)`), so a
future model swap or a changed chunk both self-heal via `embed-backfill`
without a manual migration. `evaluate` defaults to `--pipeline both
--semantic on`: it reports the Markdown-only baseline and the production
`search_all()` unified path with per-query source mix. Use `--pipeline
unified` for only production behavior and `--semantic off` for lexical-only
comparison. It probes startup once for the entire suite and reports
`semantic_available`; an unavailable companion falls back to lexical without
retrying the cold start for every case. The bundled
`developer/eval_queries.json` targets the public guide seed so a fresh clone
can run the benchmark without private project data.

Re-ingesting unchanged content with embedding enabled fills any missing,
stale, or malformed embeddings for that document. `doctor` additionally
checks foreign-key violations, exact FTS row identities, and malformed
embedding BLOBs; missing embeddings remain informational because text-only
operation is a supported fallback.

Run the regression benchmark whenever retrieval logic or seed documents
change:

```bash
python3 endeavor_db.py evaluate --json
```

Record whether a result was useful so future ranking adjustments can use real
evidence (this command does not automatically alter ranking):

```bash
python3 endeavor_db.py feedback \
  --agent codex --query "silent failure" --result 484 --useful yes \
  --note "Used the failure-type table"
```

To ingest another project memory:

```bash
python3 endeavor_db.py ingest \
  DEMO_APP/developer/PROJECT_MEMORY.md \
  --project DEMO_APP --kind project_memory
```

## SQLite-Native Records and References

Use native records when an audit and its later fix must remain connected
inside SQLite. Both relation endpoints must already exist in `memory_records`;
foreign keys reject dangling references. IDs are stable uppercase hyphenated
keys such as `AUDIT-MEM-001` and never depend on the replaceable integer IDs
in the Markdown-derived `knowledge` table.

`status` and `action_state` answer different questions. `status` describes
truth/lifecycle (`open`, `current`, `resolved`, `accepted`); `action_state`
describes work (`actionable`, `deferred`, `blocked`, `nonactionable`, `done`).
Open audits/verifications default to actionable, while decisions and ordinary
knowledge default to nonactionable. `pending` uses `action_state`, never words
such as “FIXED” in a title/body. Resolving or accepting a record defaults its
action state to `done`; override explicitly only when the workflow truly differs.

Relations are directed from the newer/asserting record to its target:

- `FIX -> resolves -> AUDIT`
- `TEST -> verifies -> FIX`
- `NEW -> supersedes -> OLD`
- `A -> contradicts -> B`
- `A -> references|duplicates -> B`

Create an audit:

```bash
python3 endeavor_db.py record-add \
  --id AUDIT-MEM-001 --project ENDMEMEX --type audit --status open \
  --title "Reference lifecycle audit" \
  --content "Typed internal references are missing." --agent codex
```

Long or multi-line content (a full audit report, non-ASCII text) does not
have to survive shell quoting: use `--content-file <path>` instead of
`--content` (`--content-file -` reads stdin). If the record documents a file
you also wrote to disk, pass `--source <repo-relative-path>` so it lands in
`metadata.source` — a later `record-show`/`query` result can then be
followed straight to that file, the same convention `knowledge` chunks use
for their own `source_path`:

```bash
python3 endeavor_db.py record-add \
  --id AUDIT-MEM-002 --project ENDMEMEX --type audit \
  --title "Full bug audit" --content-file developer/audit_2026-07-16.md \
  --source developer/audit_2026-07-16.md --agent claude
```

After implementing the fix, create the fix and its relation atomically. If a
target does not exist, the entire command fails and no partial fix record is
left behind:

```bash
python3 endeavor_db.py record-add \
  --id FIX-MEM-001 --project ENDMEMEX --type fix \
  --title "Typed references implemented" \
  --content "Added stable records, foreign-key edges, and lifecycle traversal." \
  --link resolves:AUDIT-MEM-001 --note "Validated implementation" --agent codex
```

Attach verification and inspect the complete lifecycle:

```bash
python3 endeavor_db.py record-add \
  --id VERIFY-MEM-001 --project ENDMEMEX --type verification \
  --title "Reference regression" --content "45 unit tests passed." \
  --link verifies:FIX-MEM-001 --agent codex

python3 endeavor_db.py record-show AUDIT-MEM-001 --depth 3
```

`record-show` reports `effective_status`, `is_current`,
`current_record_ids`, `has_ambiguous_current`, `conflicts_with`, and
`has_unresolved_conflict`. Expansion is capped at 1,000 records; lower it with
`--max-records`. A
`resolves` or `supersedes` edge advances the lifecycle; following an old
record therefore reaches the terminal current record. `verifies` adds
evidence without replacing the fix.

When knowledge changes, create a new record and point it to the old one with
`supersedes`; do not rewrite history. Use `contradicts` when both claims still
need review. A contradiction remains unresolved while both sides are current;
superseding or resolving one side clears that review flag without deleting
the historical edge. Lifecycle cycles and parallel successors are rejected
atomically, including writes made directly through SQLite. `resolves` accepts
only `fix -> audit`, `verifies` only `verification -> fix`, and `supersedes`
requires matching record types. Symmetric contradictions/duplicates are
stored once regardless of direction.

Normal `query` already includes current native truth. Use `record-search` when
you need native-only history or lifecycle-specific filtering:

```bash
python3 endeavor_db.py record-search "camera cleanup" --project DEMO_APP
python3 endeavor_db.py record-search "old behavior" --current-only
```

`--current-only` follows a matched historical record to its current successor,
even when the successor does not contain the old search wording. Use
`record-update` only to correct or enrich the same record; use `supersedes`
when the truth itself changed.

`record-add --link` (above) creates a new record and its relation
atomically — the common case. Use `record-link` instead when both records
**already exist** and you only now realized they should be connected (a fix
recorded separately from its audit, a later verification tying back to an
older fix, a duplicate found after the fact):

```bash
python3 endeavor_db.py record-link \
  VERIFY-MEM-002 verifies FIX-MEM-001 --agent codex \
  --note "Confirmed after the fact via record-search"
```

Same relation vocabulary and foreign-key/type/cycle validation as
`record-add --link` — both endpoints must already exist, and an invalid
combination (e.g. `resolves` where the source isn't a `fix` or the target
isn't an `audit`) is rejected the same way.

Current-head resolution uses materialized union-by-size components. Reads
follow at most logarithmic parent depth instead of recomputing a transitive
closure, searches resolve/deduplicate candidates in one SQL query, and
`doctor` validates lifecycle graphs in O(records + relations). It also reads
the real FTS inverted-index identity rather than trusting external-content
shadow row counts.

## Retiring a Document into Memory (archive-and-delete)

For the recurring request *"งานนี้ทำเสร็จแล้ว เอาไปเก็บใน ENDMEMEX แล้วลบไฟล์
.md ออก"* — a one-off guide, plan, or hand-off note whose job is done, and
whose knowledge should survive the file. Follow this instead of re-deriving
it; the two decisions below are the ones that are easy to get wrong.

### 1. Pick the destination: `record-add`, not `ingest`

| Source file after the operation | Destination |
|---|---|
| stays, and is git-tracked | `sync_tracked.py` / `ingest` → the `knowledge` table |
| **is being deleted**, or is untracked | **`record-add --type knowledge`** → `memory_records` |

`ingest` is the wrong tool for a file you are about to delete.
`sync_tracked.py --prune` builds its allowed set from git-tracked docs only
(see `_database_source_path`), so `prune_documents` deletes the rows of any
indexed document whose source is no longer tracked — the archive would
silently disappear on the next full sync. Native records are not regenerated
from Markdown and survive re-indexes; that is exactly what they are for.

### 2. Verify BEFORE deleting, not after

```bash
git log --oneline -1 -- <FILE>     # empty output = never committed
```

An untracked file has no git history, so `rm` is irreversible and the record
becomes the only surviving copy. Check in this order:

1. **Read the whole file.** Never archive a file you have only skimmed.
2. `record-add --type knowledge --status current` with the full content.
   Preserve verbatim anything that cannot be re-derived — config blocks,
   shell commands, exact flags, IDs. Prose can be tightened; payloads cannot.
3. **Round-trip the payload.** Extract the copied config/command back out of
   the stored record and compare it to the original byte-for-byte. Escaping
   is the usual casualty when text passes through JSON.
4. **Execute it if it is executable.** A command recovered from the record
   should actually run and produce the output the document claims. This is
   what turns "I copied the text" into "the archive works".
5. **Confirm it is findable:** `query "<distinctive words>"` should return it,
   and `record-search "<topic>" --current-only` should rank it.
6. Only then `rm <FILE>`, and checkpoint the deletion.

### Writing the record

Say in the first line **where it came from and why it was deleted**, so a
future reader knows this is an archive rather than a live document, and note
that it is the only copy. Then keep the source's own structure. Give it a
speaking ID (`KNOW-<TOPIC>-001`) and the project label the content belongs to
— not `ENDMEMEX`, unless the content really is about this database.

For example, an archived setup guide can retain its exact shell command and
record that the command was compared byte-for-byte and executed against mock
input before the source file was removed.

## Agent-to-Agent Messaging (mailbox convention, no new code)

Codex and Claude Code still don't message each other directly or run as
always-on listeners (see "Watching the other agent's activity live" above) —
this is a **naming convention layered on the existing durable-records
machinery** (`record-add`/`record-search`/`record-update`/`record-show`),
not a new mechanism. It gives you something `activity_log` can't: an
explicitly *addressed* note the other agent can find by searching for its own
name, with reply-threading and a read/unread status, entirely out of tools
that already exist.

**Send:** create a `knowledge` record whose title is tagged `[TO:<agent>]`.
Leave `--id` off — an ID auto-generates:

```bash
python3 endeavor_db.py record-add \
  --project ENDMEMEX --type knowledge --status open \
  --title "[TO:codex] short subject" \
  --content "message body" --agent claude
```

**Check inbox:** search for your own tag. Every result carries its `status`
in the returned JSON, so filter for `"status": "open"` client-side (no
separate status filter exists on `record-search`):

```bash
python3 endeavor_db.py record-search "[TO:codex]" --current-only
```

**Reply (threaded):** create your own `[TO:<sender>]` record and link it back
with `references` so `record-show` on either ID reveals the full thread:

```bash
python3 endeavor_db.py record-add \
  --project ENDMEMEX --type knowledge --status open \
  --title "[TO:claude] Re: short subject" \
  --content "reply body" --link references:<original-id> --agent codex
```

**Mark read/handled** (CLI or `endeavor_memory_record_update` MCP tool):

```bash
python3 endeavor_db.py record-update <id> --status resolved --agent codex
```

All of send/check-inbox map directly to the `endeavor_memory_record_add` and
`endeavor_memory_record_search` MCP tools, so this works identically whether
an agent uses the CLI or MCP. Still pull-based and asynchronous by design —
an agent has to think to check its inbox (e.g. once at `bootstrap` time), the
same way it has to think to call `handoff`; nothing here turns either agent
into a background listener. Sending mutates the local database; checking the
inbox is read-only.

## Shared Session and Checkpoint Workflow

Start a work session:

```bash
SESSION_ID=$(python3 endeavor_db.py session-start \
  --project DEMO_APP \
  --goal "Improve OCR PDF playback" \
  --agent codex)
```

Record a checkpoint after a meaningful milestone and before a context/session
limit:

```bash
python3 endeavor_db.py checkpoint \
  --session "$SESSION_ID" \
  --agent codex \
  --summary "OCR provenance implemented" \
  --work-done "Added source metadata and rewrite routing" \
  --current-state "Unit tests pass; live test remains" \
  --next-steps "Run image-only PDF through Apple Vision and production LLM" \
  --file DEMO_APP/extract.py \
  --file DEMO_APP/prep.py \
  --verify "unit 100/100 passed" \
  --status paused
```

For a task that will only ever need one session, `checkpoint --project ... --goal ...`
collapses `session-start` + `checkpoint` into a single call: it starts a session
automatically only when `--project` has no active/paused/blocked session yet
(an explicit `--session` that does not resolve is still a hard error, not
silently auto-created):

```bash
python3 endeavor_db.py checkpoint \
  --project ENDMEMEX --goal "Implement CLI ergonomics fixes" \
  --agent claude --summary "content-file/--source/--goal landed, 92/92 tests"
```

The check-then-create in `--project`/`--goal` auto-start is serialized with
`BEGIN IMMEDIATE`: two agents both sending the project's first checkpoint at
the same moment resolve to one session, not two parallel ones.

Add `--auto-files` to append git-status-detected changed files to `--file`
instead of listing every touched path by hand. It is scoped to
`ROOT/<project>/` via `git status --porcelain --untracked-files=all -- <project>`
— if the project label isn't also a real top-level directory, it contributes
no files rather than falling back to an unscoped repo-wide status (which
could pull in another concurrent agent's unrelated edits into this
checkpoint's provenance):

```bash
python3 endeavor_db.py checkpoint \
  --project ENDMEMEX --agent claude \
  --summary "agent-help + pack + compact query landed" --auto-files
```

The next agent resumes by reading:

```bash
python3 endeavor_db.py handoff --project DEMO_APP --json
python3 endeavor_db.py handoff --all-paused --json
python3 endeavor_db.py handoff --session <SELECTED_SESSION_ID> --json
```

When one or more paused sessions exist, show the queue and ask the user which
session to resume. Carry that explicit ID through `bootstrap --project ...
--session ...`, `pack`, and `checkpoint`. A project-only lookup is accepted
only when exactly one active/paused/blocked session matches; ambiguity is a
hard error rather than a recency-based guess.

With `--json`, a project that has no resumable session returns
`{"session": null, "checkpoint": null}` and exit 0 — machine callers parse
nulls instead of pattern-matching a benign stderr message. Without `--json`
(and for an explicit `--session` that fails to resolve, which is a typo, not
an empty state) it remains a hard error. `bootstrap` (see README §Start a
project session) wraps this same read together with embedding backfill and
freshness checks.

For large/structured checkpoints, pass a JSON file with `--payload`; explicit
CLI flags override fields from that file. The payload must be a JSON object:
text fields must be strings, `files_changed`/`commands_run`/`verification`
must be arrays of strings, `metadata` must be an object, `pinned` must be a
boolean, and `status` must be one of `active`, `paused`, `completed`, or
`blocked`. Unknown fields are rejected before an auto-started session can be
written. Supported payload fields are:

```json
{
  "summary": "required short summary",
  "work_done": "what is already complete",
  "current_state": "truthful state right now",
  "next_steps": "ordered continuation instructions",
  "blockers": "empty when unblocked",
  "files_changed": ["path/file.py"],
  "commands_run": ["python3 -m unittest ..."],
  "verification": ["5/5 passed"],
  "metadata": {},
  "status": "active"
}
```

Close completed work:

```bash
python3 endeavor_db.py checkpoint \
  --session "$SESSION_ID" --agent claude \
  --summary "Completed and verified" --status completed
python3 endeavor_db.py session-close \
  --session "$SESSION_ID" --agent claude --status completed
```

### Checkpoint Timeline (who did what, read-only)

`timeline` reads a checkpoint-by-checkpoint view joined to its session —
filterable by project, agent, session status, and session ID — instead of
only the single latest checkpoint `handoff` returns:

```bash
python3 endeavor_db.py timeline --project ENDMEMEX --agent claude
python3 endeavor_db.py timeline --status paused --json
python3 endeavor_db.py timeline --session <SESSION_ID> --oldest-first
```

Default output is human-readable Markdown (newest checkpoint first); pass
`--json` for the full structured record set. Each record reports both
`session_status` (the session's active/paused/completed/blocked lifecycle
state) and a derived `checkpoint_status` — `current` for the newest
checkpoint of its session, `historical` for an earlier, superseded one —
since `checkpoints` itself has no status column of its own. `--limit`
defaults to 100 and is capped at 500; `truncated: true` in the JSON means
more matching checkpoints exist than were returned. Results cover only
currently-retained checkpoints (the two caps below) and never read
`activity_log`, which is pruned far more aggressively and is not a source of
truth for this view. The same filters are exposed to MCP clients as the
read-only `endeavor_memory_timeline` tool.

## Checkpoint Quality Rules

- Write checkpoints after each material phase, after meaningful edits/tests,
  before compaction or a usage limit, and before switching agents.
- State what remains. Never label an unverified change complete.
- Include exact paths and verification results; do not paste large logs.
- Never store tokens, credentials, private keys, or secret environment values.
- Use `paused` when another agent can continue; use `blocked` only for a real
  external blocker; use `completed` only after verification.

### Checkpoint retention (two tiers)

Checkpoints are resumable context, not permanent query history, so they are
bounded on two independent axes — both visible in `stats` as
`checkpoints_cap_per_session` and `checkpoints_cap_total`:

| Tier | Limit | Scope |
|---|---|---|
| `MAX_CHECKPOINTS` | 500 | per session — keeps active work from evicting a paused session's context |
| `MAX_TOTAL_CHECKPOINTS` | 10,000 | whole table — the per-session cap alone bounds nothing, since sessions are never pruned and the ceiling was 500 × an ever-growing session count |

The global tier **never deletes the newest checkpoint of a session that is not
`completed`.** That row is exactly what `handoff` returns, and a plain
"keep the newest N" would delete the globally oldest rows first — silently
turning a session paused months ago into one whose resume context is `null`.
Exempt rows sit on top of the budget rather than consuming it, so the table
settles at 10,000 + one row per open session + every explicitly pinned row.

At the mid-2026 working rate (~28 checkpoints/day), 10,000 is close to one
year of unpinned history. The cap exists to stop unbounded growth, not to
reclaim meaningful space — the database's size is dominated by the trigram
FTS index, not by checkpoints.

Use `--pin` on checkpoint creation or `pin-checkpoint <ID>` afterward for a
checkpoint that must survive indefinitely; use `unpin-checkpoint <ID>` to
return it to normal pruning. Pinned rows never count toward either cap.
`stats` emits an advisory warning above 1,000 pins; this is not a hard limit.

## Agent Presence (who's working right now)

`agent_presence` is an **opt-in** live "who is working on what" board. Use it
only when the user explicitly asks agents to announce work or is coordinating
multiple concurrent sessions; ordinary single-session work must not create
these additional shared-database writes. When opted in, a parallel agent on
the same host can see another agent is already active on a project instead of
duplicating work:

```bash
python3 endeavor_db.py presence-start --agent claude --project <PROJECT> --task "short description"
python3 endeavor_db.py presence-heartbeat --agent claude --project <PROJECT> --task "updated description"
    # refresh, call at the same cadence as checkpoint -- do not add a new polling loop
python3 endeavor_db.py presence-stop --agent claude --project <PROJECT>
python3 endeavor_db.py presence --json [--project <PROJECT>]
```

`presence-heartbeat` only ever refreshes an **active** row. It reports
`{"updated": false}` — a no-op, not an error — when that identity either never
called `presence-start` or has already called `presence-stop`; a heartbeat
never resurrects a stopped row. Use `presence-start` again to resume (it
reactivates the same identity on conflict). A second `presence-stop` is also
an idempotent no-op: it creates no duplicate activity event or sidecar write.
Any presence row older than three days is pruned on the next start, whether
it was stopped or abandoned active; live agents heartbeat far more often.

Identity is `(machine, agent, project, instance)`, **not pid** — every CLI
call (and every MCP write, which shells out to the CLI) is its own
short-lived subprocess, so a pid cannot survive across a separate
start/heartbeat/stop sequence the way it would for a single long-lived
process. `--instance` defaults to `""` and only needs to be set to
distinguish two concurrent instances of the same agent working the same
project on the same machine at once; `--pid` is accepted but stored purely as
an informational "last process" value, never used to find the row. A row
older than `PRESENCE_STALE_SEC` (30 min — 2x the checkpoint cadence) is still
returned but marked `"stale": true`; never treat an unflagged row as proof
someone is *currently* typing, only that they checkpointed/heartbeated
recently.

**Same host is real-time**, backed directly by the WAL-mode table like
every other read/write here.

**Important: `presence-start`/`-heartbeat`/`-stop` are database writes**, just
like `checkpoint` or `session-start`; calling them faster than the checkpoint
cadence adds unnecessary write load. Every `presence-*` call also mirrors the
host's active rows to `.presence/<machine>.json`, serialized against concurrent
local writers with an advisory file lock. `presence`'s `"local"` list is the
current database's live rows; `"remote"` contains last-known snapshots copied
in through an operator-chosen transport, explicitly marked
`"source": "sidecar"` and staleness-flagged. Never treat a remote snapshot as
proof that another host is currently active or that a shared writable SQLite
deployment is safe.
`presence`/`--json` degrades gracefully (empty local list) on a database that
hasn't been migrated to the `agent_presence` table yet — no `init` required
first. `.presence/` is gitignored, per-machine local state, not committed
history.

## Sync Freshness Signal (informational, not a lock)

Every write command also mirrors "I just wrote, at this time" to
`.sync_freshness/<machine>.json` — same single-writer-per-file
pattern as the presence sidecar above, applied to every write path, not just
`agent_presence`. This is purely informational: it never blocks or gates a
write by itself. Read it with:

```bash
python3 endeavor_db.py sync-status --json
```

It is useful for observing when another host last wrote, but it never
authorizes a remote write or proves that an external synchronization transport
has caught up. A missing or old entry is informative; a recent-looking entry
is only a lower bound on how long ago that host wrote.

## Remote Write Gateway and Durable Events

`write_gateway.py` is the supported transport when another host must request
a mutation without sharing the writable SQLite file. One designated host runs
the service and remains the sole SQLite writer. Each request is
authenticated by `ENDMEMEX_GATEWAY_TOKEN` (minimum 32 characters), restricted
to an allowlist of ordinary write commands (not `ingest`, which is local-only), and persisted under an
`idempotency_key` before dispatch. A retry with the same key replays the
receipt rather than executing twice. A receipt left `processing` by a crash
fails closed and requires operator inspection/new key; it is never guessed safe
to replay.

For cross-machine binding, TLS is mandatory:

```bash
# Designated writer host (certificate/key paths are operator-managed)
ENDMEMEX_GATEWAY_TOKEN='…' python3 write_gateway.py serve \
  --bind 0.0.0.0 --port 8781 --cert /secure/server.crt --key /secure/server.key

# Remote host: queue locally, then submit; failed delivery, a terminal remote
# error, or a crash-left processing receipt remains visible in the
# machine-specific durable outbox and `flush` retries with the same key.
ENDMEMEX_GATEWAY_URL='https://writer.example:8781' ENDMEMEX_GATEWAY_TOKEN='…' \
  python3 write_gateway.py submit --idempotency-key remote:checkpoint-0001 \
  checkpoint --project DEMO --agent codex --summary 'saved remotely'
python3 write_gateway.py flush
```

Do not put the token in a record, checkpoint, command log, or repository file.
The gateway forbids `--db` and writer-host filesystem-reading payload flags in
both `--flag value` and `--flag=value` spellings.

`durable_events` closes the background-completion gap. A delegated run given
`--project` publishes one deduplicated `delegation.completed` event after its
terminal result is safely on disk. A host/orchestrator polls by monotonic ID
and acknowledges only after waking/continuing the owning conversation:

```bash
python3 endeavor_db.py event-poll --after 0 --project ENDMEMEX --json
python3 endeavor_db.py event-ack <EVENT_ID> --agent codex
```

Events are the durable handoff boundary; the host still owns the actual wake
mechanism. An unacknowledged event survives process restarts and remains
visible through the matching MCP poll/ack tools.

## Database Maintenance

`VACUUM` reclaims space from deleted/updated rows and rewrites the whole
file; `PRAGMA optimize` refreshes query-planner statistics. Both are exposed
as one manual-only command, deliberately never run automatically after a
write (unlike `activity`'s auto-refresh):

```bash
python3 endeavor_db.py maintenance --yes
```

`--yes` is required — omitting it is a hard error, not a silent no-op — since
this briefly takes an exclusive lock and rewrites the database file. Run it
only when no other agent process is
likely to be writing (e.g. between sessions), never as part of a routine
write path. `busy_timeout` still applies: if another writer holds a
transaction, this waits up to 15s and then fails cleanly with an error rather
than corrupting anything.

## MCP Server

`mcp_server.py` is a minimal stdio MCP bridge exposing `endeavor_db.py` as
tools, for MCP-capable clients instead of shelling out to the CLI directly.
It is the shared memory surface for both Claude Code and Codex.
Read-only tools (`endeavor_memory_query` — compact by default,
`endeavor_memory_pack`, `endeavor_memory_handoff`, `endeavor_memory_record_show`,
`endeavor_memory_record_search`, `endeavor_presence_list`, `endeavor_sync_status`)
always run. Write tools (`endeavor_memory_checkpoint`, `endeavor_memory_bootstrap`,
`endeavor_memory_record_add`, `endeavor_presence_start`, `endeavor_presence_heartbeat`,
`endeavor_presence_stop`) shell out to the same CLI commands, so they
inherit the same WAL/`BEGIN IMMEDIATE` concurrency guarantees as two CLI
processes racing each other.

The MCP contract is agent-facing rather than name-only: `initialize` returns
cross-tool workflow instructions; every tool declares read/write annotations,
strict argument schemas (`additionalProperties: false`), field semantics, and
its JSON-in-text result contract. Invalid or misspelled arguments return
`[error]` before CLI dispatch. `endeavor_memory_query` exposes project/category/
status/module/bug/session filters plus `semantic = auto|on|off`;
`endeavor_memory_record_search` exposes type, limit, and lifecycle-resolved
`current_only`. A stale query result means open the cited source before relying
on it. A normal empty handoff returns null session/checkpoint values, not an
error.

The public server has no username- or machine-specific write policy. Apply
read-only access through the process account, filesystem permissions, or the
client's tool policy. For remote writers, keep SQLite on one designated host
and use `write_gateway.py`.

The `agent` field on memory MCP tools and the CLI `--agent` flag accepts these
attribution identities: `codex`, `claude`, `human`, `system`, and `endeavor`.
Use `endeavor` when the Endeavor runtime itself performs the write. This is an
actor identity for audit/session attribution; it is not an additional
`agent_mcp_server.py` delegation target.

### Registration

Register each stdio server with an absolute script path. Both should use the
ENDMEMEX repository root as their working directory so they resolve the same
local `endeavor_memory.sqlite3` and run-artifact paths. The core server uses
only the standard library and does not touch the database until a tool is
called.

For Claude Code user scope:

```bash
claude mcp add --scope user endmemex -- python3 /absolute/path/to/ENDMEMEX/mcp_server.py
claude mcp add --scope user endeavor-agents -- python3 /absolute/path/to/ENDMEMEX/agent_mcp_server.py
```

Codex stores MCP registration in its user configuration. Register both
servers with absolute paths:

```bash
codex mcp add endmemex -- \
  python3 /absolute/path/to/ENDMEMEX/mcp_server.py
codex mcp add endeavor-agents -- \
  python3 /absolute/path/to/ENDMEMEX/agent_mcp_server.py
```

`codex mcp add` has no `cwd` flag. After adding `endmemex`, edit its block in
`~/.codex/config.toml` so it has the repository root as its working directory:

```toml
[mcp_servers.endmemex]
command = "python3"
args = ["/absolute/path/to/ENDMEMEX/mcp_server.py"]
cwd = "/absolute/path/to/ENDMEMEX"
```

Then run `codex mcp list` and confirm both `endmemex` and `endeavor-agents`
are `enabled`. Restart or reconnect Codex once; an already-running session
does not reload changed MCP configuration.

**After editing either MCP server or its client registration, a running client
keeps its old server/config state until it restarts or reconnects** (same file-edit ≠
process-state rule as everything else in this workspace) — newly added tools
will not appear in an already-open session.

## Agent MCP Server

`agent_mcp_server.py` is a separate stdio MCP process for managed cross-agent
delegation. It is intentionally not part of the `endmemex` memory server:
model/CLI launch can involve credentials, network access, cancellation, and a
long-running child process, so a failure or blocked launch must not take down
project-memory access.

### Agent operating recipe

Use this decision order before reading the implementation details below:

1. Do not delegate a `worker` unless the user explicitly asked for delegation,
   parallel work, or a second opinion. Reviewers and advisors remain read-only.
2. Prefer the connected `endeavor-agents` MCP server. Use
   `agent_delegate.py` directly only as the bounded fallback when that server is
   unavailable.
3. Give the cold child one self-contained task: exact deliverable, files in and
   out of scope, known constraints, and the definition of done. Never include
   secrets.
4. Start the run, retain its `run_id`, poll `status`, and relay only newly
   observed progress. The parent reproduces findings and owns command execution,
   decisions, and final verification.
5. Request `workspace_write` only for an explicitly authorized `worker` that
   must edit files. Read-only remains the default.

| Target | CLI/provider | Managed write-worker capability | Model guidance |
|---|---|---|---|
| `codex` | OpenAI Codex | Workspace-sandboxed edits and commands | Use the exact full ID `gpt-5.6-sol`, `gpt-5.6-terra`, or `gpt-5.6-luna`. |
| `claude` | Anthropic Claude Code | File edits, but no Bash | Use a full ID such as `claude-sonnet-5`; moving aliases are `fable`, `opus`, `sonnet`, and `haiku`. |
| `antigravity` | Google Antigravity (`agy`) | File edits, but no `run_command` | Use an exact slug from `agy models`; the verified copy-ready slug is `gemini-3.7-flash-medium`. |

#### Current model names and the hint source of truth

**Model catalog updated/verified: 2026-08-30.** The names below are the
current values checked against the official vendor documentation and the
installed CLIs in the verified environment. A model name is target-specific:
for example, `claude-sonnet-5` is a Claude Code model ID, while
`claude-sonnet-4-6` is an Antigravity (`agy`) slug and must not be swapped
between targets.

| Target | Exact value to put in `model` | What it is |
|---|---|---|
| OpenAI Codex | `gpt-5.6-sol` / `gpt-5.6-terra` / `gpt-5.6-luna` | Official full model IDs. Do not shorten them to `sol`, `terra`, or `luna`. |
| Claude Code | `claude-fable-5` / `claude-opus-5` / `claude-sonnet-5` / `claude-haiku-4-5-20251001` | Official full Anthropic model IDs. |
| Claude Code | `fable` / `opus` / `sonnet` / `haiku` | Official Claude Code moving aliases; use full IDs when deterministic selection is required. |
| Antigravity | `gemini-3.7-flash-medium` | Exact slug verified by a real read-only call on `agy 1.1.22`; refresh with `agy models` if the account/CLI differs. |

The verified `agy models` snapshot for `agy 1.1.22` is:

```text
gemini-3.7-flash-high
gemini-3.7-flash-medium
gemini-3.7-flash-low
gemini-3.6-flash-high
gemini-3.6-flash-medium
gemini-3.6-flash-low
gemini-3.5-flash-high
gemini-3.5-flash-medium
gemini-3.5-flash-low
gemini-3.1-pro-high
gemini-3.1-pro-low
claude-sonnet-4-6
claude-opus-4-6-thinking
gpt-oss-120b-medium
```

For Antigravity, this snapshot is a convenience list, not a permanent
allowlist: run `agy models` and copy the exact slug printed by the live CLI.
Headless mode rejects an unknown slug rather than silently selecting another
model.

The live MCP error hint is generated in
`agent_mcp_server.py`. Use this checklist whenever a vendor changes a model
name, alias, or slug. The checklist is intentionally explicit because a
model-name change has runtime, documentation, and contract surfaces.

**Model-name update checklist**

- [ ] Open the vendor's current official model documentation and the local CLI
  discovery command where applicable (`agy models`). Set `MODEL_CATALOG_DATE`
  to the verification date.
- [ ] Run a real, read-only probe for every affected target/model; record
  whether the model was accepted, the CLI version, and any account/provider
  blocker. Do not describe an account-blocked probe as a successful response.
- [ ] Update the runtime source of truth in `agent_mcp_server.py`:
  `MODEL_CATALOG_DATE`, `CODEX_MODEL_IDS`, `CLAUDE_MODEL_ALIASES`,
  `CLAUDE_MODEL_IDS`, `AGY_MODEL_SLUGS`, `AGY_RECOMMENDED_MODEL`,
  `MODEL_SELECTION_GUIDANCE`, and `START_USAGE_EXAMPLES`.
- [ ] Confirm in that same server file that `START_USAGE_HINT`,
  `SERVER_INSTRUCTIONS`, the `endeavor_agent_start` tool description, and the
  `model` schema description consume the updated catalog/examples. Malformed
  `endeavor_agent_start` arguments receive `START_USAGE_HINT`, so changing
  only this manual will not update the live error response.
- [ ] Update the human/agent help mirrors: `endeavor_db.py`
  (`AGENT_HELP_TEXT`), this manual, and `agent_delegate.py` direct-call
  examples when those examples change.
- [ ] Search for stale model literals across `AGENT.md`, `CLAUDE.md`,
  `AGENT_PROCEDURE.md`, and the source tree with `rg`; update only literals
  intended to represent the current catalog.
- [ ] Update catalog/date assertions in
  `developer/test_agent_mcp_server.py` and any direct-example tests whose
  expected model values changed.
- [ ] Regenerate (do not hand-edit)
  `developer/phase0_golden_contract.json` from the live server contract.
- [ ] Run the compatibility, ENDMEMEX MCP, and agent-delegation tests, then
  run `py_compile`, `git diff --check`, the live MCP `initialize`/`tools/list`
  smoke test, and a malformed `endeavor_agent_start` call. Verify the date,
  exact model values, examples, operating principles, and error hint all match.
- [ ] Run the real read-only model probes again after the edits when the
  catalog or payloads changed, and report successful responses separately from
  account/provider blockers.
- [ ] After changing this tracked manual, run
  `python3 sync_tracked.py ENDMEMEX_USER_MANUAL.md` and checkpoint the material
  verification phase according to the project workflow.

#### Copy-ready managed start payloads

Use one of these complete payloads as the `endeavor_agent_start` arguments.
Only change the prompt and, when needed, the target-specific `model` value.

```json
{"target":"codex","prompt":"Review the current diff; do not edit files.","role":"reviewer","access":"read_only","model":"gpt-5.6-sol","reasoning_effort":"medium","timeout":900}
{"target":"codex","prompt":"Review the current diff; do not edit files.","role":"reviewer","access":"read_only","model":"gpt-5.6-terra","reasoning_effort":"medium","timeout":900}
{"target":"codex","prompt":"Review the current diff; do not edit files.","role":"reviewer","access":"read_only","model":"gpt-5.6-luna","reasoning_effort":"medium","timeout":900}
{"target":"claude","prompt":"Review the current diff; do not edit files.","role":"reviewer","access":"read_only","model":"claude-sonnet-5","reasoning_effort":"medium","timeout":900}
{"target":"antigravity","prompt":"Review the current diff; do not edit files.","role":"reviewer","access":"read_only","model":"gemini-3.7-flash-medium","reasoning_effort":"medium","timeout":900}
```

The wrapper passes `model` through to the selected CLI; it does not maintain
an allowlist. The target CLI and authenticated account remain the final
authority for availability. Use the live discovery commands below when the
installed CLI or account differs from this dated snapshot.

The server also exposes this short operating contract in
`initialize.instructions`, so an agent can see it before choosing a tool:

- Start a worker only after explicit user authorization; use the connected
  `endeavor-agents` server and its direct wrapper only as the bounded fallback.
- Give the cold child one bounded, self-contained deliverable with exact scope,
  constraints, and done criteria.
- Keep reviewers/advisors read-only; use `workspace_write` only for an
  explicitly authorized worker.
- Never put secrets in prompts or stored run artifacts.
- Keep the returned `run_id`, poll `status`, relay only new progress, and use
  `cancel` when needed.
- The parent owns commands, decisions, and final verification. Use ENDMEMEX
  `bootstrap`/`query` before non-trivial work and `checkpoint`/`handoff` for
  every material phase or multi-phase task.
- Use memory actor `endeavor` only when the Endeavor runtime writes; keep
  SQLite writes local to one host and use the authenticated write gateway for
  remote mutations.

If `endeavor_agent_start` receives a missing, unknown, or invalid argument, it
returns `[error]` followed by a usage hint containing a valid JSON example and
the target-specific model values. Correct the payload before retrying; do not
infer that a child run started unless the response contains its `run_id`.

`role: reviewer` and `role: advisor` always force read-only access. A
`role: worker` also starts read-only unless `access: workspace_write` is
explicitly supplied.

Minimal managed lifecycle:

```text
Tool: endeavor_agent_start
Arguments:
{
  "target": "codex",
  "prompt": "Review agent_delegate.py for one concrete issue. Cite file:line evidence; do not edit files.",
  "role": "reviewer",
  "access": "read_only",
  "model": "gpt-5.6-sol",
  "reasoning_effort": "medium",
  "timeout": 900
}

Tool: endeavor_agent_status
Arguments: {"run_id": "<RUN_ID_FROM_START>"}

Tool: endeavor_agent_cancel
Arguments: {"run_id": "<RUN_ID_FROM_START>"}
```

While a run is active, remember the largest observed `progress_bytes` and
surface only progress not already relayed. Stop polling only at a terminal
status. Cancellation is for a run that should stop, not a substitute for
polling. On completion, inspect the terminal result/output and artifacts before
using the child's conclusion.

The initial surface has three tools:

- `endeavor_agent_start` preallocates a run ID, starts
  `agent_delegate.py --background`, and returns the ID immediately. If the
  launch handshake times out, the error still includes that ID so the caller
  can recover the run through `status`/`cancel`. Access defaults to
  `read_only`. Explicit `access: workspace_write` is accepted only with
  `role: worker`: Codex uses `--sandbox workspace-write`; Claude receives
  `Read`, `Grep`, `Glob`, `Edit`, and `Write` with `acceptEdits`, but no `Bash`;
  Antigravity (`agy`) receives `--mode accept-edits`, which auto-applies file
  edits but still denies shell/`run_command` since
  `--dangerously-skip-permissions` is never passed for `workspace_write` — the
  same edit-only-no-shell shape as Claude, verified live against a real `agy`
  run. Only the read tools are passed through `--allowedTools`; edit tools
  remain available through `--tools` without bare global preapproval, so
  `acceptEdits` retains the working-directory edit boundary.
  Reviewer/advisor runs always remain read-only. All MCP-launched children are
  isolated from ambient customization: Codex ignores user config/rules and is
  ephemeral, Claude uses safe mode, no session persistence, and an explicitly
  empty MCP configuration, and Antigravity uses `--new-project` (the CLI has
  no one-run flag to disable ambient MCP configuration, so inspect
  `agy mcp list`). Antigravity
  runs also always get `--add-dir <cwd>` and a `--print-timeout` derived from
  the run's `timeout`; without `--add-dir`, `agy` silently edits its own
  internal scratch directory instead of the intended working directory and
  reports success — see the `agent_delegate.py` manual section above for the
  live-verified details. The adapter also selects each CLI's JSONL/streaming
  event mode so useful model text is written incrementally rather than
  appearing only after the process exits.
- `endeavor_agent_status` polls state and returns terminal output tails and
  artifact paths when available. While a run is active it returns normalized
  `progress_tail`, `progress_bytes`, and `progress_format`; callers should poll
  and relay newly observed chunks to the user. Long blocking `wait` is
  deliberately omitted.
- `endeavor_agent_cancel` records a cancellation request; the run's own manager
  (which holds the live `Popen` object) signals and reaps the child. The control
  process never signals a PGID loaded from disk, and may return
  `cancel_pending` until the manager confirms exit. A stable guard remains the
  process-group leader until the target and descendants exit, including after
  a target exits early or a descendant ignores `SIGTERM` -- bounded by a short
  grace window (`GUARD_DRAIN_GRACE_S`, 5s) after the target's own exit, after
  which the guard best-effort SIGKILLs any leftover descendant directly and
  still returns the target's real exit code, so a completed result is never
  discarded and misreported as `timed_out` just because one descendant
  outlived it.

The adapter fixes the working directory at the repository root, does not expose
arbitrary working directories, binaries, permission modes, or checkpoints, and
puts a short timeout around each control command. The existing
`agent_delegate.py` remains the sole owner of recursion guards, model defaults,
process lifecycle, retries, artifacts, logging, and cancellation.

Admission control permits at most four live managed runs and six starts per
60 seconds across local MCP server processes. Each stdout/stderr artifact is
capped at 4 MiB; exceeding the cap terminates and reaps the child instead of
allowing unbounded disk or model-cost growth. MCP `2025-03-26` requests support
ping, incoming JSON-RPC batches, and notification no-response semantics.

`workspace_write` must be an explicit per-call choice; it is never inferred
from the prompt. Claude write workers are deliberately edit-only because the
Claude CLI does not expose a filesystem sandbox equivalent to Codex's
workspace policy. The adapter therefore does not preapprove `Edit`/`Write`
globally and relies on Claude's `acceptEdits` working-directory boundary. The
parent remains responsible for command execution and final verification for
those runs.

"Read-only" here means the child cannot edit through its granted tools; it is
not an OS-level repository-confined read sandbox. Do not delegate prompts that
may inspect host files the remote model is not authorized to read, and never
include secrets. The server negotiates MCP `2025-03-26` when available so its
tool annotations are protocol-compatible; execution failures set `isError`.

Register `endeavor-agents` in each client as shown in the MCP registration
section above. For Codex, the direct command is:

```bash
codex mcp add endeavor-agents -- \
  python3 /absolute/path/to/ENDMEMEX/agent_mcp_server.py
```

Restart or reconnect the client after registration. The tools then appear
under the `endeavor-agents` server; no memory database is opened by this MCP
adapter.

## Cross-Agent Delegation (agent_delegate.py)

A parent agent can launch any supported target CLI as a headless one-shot
sub-agent. This is the direct fallback path; prefer the managed Agent MCP
Server when it is connected:

```bash
# Claude -> Codex, using the Codex CLI configured model by default
python3 agent_delegate.py codex "summarize schema.sql" \
  --sandbox read-only

# Explicit OpenAI Codex model ID (the target CLI/account must make it available)
python3 agent_delegate.py codex "review this diff" \
  --sandbox read-only --model gpt-5.6-sol --reasoning-effort high

# Codex -> Claude (claude -p; model defaults to haiku)
python3 agent_delegate.py claude "review this diff for bugs" \
  --model claude-sonnet-5 --allowed-tools Read Grep

# Structured, validated result with one bounded transient retry
python3 agent_delegate.py claude "return a JSON audit" \
  --model claude-sonnet-5 --role reviewer --expect-json --min-output-chars 2 \
  --retries 1 --result-format json

# Claude/Codex -> Antigravity (agy -p; read-only by default).
# Use `agy models` first and pass a slug available to the current account.
python3 agent_delegate.py antigravity "summarize schema.sql" \
  --model gemini-3.7-flash-medium

# Antigravity write worker: file edits auto-apply, shell stays denied
python3 agent_delegate.py antigravity "append a TODO note to NOTES.md" \
  --sandbox workspace-write --model gemini-3.7-flash-medium
```

#### Model selection and live discovery

The wrapper does not translate or validate model names; it passes `--model` to
the target CLI. Availability depends on the installed CLI and authenticated
account, so do not freeze per-machine defaults or CLI versions into durable
instructions. The dated exact-value catalog and update checklist are in
[Current model names and the hint source of truth](#current-model-names-and-the-hint-source-of-truth).

| Target | Managed MCP with omitted `model` | Direct wrapper with omitted `--model` | Explicit selection |
|---|---|---|---|
| Codex | The MCP adds `--isolated`; Codex ignores user config/rules, so its user-configured model is not the managed default. | Uses the Codex CLI configuration. | Use the exact full ID `gpt-5.6-sol`, `gpt-5.6-terra`, or `gpt-5.6-luna`, if accepted by the current CLI/account. |
| Claude Code | The wrapper explicitly supplies `haiku`. | The wrapper explicitly supplies `haiku`. | Use a full ID such as `claude-sonnet-5`; moving aliases are `fable`, `opus`, `sonnet`, and `haiku`. |
| Antigravity (`agy`) | No wrapper model default; `agy` selects its configured/default model. MCP isolation uses `--new-project`, but the CLI has no one-run flag to suppress ambient MCP configuration. | No wrapper model default; `agy` selects its configured/default model. | Run `agy models` for the live list, then pass an exact slug reported for the current account; the verified snapshot recommends `gemini-3.7-flash-medium`. |

Run live discovery instead of trusting a dated snapshot:

```bash
python3 agent_delegate.py diagnose codex
python3 agent_delegate.py diagnose claude
python3 agent_delegate.py diagnose antigravity
agy models
claude --help
```

`diagnose` reports the resolved executable and installed version; it never
spawns a model. The target CLI remains the authority for whether an explicit
alias/ID is available to the current account. For managed Codex runs, pass an
explicit model whenever exact model selection is part of the task contract.

Sources: [OpenAI API Models](https://developers.openai.com/api/docs/models),
[OpenAI latest-model guidance](https://developers.openai.com/api/docs/guides/latest-model),
and [Anthropic Claude Code CLI usage](https://docs.anthropic.com/en/docs/claude-code/cli-usage).
The wrapper's `haiku` default is an implementation default, not a claim about
which dated Claude model Anthropic currently maps that alias to.

Key behavior:

- **Recursion guard** — `ENDEAVOR_DELEGATE_DEPTH` is set on the child;
  a sub-agent trying to delegate again is refused (exit 2), so
  Claude→Codex→Claude loops cannot happen. Max depth is 1.
- **Always file-logged; project-scoped runs emit one durable event** — every delegation appends one
  JSON line to `.agent_delegate_log.jsonl` (timestamp, caller,
  target, model/role, run ID, exit code, duration, artifact paths/checksums,
  output tail). Each run also has an atomic state/request/result envelope and
  disk-streamed stdout/stderr under ignored
  `.agent_delegate_runs/<RUN_ID>/`. These are plain file writes,
  safe as local artifacts, and avoid holding unbounded child output in memory.
  Starting a run also ages out old run directories (`RUN_DIR_MAX_AGE_DAYS`,
  14 days) — but only ones whose `state.json` shows a terminal status, plus
  directories with no state at all that are equally cold. Anything still
  queued/running, anything recent, and anything whose state cannot be parsed
  is left alone, and a failure to prune can never block a run from starting.
  The `.jsonl` log is the durable record; the run directories are debugging
  material with a shelf life. When `--project <P>` is supplied, terminal
  completion also publishes a deduplicated `delegation.completed` event for
  the host; without `--project`, status/wait polling remains required.
- **Opt-in checkpoint** — add `--checkpoint --project <P>` to also record
  a real checkpoint in the local ENDMEMEX database.
- **Sub-agents start cold** — the child knows nothing about the parent
  session. Put the needed context in the prompt, or tell it to read the
  ENDMEMEX handoff (`endeavor_db.py handoff --project <P> --json`).
- **Headless stdin boundary** — foreground children and background managers
  receive a closed stdin (`DEVNULL`). This prevents a target CLI from consuming
  unrelated piped parent input or hanging while waiting for an inherited pipe.
- **Explicit roles** — `--role worker` preserves the original prompt;
  `reviewer` and `advisor` prepend a read-only evidence/critique contract and
  enforce it at runtime: Codex and Antigravity must use `--sandbox read-only`,
  while Claude is limited to `Read`, `Grep`, and `Glob` tools (or no tools).
- **Antigravity (`agy`) sandbox translation and the `--add-dir` requirement** —
  the wrapper always injects `--add-dir <cwd>` and `--print-timeout <timeout>s`
  for antigravity runs. **`--add-dir` is not optional**: verified live, without
  it `agy` silently operates on its own internal scratch directory
  (`~/.gemini/antigravity-cli/scratch/`) instead of `--cwd` and reports
  `status:"SUCCESS"` having edited a completely different file, with no error
  — a silent-wrong-file failure mode, not a missing feature. `agy` has no
  dedicated read-only mode value; `--sandbox read-only` (the default) omits
  `--mode` entirely, which leaves `agy`'s default request-review posture —
  verified live, a denied write/shell attempt there returns exit 1 with
  `status:"ERROR"` and a `permission check failed` message, not a silent
  no-op. `--sandbox workspace-write` adds `--mode accept-edits` (file edits
  auto-apply; shell/`run_command` is still denied — verified live). `--sandbox
  danger-full-access` additionally adds `--dangerously-skip-permissions`,
  which approves everything including shell; only use it in a disposable
  sandbox. `--model`/`--reasoning-effort` map to `agy`'s own `--model`/
  `--effort low|medium|high`, which matches this wrapper's convention
  directly. `--isolated` maps to `--new-project`; the CLI exposes no flag to
  disable ambient MCP configuration for one run, so that part of the isolation
  promise is a known gap. Inspect `agy mcp list` before relying on an
  ambient-MCP-free run.
- **Future-compatible model selection** — `--model` is a free-form alias/full-ID
  passthrough for all three target CLIs, with no vendor-model allowlist. When
  omitted, Claude uses the wrapper's `haiku` default, a direct Codex wrapper run
  uses its CLI configuration, and Antigravity uses its own configured/default
  model. An isolated managed Codex run ignores user configuration, so supply an
  explicit model when that choice must be deterministic. `advise` likewise
  accepts arbitrary values through
  `--worker-model` and `--advisor-model`; `sonnet` and `opus` are only defaults.
  The wrapper deliberately does not maintain a model catalog or preflight model
  availability: the target CLI validates an explicit alias/ID, and an unknown
  model is returned as an ordinary child failure with the normal run artifacts.
  Use a full model ID documented for the target CLI/account rather than an
  undocumented short string. An unknown model is an expected target-CLI
  validation failure, not a wrapper bug: `--model` forwards exactly the value
  given.
- **Reasoning effort** — optional `--reasoning-effort` is also a passthrough:
  it becomes Codex's `model_reasoning_effort` configuration or the target
  CLI's `--effort` flag for Claude and Antigravity. When omitted, the wrapper
  explicitly selects `medium` for all three targets; an explicit value is
  validated by the target CLI. For example, use `--model gpt-5.6-sol` with
  `--reasoning-effort high` for a read-only Codex advisor.
- **Bounded retry** — `--retries N --retry-delay S` retries only timeout and
  recognized transient service/network failures. Authentication, recursion,
  validation, and ordinary child failures are never retried blindly.
- **Validation** — `--expect-json`, `--expect-regex`, and
  `--min-output-chars` turn an invalid successful-looking response into exit 4.
- **Diagnostics** — `diagnose claude|codex|antigravity` reports caller/depth
  plus the exact executable path and version. Antigravity's binary is `agy`,
  not `antigravity`; the wrapper resolves this automatically via
  `TARGET_BINARIES`. `--binary /full/path` resolves machines with multiple CLI
  installations. A Codex-side Claude `Not logged in` failure is classified as
  `sandbox_credential_unavailable` with an instruction to retry outside the
  sandbox before asking the user to log in again.
- Exit codes: child's own code; `2` = recursion refused; `3` = target/run not
  found; `4` = output validation failed; `124` = timeout; `126` = launch
  failure; `130` = cancelled.
- Useful flags: `--model` selects any model alias/full ID understood by the
  target CLI; `--json` requests machine-readable *child* output;
  `--result-format json` wraps any child in a stable runtime envelope;
  `--cwd`, `--permission-mode` (Claude), `--caller`, `--parent-record`.
  For Claude, `--available-tools` sets the exact `--tools` availability list;
  `--allowed-tools` is the narrower preapproval list passed to
  `--allowedTools`.

### Background runs and parallel work

Start any number of sibling runs from the parent agent; never ask a delegated
child to delegate again (the depth cap still applies):

```bash
python3 agent_delegate.py claude "audit module A" \
  --model sonnet --role reviewer --background
python3 agent_delegate.py claude "audit module B" \
  --model sonnet --role reviewer --background

python3 agent_delegate.py status <RUN_ID> --json
python3 agent_delegate.py wait <RUN_ID> --timeout 900 --json
python3 agent_delegate.py cancel <RUN_ID>
```

`status` includes the phase/attempt, child PID, artifact paths, and last log
activity. A missing background worker becomes `worker_died` instead of staying
queued forever. `cancel` targets only the resolved child process group, forwards
advisor-group cancellation to the active worker/advisor, and escalates from
SIGTERM to SIGKILL after a bounded grace period. A pre-spawn cancellation may
briefly report `cancel_pending` until the manager reaches its safe point.

### Worker + advisor (wrapper defaults: `sonnet` → `opus`)

The advisor command keeps role attribution explicit and runs the configured
models as sibling children of the parent (not nested delegation). The example
shows the defaults, but both model arguments are free-form passthrough values:

```bash
python3 agent_delegate.py advise "audit ENDMEMEX for concrete bugs" \
  --worker-model sonnet --advisor-model opus \
  --allowed-tools Read Grep Glob --result-format json
```

The configured worker (`sonnet` alias by default) performs the bounded task
first. Its artifact is passed to the configured advisor (`opus` alias by
default) with a read-only
`accept`/`reject`/`needs-evidence` review contract. The group envelope contains
both run IDs and full attribution. The parent agent remains responsible for
reproduction, decisions, edits, and verification; model votes never replace
evidence. The group run ID is printed to stderr immediately so a second
terminal can inspect or cancel the group while a phase is running.

Command-construction and managed-run tests cover all target adapters and
free-form model forwarding; they do not assert that every supplied future
model ID exists. The wrapper invokes Codex non-interactively and selects its
safety boundary through `-s/--sandbox`.
