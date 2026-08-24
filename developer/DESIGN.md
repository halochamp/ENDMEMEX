# ENDEAVOR Memory — Detailed Design

Status: implemented design, schema version 12
Audience: maintainers of `ENDMEMEX`, Codex, and Claude Code
Runtime entry point: `endeavor_db.py`

## 1. Purpose

ENDEAVOR Memory is the shared local persistence layer for Codex and Claude
Code in the ENDMEMEX workspace. It provides three related but
deliberately separate kinds of memory:

1. Searchable knowledge derived from reviewed Markdown.
2. Durable SQLite-native audit, fix, verification, decision, and knowledge
   records with stable internal references.
3. Resumable agent sessions and bounded checkpoints for handoff between
   agents.

The system is SQLite-first for indexing and continuity, but it does not make
SQLite the authority for every kind of information. Human-readable project
documents remain authoritative for Markdown-derived knowledge. SQLite-native
`memory_records` are the explicit exception: their stable IDs and typed
relations are authoritative inside the database.

## 2. Design Goals

- Preserve provenance from every Markdown result back to a source file,
  heading, and line range.
- Make lexical search the complete, always-available retrieval path.
- Treat semantic search as an optional quality enhancement, never as a
  database availability requirement.
- Make repeated ingestion idempotent and safe.
- Support concurrent Codex and Claude processes on one Mac.
- Preserve audit history while resolving an old claim to current truth.
- Keep checkpoint and relation traversal bounded as the database grows.
- Fail closed on invalid lifecycle relationships, dangling references,
  cycles, and ambiguous successors.
- Keep read-only commands read-only: they must not migrate the database or
  acquire a write lock.
- Expose enough diagnostics that an agent does not confuse a sandbox policy,
  a missing Python package, and a stopped companion process.

## 3. Non-Goals

- ENDEAVOR Memory is not a distributed database.
- It does not safely support concurrent writes from multiple hosts through a
  filesystem-sync service.
- It is not a filesystem watcher. SQLite triggers do not read Markdown files.
- It does not replace Git history or human-readable project documentation.
- It does not require MiniLM, Torch, FastAPI, or network access for lexical
  storage and search.
- It does not store every query automatically.
- It does not require an approximate-nearest-neighbor dependency. HNSW is an
  optional per-machine sidecar; lexical retrieval remains complete when the
  dependency/index is missing, stale, or incompatible.

## 4. Directory Layout

```text
ENDMEMEX/
├── README.md                    operator quick start and command reference
├── endeavor_db.py               stdlib-only CLI, storage, retrieval, lifecycle
├── schema.sql                   runtime schema, indexes, and SQLite triggers
├── sync_tracked.py              curated Git-tracked Markdown synchronization
├── embed_config.py              lightweight shared embedding contract
├── embed_server.py              optional FastAPI MiniLM companion
├── endeavor_memory.sqlite3      local runtime database; Git-ignored
└── developer/
    ├── DESIGN.md                this canonical developer design
    ├── eval_queries.json        retrieval regression cases
    └── test_endeavor_db.py      fast, hermetic unit/regression suite
```

`graphify-out/`, SQLite sidecars, logs, locks, and `__pycache__` are generated
runtime/developer artifacts. They are not source-of-truth design files.

## 5. High-Level Architecture

```text
Reviewed Git-tracked Markdown
          │
          ▼
 sync_tracked.py / ingest
          │ hash + chunk + metadata
          ▼
 documents ──1:N── knowledge ──SQLite triggers──► 3 knowledge FTS indexes
                          │
                          └──best effort──► float16 MiniLM embeddings

Direct SQLite-native writes
          │
          ▼
 memory_records ──typed edges──► memory_relations
          │                          │
          ├──► memory_records_fts    └──► materialized lifecycle components
          └──────────────────────────────► current truth resolution

Agent continuity
          │
          ▼
 sessions ──1:N── checkpoints ──► handoff

query
  ├──► multi-pass Markdown retrieval
  ├──► optional exact semantic / HNSW candidate pass
  └──► current SQLite-native truth
              │
              ▼
          ranked results with provenance and match reasons

Background completion ──► durable_events ──poll/ack──► host wake/continuation
Remote outbox ──authenticated HTTPS/idempotency──► designated-host write gateway
```

## 6. Process Boundaries

### 6.1 Main CLI process

