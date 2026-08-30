# ENDMEMEX — Agent Procedure

This is the agent-focused operating procedure for the standalone public ENDMEMEX repository.

It is intentionally an **index and execution workflow**, not a duplicate of [`ENDMEMEX_USER_MANUAL.md`](ENDMEMEX_USER_MANUAL.md). The User Manual remains authoritative for exact command semantics and detailed lifecycle behavior.

Document roles:

- `AGENTS.md` — discovery entry point.
- `CLAUDE.md` — hard constraints.
- `AGENT.md` — quick operational guide.
- **This file** — full agent workflow and routing to the manual.
- `README.md` — urgent rules/task index/public overview.
- `ENDMEMEX_USER_MANUAL.md` — detailed authoritative command/lifecycle reference.
- `INSTALL.eng-th.md` — install/public-release privacy guidance.

## 1. Start-of-task procedure

Before non-trivial work:

1. Read `CLAUDE.md` and the README urgent rules.
2. Inspect Git status.
3. Identify the selected ENDMEMEX project label/database context.
4. Bootstrap once using the documented `bootstrap --project <PROJECT> --json` flow.
5. Query existing knowledge before rediscovering prior work or making a high-impact decision.
6. Open/validate cited sources before relying on stale results.
7. Read only the relevant User Manual section for the task.
8. Define the exact lifecycle/write/retrieval invariant before editing code.

## 2. Memory write lifecycle

Every successful project-memory write is user-visible state change.

For writes/checkpoints/records:

- use the documented project/session identifiers;
- checkpoint after material phases, verification, handoff, and commit—not only at the end;
- record completed work, current state, exact next steps, blockers, changed files, commands, and verification;
- never mark unverified work `completed`;
- tell the user immediately after every successful project-memory write;
- never store secrets, credentials, personal data, or large raw logs.

Exact lifecycle commands: use the User Manual's shared session/checkpoint sections.

## 3. Query/current-truth procedure

Before relying on retrieved knowledge:

1. Query narrowly for the current question.
2. Prefer current/non-superseded records.
3. Resolve lifecycle links (fix, verification, supersession, contradiction) before treating an old record as current truth.
4. If a result is marked stale, open and validate the cited source.
5. Do not substitute a broad Git scan for the mandatory pending-work procedure when the user asks what is pending/next.

For pending/backlog/handoff requests, follow the README mandatory pending-work procedure exactly.

## 4. Tracked knowledge synchronization

After changing tracked knowledge Markdown:

```bash
python3 sync_tracked.py <path>...
```

Use `ingest` for a persistent untracked document only when the source will remain available.

If the source document will be retired/deleted, use the documented durable archival flow based on `record-add --type knowledge`, verify round-trip retrieval/search, then delete only after the archive is proven complete.

Never use `ingest` as the retirement workflow for a source that will disappear.

## 5. SQLite ownership and remote mutation

A writable ENDMEMEX SQLite database belongs to one host/process domain at a time.

Rules:

- do not place a writable SQLite DB in a filesystem-sync folder and treat it as distributed state;
- do not allow two hosts to mutate the same synchronized SQLite copy;
- use the authenticated write gateway for remote mutation;
- keep remote-read/write semantics explicit;
- preserve WAL/locking/migration safety;
- never commit DB/WAL/journal/runtime state.

Any change to DB connection, locking, migration, write gateway, or remote semantics requires concurrency/failure regression coverage.

## 6. Embedding and semantic search procedure

When semantic search/embedding fails:

1. Run `python3 endeavor_db.py embed-diagnose` first.
2. Use its result to distinguish package/config/service/model failures.
3. Do not restart/change dependencies before diagnosis.
4. Preserve lexical fallback/current behavior where documented.
5. For ranking/retrieval changes, run the deterministic evaluation command.

Do not report an embedding companion as missing solely from a failed query without the diagnostic pass.

## 7. Records and lifecycle links

For audits, fixes, verification, supersession, and contradiction:

- use typed lifecycle links rather than unstructured prose only;
- keep historical records immutable where the model requires it;
- resolve current truth from lifecycle relations;
- do not silently rewrite history to make old state look current;
- test reference integrity when changing record lifecycle code.

Use the User Manual SQLite-native records/reference sections for exact commands.

## 8. Mailbox and coordination

Mailbox notes are pull-based knowledge records.

- use the documented `[TO:<agent>]` convention;
- do not assume a background listener;
- scope notes to durable, useful handoff information;
- do not put secrets or large raw logs into mailbox records.

## 9. Presence

Presence writes are opt-in only.

- ordinary single-session work must not auto-start presence;
- read-only presence checks are safe;
- start/refresh presence only when the user is coordinating concurrent work or explicitly asks agents to announce activity;
- clear/finish presence according to the documented lifecycle.

## 10. Delegation/advisor boundary

Worker delegation is user-gated.

For delegation:

- require user authorization;
- give an exact deliverable, file scope, constraints, and definition of done;
- keep the parent agent responsible for integration and final verification;
- use the MCP agent server or documented bounded fallback;
- do not let a worker silently broaden scope.

Reviewers/advisors remain read-only unless separately authorized as workers.

## 11. Code-change procedure

For production code changes:

1. identify the exact persistence/retrieval/lifecycle invariant;
2. inspect the smallest complete call path;
3. preserve backward compatibility or provide an explicit migration where persistent state changes;
4. add/update deterministic tests;
5. run targeted tests;
6. run the standard full regression;
7. run `doctor`;
8. run retrieval evaluation when ranking/search semantics change;
9. inspect final diff and privacy/release hygiene.

Standard regression:

```bash
python3 -m unittest discover -s developer -p 'test_*.py'
python3 endeavor_db.py doctor
```

For retrieval/ranking changes:

```bash
python3 endeavor_db.py evaluate --semantic off --json
```

## 12. Documentation changes

Keep detailed command semantics in the User Manual rather than copying them into agent docs.

- `AGENT.md` = quick operational guide;
- this file = agent workflow/index;
- `CLAUDE.md` = hard rules;
- README = urgent rules/task index/public overview;
- User Manual = detailed authoritative procedures.

When headings/files change, update inbound links and verify anchors.

## 13. Git/release hygiene

Before commit/push:

- inspect status/diff;
- stage only intended files;
- do not stage SQLite DBs/WAL/journals, logs, caches, sidecars, presence state, agent-run artifacts, secrets, local tokens, or private documents;
- follow `INSTALL.eng-th.md` public-release privacy guidance;
- use focused commits;
- never force-push unless explicitly requested and appropriate.

## 14. Completion criteria

A task is complete when applicable items hold:

- requested behavior is implemented;
- memory lifecycle/current-truth semantics remain correct;
- standard regression passes;
- `doctor` passes;
- retrieval evaluation passes when relevant;
- tracked-knowledge sync ran when required;
- successful memory writes were reported to the user;
- no private/runtime DB state is staged;
- final report distinguishes current verified truth from stale/unverified records.

**Decision rule:** durable state requires explicit lifecycle, explicit ownership, and verification before claiming current truth.