`endeavor_db.py` intentionally uses only the Python standard library. It owns:

- SQLite connection and migration management.
- Markdown chunking and metadata extraction.
- Lexical retrieval, RRF fusion, filtering, and reranking.
- Session/checkpoint operations.
- SQLite-native record and relation operations.
- Embedding vector serialization and the HTTP client for the companion.
- Health, integrity, FTS identity, lifecycle, and embedding diagnostics.

This boundary allows plain lexical operations to run even when the active
`python3` does not contain ML packages.

### 6.2 Embedding companion process

`embed_server.py` is a separate FastAPI/Uvicorn process bound to
`127.0.0.1:8770`. It imports `sentence_transformers` and owns the MiniLM model.
The main CLI communicates with it over HTTP and never imports Torch or
SentenceTransformers itself.

The companion process is the warm-state boundary: a loaded model exists only
while that process is alive.

### 6.3 Synchronization process

`sync_tracked.py` discovers reviewed, human-authored, Git-tracked Markdown and
invokes the CLI ingestion path with `--no-embed`. Its responsibility is
lexical freshness. Embedding freshness is handled separately by
`embed-backfill`.

## 7. Database Opening and Migration

The default database is `endeavor_memory.sqlite3`. It may be
overridden with `--db` or `ENDEAVOR_DB_PATH`.

Writable connections configure:

- `PRAGMA foreign_keys = ON`
- `PRAGMA journal_mode = WAL`
- `PRAGMA synchronous = NORMAL`
- `PRAGMA busy_timeout = 15000`
- SQLite connection timeout of 15 seconds

Read-only commands open `file:<path>?mode=ro`, set `query_only`, and do not run
schema initialization. The read-only set includes query, stats, doctor,
evaluate, embed-status, record reads/searches, and handoff.

Write commands call `initialize()` before their operation. Migration uses
`BEGIN IMMEDIATE`, executes `schema.sql` without `executescript`'s implicit
commit behavior, rebuilds derived structures when required, writes the schema
version last, and rolls back the entire migration on failure.

The early return when `database_meta.schema_version` already equals the code's
`SCHEMA_VERSION` makes repeated initialization effectively constant-time.

## 8. Storage Model

### 8.1 `database_meta`

Stores schema/index contract versions and their update timestamps. A version
marker is written only after the complete migration succeeds.

### 8.2 `documents`

One row represents one source Markdown file:

- Stable uniqueness: `source_path`.
- Idempotency: SHA-256 `content_hash` plus `index_version`.
- Routing: `project` and `kind`.
- Provenance: title, source mtime, and import time.

If a source changes, ingestion updates this row and replaces all derived
`knowledge` rows for that document in one transaction.

### 8.3 `knowledge`

One row represents one heading-aware Markdown chunk. It stores:

- Search text: title, content, and tags.
- Routing metadata: project, category, status, bug ID, module, and session.
- JSON metadata containing every extracted module/bug identifier.
- Parent heading for result diversity.
- Exact source path, heading, and line range.
- Optional float16 embedding BLOB and content/model hash.

`knowledge.id` is replaceable because re-ingestion deletes and recreates
chunks. It must never be used as a durable relation target.

### 8.4 Knowledge FTS indexes

Three external-content FTS5 indexes cover different retrieval failures:

| Index | Tokenizer | Role |
|---|---|---|
| `knowledge_fts` | Unicode61 | normal exact/prefix/all-term and broad matching |
| `knowledge_fts_porter` | Porter + Unicode61 | English stemming |
| `knowledge_fts_trigram` | case-insensitive trigram | substring and identifier fragments |

Insert/update/delete triggers keep all three indexes synchronized with
`knowledge`. Separate `fts5vocab(..., 'instance')` tables expose the real
inverted-index document identities for `doctor`; reading an external-content
FTS table directly could mask a stale index by falling back to the content
table.

### 8.5 `query_feedback`

Stores explicit usefulness feedback, selected result identifiers, notes, and
agent identity. Feedback is evidence for a later reviewed ranking change. It
never modifies ranking automatically.

### 8.6 `sessions`, `checkpoints`, and `activity_log`

`sessions` stores a shared goal, state, initiating agent, last agent, and
timestamps. Valid states are active, paused, completed, and blocked.

`checkpoints` stores resumable state: summary, completed work, current state,
next steps, blockers, changed files, commands, and verification. Sequence is
unique within a session. Retention keeps 500 unpinned checkpoints per session
and 10,000 unpinned checkpoints globally. The newest checkpoint of every
non-completed session and every explicitly pinned checkpoint are exempt.

`activity_log` records lightweight write events such as ingest, checkpoint,
record creation, and relation creation. It is not a raw query log.

### 8.7 `memory_records`

SQLite-native records use stable uppercase hyphenated IDs, for example
`AUDIT-ENDEAVOR-MEMORY-001`. Supported types are:

- audit
- fix
- verification
- decision
- knowledge

The explicit status is open, current, resolved, or accepted. Superseded and
resolved lifecycle state may also be computed from incoming relations rather
than being manually written onto the historical record.

Unlike Markdown-derived knowledge, these records are authoritative inside
SQLite and are safe durable relation endpoints.

### 8.8 `memory_relations`

Relations are directed from the asserting/newer record to the target:

| Relation | Required shape | Lifecycle effect |
|---|---|---|
| `references` | any existing record to another | none |
| `resolves` | fix → audit | advances current truth |
| `verifies` | verification → fix | evidence only |
| `supersedes` | same record type → same record type | advances current truth |
| `contradicts` | any two existing records | conflict evidence |
| `duplicates` | matching record types | duplicate evidence |

Foreign keys reject dangling endpoints. SQLite triggers enforce typed edge
shape, current-source/current-target requirements, no lifecycle cycle, and no
parallel successor. Contradicts and duplicates are symmetric and stored in a
canonical direction with a unique symmetric index.

### 8.9 Materialized lifecycle components

`memory_components` and `memory_record_components` materialize connected
`resolves`/`supersedes` histories. Every new record begins in a one-record
component. Adding a lifecycle edge unions the two components by size and sets
the root's `current_id` to the newer/asserting source.

Union-by-size bounds parent depth to O(log N), normally zero to two levels,
instead of running a recursive transitive closure for every query. Reads can
resolve many record IDs to current heads in batches.

### 8.10 `memory_records_fts`

Provides full-text retrieval over native record ID, project, type, title,
content, and status. Triggers synchronize it with `memory_records`.

## 9. Markdown Knowledge Write Path

Markdown is not pulled into SQLite by a trigger. The required flow is:

```text
agent edits reviewed .md
        │
        ├── explicit: python3 sync_tracked.py
        └── advisory: local Git pre-commit hook calls the same sync
                         │
                         ▼
                   ingest_markdown()
```

### 9.1 Source discovery

`sync_tracked.py` asks Git for tracked `*.md` files. It excludes generated
directories, virtual environments, third-party notices, selected source
libraries/translations, and large prompt-baseline snapshots. Project and kind
are derived deterministically from the repository-relative path.

The local pre-commit hook is advisory. Its source of truth is Git-tracked at
`hooks/pre-commit`; the live `.git/hooks/` copy is installed
with `endeavor_db.py install-hooks` and `doctor` reports
`installed`/`differs`/`missing` per hook so drift cannot stay silent. A source
edit that is not committed is not automatically synchronized; the agent must
run `sync_tracked.py` explicitly.

### 9.2 Idempotency decision

Ingestion reads raw bytes, decodes UTF-8, and computes SHA-256. The source is
unchanged only when content hash, index version, project, and kind all match.
An unchanged document is not rechunked, but missing/stale embeddings may still
be repaired when embedding is enabled.

### 9.3 Chunking and metadata

The Markdown parser creates heading-aware chunks and preserves source line
ranges. Oversized paragraphs are hard-split at grapheme-safe boundaries so a
chunk never exceeds the configured 500-character bound without stranding a
Thai combining mark at the start of the next chunk. Classification and metadata extraction
derive category, status, bug identifiers, module names, session labels,
parent headings, and tags.

### 9.4 Atomic lexical replacement

The write transaction upserts the `documents` row, deletes old chunks for the
document, inserts all new chunks, and writes an activity event. FTS triggers
update the three lexical indexes inside the same transaction. A failure rolls
back the complete lexical replacement.

### 9.5 Best-effort embedding after commit

Embedding occurs only after lexical ingestion commits. Companion/package/model
failure can therefore never erase or roll back searchable lexical knowledge.

## 10. SQLite-Native Record Write Path

Audit/fix/verification records that need durable references are written
directly through `record-add`, `record-update`, and `record-link`.

`record-add --link ...` creates the record and all requested relations in one
transaction. If any target or relation is invalid, the new record and its
links are rolled back together.

Truth status and work state are orthogonal columns. `status` participates in
the record lifecycle; `action_state` is one of actionable/deferred/blocked/
nonactionable/done and alone drives normal pending-work triage. Migration is
type/metadata based and deliberately never interprets prose such as “FIXED”.

Historical truth is not rewritten when the truth changes. The preferred flow
is:

```text
AUDIT (open)
  ▲
  └── FIX resolves AUDIT
        ▲
        └── VERIFICATION verifies FIX

NEW DECISION supersedes OLD DECISION
```

`record-update` is reserved for correcting/enriching the same assertion.
`supersedes` represents a changed assertion.

## 11. Session and Checkpoint Write Path

`session-start` creates a project-scoped goal. A project-only lookup succeeds
only when exactly one active/paused/blocked session matches; otherwise callers
must carry the selected session ID through handoff, bootstrap, pack, and
checkpoint. A checkpoint then enters `BEGIN IMMEDIATE` before allocating
the next sequence number. This prevents two agents from reading the same
`MAX(sequence)` and attempting the same next sequence concurrently.

The checkpoint insert, global retention cleanup, session state/last-agent
update, and activity event commit atomically. A completed session cannot be
reopened by a later checkpoint. `handoff` returns the selected session and its
latest checkpoint without writing.

### 11.1 Checkpoint Timeline Read Path

`timeline` (CLI subcommand and the read-only `endeavor_memory_timeline` MCP
tool) is a single parameterized join of `checkpoints` to `sessions`, filtered
by any combination of project, agent, session status, and session ID, with
every selected column explicitly aliased to avoid the two tables' overlapping
names (`id`, `created_at`, `metadata`). `checkpoints` carries no status column
of its own, so each record's `checkpoint_status` is derived per row —
`current` when a checkpoint's `sequence` equals `MAX(sequence)` for its
session, `historical` otherwise — while `session_status` is the session's own
active/paused/completed/blocked lifecycle state (the same enum `checkpoint
--status`/`session-close --status` already write). Default ordering is
`created_at DESC, id DESC`; `--oldest-first` reverses both keys together so
the tiebreak stays deterministic in either direction. `limit` defaults to 100
and is clamped to a maximum of 500; the response's `truncated` flag reports
whether more matching rows exist than were returned, alongside `count`,
`total_matching`, `order`, `filters`, and a `retention_notice` built from the
caller-supplied per-session/global caps (never a hardcoded string, so a test
that monkeypatches the caps still gets a truthful notice).

This function takes those caps as explicit keyword parameters rather than
importing them from `config`, keeping it inside the same transitively-clean
set as the rest of `sessions.py` (see that module's docstring) — the
`endeavor_db.py` facade resolves the real `MAX_CHECKPOINTS`/
`MAX_TOTAL_CHECKPOINTS` and passes them in. `activity_log` is never consulted:
it is pruned far more aggressively than checkpoints and is not a source of
truth for this view. The CLI connects read-only for `timeline` like every
other read command in this section; the Markdown renderer is a pure function
over the already-built result dict (no additional queries) and only emits a
`file://` link for a `files_changed` entry that actually resolves to an
existing path, leaving anything else as plain text rather than guessing.

## 12. Lexical Retrieval Pipeline

The query path normalizes whitespace, extracts at most 12 non-stopword terms,
adds reviewed Thai/English aliases, and produces FTS expressions. ASCII terms
of at least four characters use prefix matching; Thai terms remain exact.

Each FTS pass returns at most 30 candidates. BM25 uses field weights 4.0 for
title, 1.0 for content, and 0.8 for tags.

| Pass | Weight | Purpose |
|---|---:|---|
| all terms, Unicode FTS | 1.6 | high-precision lexical match |
| any term, Unicode FTS | 0.8 | broad recall |
| all terms, Porter FTS | 1.2 | English morphology |
| trigram phrase | 0.9 | substring/identifier recovery |
| metadata rescue | 1.5 | punctuation-heavy bug/module/session IDs |

Each pass contributes weighted reciprocal rank:

```text
pass contribution = pass_weight / (60 + rank_in_pass)
candidate RRF score = sum(all pass contributions)
```

Ranking by rank rather than raw BM25/cosine magnitude makes independent
retrieval strategies comparable.

Deterministic boosts are applied after fusion:

- Exact normalized query inside title: +0.45.
- Every query term present in title+content: +0.25.
- Training-method intent/category match: +0.50.
- Other recognized intent/category match: +0.22.
- Resolved knowledge: +0.12.
- Accepted historical knowledge: -0.05.
- Bug identifier term match: +0.35.

Final selection removes duplicate title/content pairs and allows at most two
results from the same document parent heading. Ties fall back to stable row ID
ordering.

Every result exposes `match_reasons`; a rank is evidence of retrieval, not
proof that the content is correct.

## 13. Semantic Retrieval Pipeline

### 13.1 Vector contract

- Model: `paraphrase-multilingual-MiniLM-L12-v2`.
- Dimension: 384.
- Stored form: normalized float16 little-endian BLOB, 768 bytes per row.
- Content identity: SHA-256 of `model_name + ':' + content`.
- HTTP batch size: 256 texts.

A malformed BLOB or stale content/model hash is treated as pending and is not
used for ranking.

### 13.2 Companion lifecycle

`ensure_embed_server()` first reuses a compatible ready service. Startup is
serialized by `.embed_start.lock` so concurrent agents do not race to bind
port 8770. The launcher probes interpreters independently of the CLI's
`sys.executable`; `ENDEAVOR_EMBED_PYTHON` is an authoritative override.

The server loads the model with `local_files_only=True`. It never turns a
query into an implicit download. The process exits after one hour without an
active request, releasing model RAM.

### 13.3 Query modes

- `auto`: use semantic only if the companion is already ready; never spawn.
- `on`: spawn/wait when necessary.
- `off`: lexical only.
- Internal `ready`: evaluation already performed the one-time warm probe.

When scoped embedded rows are at most 20,000, the query vector is compared
against every valid scoped embedding to preserve zero-keyword-overlap recall.
Above 20,000, a fresh optional HNSW sidecar searches the complete vector set
and supplies at most 200 high-scoring candidates for exact cosine reranking.
The sidecar is machine-specific, snapshot-pinned by counts/latest embedding
timestamps plus a monotonic SQLite vector generation, and never trusted when
stale. This catches same-second vector rewrites without hashing the full corpus
on every query. Missing/incompatible HNSW safely
falls back to lexical candidate IDs; native fallback also caps each candidate
record at 20 embedding chunks.
SQLite-native scores are transferred from matched historical component
members to their materialized current heads and deduplicated by head before
ranking, so old terminology remains discoverable without returning stale truth.

Cosine similarity is a dot product because stored and query vectors are
normalized. The top 30 semantic rows contribute `1.0 / (60 + semantic_rank)`
to the same RRF score used by lexical passes.

### 13.4 Backfill

`embed-backfill` finds rows with no vector, invalid BLOB length, or a stale
embedding hash. It embeds in bounded batches and updates only those rows.
Changing the model name automatically invalidates old hashes and makes the
next backfill self-heal them.

### 13.5 Diagnostics hard gate

`embed-diagnose` reports:

- CLI Python and version.
- Every candidate companion Python.
- Required-module availability per candidate.
- Selected companion interpreter.
- Raw health error type, errno, and message.
- Model identity and cache-only policy.
- Classified diagnosis and exact next action.

`companion_warm=false` alone never proves missing packages or a stopped
server. `localhost_permission_denied` requires the same probe outside the
sandbox or with localhost permission. No package installation, interpreter
change, or restart is authorized from that diagnosis.

## 14. Unified Search Across Both Knowledge Stores

Normal `query` searches Markdown-derived `knowledge` and current heads of
SQLite-native `memory_records`, unless a filter is specific to Markdown.

Each store first produces its own ranked list. A second RRF layer contributes
`1.0 / (60 + rank)` per store. SQLite-native current truth wins an exact tie,
then stable identifiers provide deterministic ordering.

Native results are marked with:

- `source_path = SQLite:memory_records`
- stable record ID as source heading
- `match_reasons = [sqlite_native, current_truth]`

Markdown results retain source file and line provenance. Consumers must not
treat the two ID domains as interchangeable.

## 15. Lifecycle Read Semantics

Reading any native record resolves its materialized component root and returns:

- `current_record_ids`
- `is_current`
- `effective_status`
- `has_ambiguous_current`
- `conflicts_with`
- `has_unresolved_conflict`

`record-show` expands relations breadth-first to a maximum requested depth and
a hard cap of 1,000 records. SQL `IN` clauses are batched (normally 500
parameters) so large relation contexts stay below practical SQLite limits.

`record-search --current-only` may match a historical record by its old words,
resolve it to its current head, and return the head even when the new content
does not repeat the old wording.

`doctor` validates lifecycle graphs in O(records + relations) using Kahn's
algorithm, instead of enumerating a transitive closure.

## 16. Trigger and Hook Responsibilities

These mechanisms must not be confused:

| Mechanism | Runs where | Responsibility |
|---|---|---|
| Git pre-commit hook | outside SQLite | advisory call to synchronize changed tracked Markdown |
| `sync_tracked.py` | Python process | discover and explicitly ingest reviewed Markdown |
| knowledge FTS triggers | SQLite | mirror inserted/updated/deleted chunks into three FTS indexes |
| native FTS triggers | SQLite | mirror native records into `memory_records_fts` |
| relation validation trigger | SQLite | enforce typed edges/current endpoints/no cycles |
| lifecycle union triggers | SQLite | maintain materialized current-head components |

No SQLite trigger monitors the filesystem. An uncommitted Markdown edit
requires an explicit sync.

## 17. Concurrency and Transaction Boundaries

Supported concurrency is multiple local processes on the same host:

- WAL allows readers while a writer is active.
- Busy timeout absorbs short writer overlap.
- Foreign keys protect endpoint integrity.
- `BEGIN IMMEDIATE` serializes migrations, relation writes, and checkpoint
  sequence allocation where deferred transactions would create races.
- Startup lock serializes the external MiniLM companion.

The database must not be written concurrently from multiple hosts through a
filesystem-sync service. WAL is a local concurrency mechanism, not distributed
coordination. Keep the writable database on one host; use the authenticated
write gateway for remote mutations.

`write_gateway.py` is the controlled exception: remote callers write only to
a machine-specific local outbox, then send an allowlisted request to the Main
service over authenticated HTTPS. The Main service stores an idempotency
receipt before dispatch. Duplicate keys replay the receipt; a crash-left
`processing` key fails closed instead of risking duplicate mutation. `--db`
and Main-filesystem payload indirection are rejected at the boundary in both
separate-token and `--flag=value` forms. Document `ingest` is deliberately
local-only, so a remote caller cannot make the writer service read an arbitrary
writer-host path.

`durable_events` provides an at-least-once host notification boundary with
producer deduplication and explicit acknowledgement. Background delegation
publishes a terminal event when project-scoped; the host/orchestrator owns
polling/streaming, conversation wake, successful handling, and final ack.

## 18. Scale Strategy

The current design is intended to remain responsive with at least hundreds of
thousands of records:

- FTS5 provides inverted-index lexical retrieval.
- Project/category/status/bug/module indexes constrain scoped work.
- Candidate lists are capped before Python reranking.
- SQL operations batch large ID sets.
- Lifecycle current heads are materialized with union-by-size.
- Relation health is O(N+E).
- Read-only commands avoid migration/write-lock work.
- Semantic exact scan stops above 20,000 scoped embedded rows.
- Above the threshold, HNSW supplies at most 200 candidates; lexical
  candidates remain the fallback when the optional sidecar is unavailable.
- Embedding statistics fetch BLOB lengths rather than all vector bytes.

Lexical FTS remains the complete retrieval path at every scale. Semantic
retrieval is intentionally allowed to lose zero-keyword-overlap recall above
the threshold rather than make query memory/time proportional to every
stored vector.

## 19. Failure and Degradation Model

| Failure | Behavior |
|---|---|
| MiniLM package or cache absent | lexical ingest/search continues |
| companion unavailable | semantic pass skipped; structured reason returned |
| sandbox denies localhost | diagnose permission boundary; do not change environment |
| incompatible service on port 8770 | reject identity; do not spawn over it |
| stale/malformed embedding | skip vector and report pending |
| changed Markdown | atomic replacement of that document's chunks |
| unchanged Markdown | no duplicate chunks |
| invalid relation/branch/cycle | transaction aborts |
| checkpoint writer collision | second writer waits at `BEGIN IMMEDIATE` |
| FTS drift | `doctor` reports real inverted-index missing/extra identities |
| migration failure | complete rollback; old schema version remains authoritative |
| cross-host concurrent write through a shared folder | unsupported; risk of conflict/corruption |

## 20. Agent Operating Workflow

At the beginning of non-trivial work:

1. Run `handoff --project <PROJECT> --json`.
2. Continue a relevant session or create one.
3. Run `embed-backfill` once; failure is non-blocking.
4. If embedding fails, run `embed-diagnose` before drawing any environment
   conclusion.
5. Query memory before rediscovering prior work.
6. Open cited Markdown source lines before high-risk decisions.

During work:

1. Write project knowledge to the appropriate human-readable Markdown.
2. Run `sync_tracked.py` after changing tracked knowledge documents.
3. Write native audit/fix/verification records directly when stable internal
   references are required.
4. Checkpoint after each material phase and before compaction/agent switch.

After work:

1. Add verification to the native lifecycle when an audit was fixed.
2. Run targeted tests and `doctor`.
3. Run retrieval evaluation when retrieval logic or indexed seed content
   changed.
4. Close the session only after truthful verification.

## 21. Test and Evaluation Contracts

Fast regression suite:

```bash
python3 -m unittest discover -s developer -p 'test_*.py'
```

The suite uses temporary databases, does not require MiniLM, and must not leak
a background companion process.

Retrieval evaluation cases live in
`developer/eval_queries.json`. The CLI uses this file by
default:

```bash
python3 endeavor_db.py evaluate --pipeline both --json
python3 endeavor_db.py evaluate --pipeline unified --semantic off --json
```

Evaluation defaults to both the Markdown-only baseline and the production
`search_all()` unified path. Each report includes its pipeline, source mix,
recall at the requested limit, mean reciprocal rank, per-query rank, and
whether semantic capability was actually available.

Health/integrity gate:

```bash
python3 endeavor_db.py doctor
```

`doctor.ok` requires current schema, SQLite integrity, valid foreign keys,
exact FTS identities, no invalid embedding BLOBs, and valid lifecycle
components/relations. Missing embeddings and companion warm state are
informational because lexical-only operation is supported.

## 22. Change Checklist

When changing schema or lifecycle behavior:

1. Keep migration atomic.
2. Preserve existing documents, knowledge, sessions, and checkpoints.
3. Test direct SQLite writes as well as CLI writes; triggers are part of the
   integrity boundary.
4. Run unit tests and `doctor` against a migrated database.
5. Benchmark at representative high cardinality.

When changing retrieval:

1. Test raw lexical passes and filters.
2. Verify RRF reasons and deterministic ordering.
3. Test lexical-only fallback.
4. Run `evaluate --semantic off` and the default semantic evaluation.
5. Record ranking changes only when evaluation or reviewed feedback supports
   them.

When changing embeddings:

1. Keep `endeavor_db.py` free of heavy ML imports.
2. Preserve model name/dimension identity checks.
3. Preserve cache-only loading and lexical fallback.
4. Test sandbox-denied and unsandboxed localhost paths separately.
5. Verify stale hashes self-heal through backfill.

When changing developer layout:

1. Keep runtime assets used through `HERE / ...` at stable runtime paths or
   update every path atomically.
2. Search the repository for old paths.
3. Run the suite from the repository root exactly as documented.
4. Keep `README.md` at project root and detailed developer material under
   `developer/`.

## 23. Source-of-Truth Matrix

| Information | Authority | SQLite role |
|---|---|---|
| project memory, bug reports, training docs | reviewed Markdown in Git | searchable derived chunks |
| native audit/fix/verification/decision | `memory_records` + relations | authoritative history/current truth |
| session continuity | sessions + checkpoints | authoritative local handoff state |
| query usefulness | explicit `query_feedback` | reviewed ranking evidence |
| FTS indexes | derived from content tables | disposable/rebuildable acceleration |
| embeddings | derived from model+content | disposable/rebuildable enhancement |
| graphify output | generated analysis | navigation aid, not runtime authority |

The governing principle is: preserve human-readable provenance, use stable
SQLite IDs only where internal lifecycle references are required, and never
make an optional retrieval enhancement a gate on access to memory.
