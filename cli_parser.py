"""endeavor_db.py's CLI argument parser, Phase 5 slice.

Transitively clean: every name this references is either a pure-literal
config constant (grep-confirmed never a monkeypatch target) or an
already-extracted validator, never a call into one of endeavor_db.py's
dirty functions (search_all, bootstrap, create_memory_record, etc. --
those stay put so `main` retains its tested integration boundary).

`build_parser` takes `module_doc` as an explicit parameter rather than
reading `__doc__` directly: `__doc__` is a free variable resolved against
the *enclosing module* at call time, so if this function's body used it
directly, moving the `def` here would silently rebind the parser's
`description` to this file's docstring instead of endeavor_db.py's. The
facade wrapper in endeavor_db.py passes its own `__doc__` in.
"""
from __future__ import annotations

import argparse

from activity import ACTIVITY_EXPORT_LIMIT, ACTIVITY_EXPORT_NAME
from cli_validators import nonempty_text, pack_budget, positive_int
from config import (
    EMBED_BATCH_SIZE, HERE, MAX_MEMORY_CONTEXT_RECORDS, MEMORY_RECORD_STATUSES,
    MEMORY_ACTION_STATES, MEMORY_AGENT_CHOICES, MEMORY_RECORD_TYPES, MEMORY_RELATIONS,
    PACK_DEFAULT_BUDGET_CHARS,
)
from primitives import parse_feedback_result_id


def build_parser(module_doc: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=module_doc)
    parser.add_argument("--db", help="SQLite path (default: endeavor_memory.sqlite3)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create or migrate the database")
    sub.add_parser("seed", help="Ingest the bundled ENDMEMEX guides")
    sub.add_parser(
        "agent-help", help="Print a short agent-facing command cheat sheet (no database access)"
    )

    ingest = sub.add_parser("ingest", help="Ingest or refresh a Markdown document")
    ingest.add_argument("source")
    ingest.add_argument("--project", required=True)
    ingest.add_argument("--kind", default="project_memory")
    ingest.add_argument(
        "--no-embed", dest="embed", action="store_false",
        help="Skip the best-effort MiniLM embedding pass (lexical ingest always still runs)",
    )

    query = sub.add_parser("query", help="Search indexed knowledge")
    query.add_argument("text")
    query.add_argument("--project")
    query.add_argument("--category")
    query.add_argument("--status", dest="knowledge_status", choices=("open", "resolved", "accepted"))
    query.add_argument("--module")
    query.add_argument("--bug-id")
    query.add_argument("--session-label")
    query.add_argument("--limit", type=int, default=5)
    query.add_argument("--json", action="store_true")
    query.add_argument(
        "--semantic", choices=("auto", "on", "off"), default="auto",
        help="auto: use MiniLM only if already warm (default, never stalls). "
             "on: spawn/wait for the companion if needed (~11s cold). off: lexical only.",
    )
    query.add_argument(
        "--compact", action="store_true",
        help="Trim --json results to the ~8 fields an agent reads when browsing "
             "(id/title/project/category/excerpt/location/match_reasons/rank) "
             "instead of the full ~29-field row",
    )
    query.add_argument(
        "--check-stale", dest="check_stale",
        action="store_true", default=True,
        help="Flag markdown-sourced results whose source file has drifted from the "
             "indexed hash (reads each distinct source file once; on by default)",
    )
    query.add_argument(
        "--no-check-stale", dest="check_stale", action="store_false", help=argparse.SUPPRESS,
    )

    sub.add_parser("stats", help="Show database counts")
    sub.add_parser(
        "doctor", help="Check SQLite integrity, FTS identity, embeddings, and internal relation lifecycles"
    )
    evaluate = sub.add_parser("evaluate", help="Run retrieval evaluation cases")
    evaluate.add_argument("--file", default=str(HERE / "developer" / "eval_queries.json"))
    evaluate.add_argument("--limit", type=int, default=5)
    evaluate.add_argument("--json", action="store_true")
    evaluate.add_argument("--semantic", choices=("auto", "on", "off"), default="on")
    evaluate.add_argument(
        "--pipeline", choices=("markdown", "unified", "both"), default="both",
        help="Benchmark the Markdown-only baseline, the production unified query path, or both",
    )

    sub.add_parser(
        "embed-status", help="Check embedding coverage plus structured companion diagnostics (never spawns)"
    )
    readiness = sub.add_parser(
        "readiness",
        help="One read-only project preflight: machine role, database health, embeddings, ANN, docs, and next actions",
    )
    readiness.add_argument("--project", required=True, type=nonempty_text)
    readiness.add_argument("--json", action="store_true", help="Emit the complete machine-readable report")
    sub.add_parser("ann-status", help="Check optional per-machine HNSW sidecar freshness")
    sub.add_parser("ann-build", help="Build/rebuild the optional per-machine HNSW sidecar")
    sub.add_parser(
        "embed-diagnose",
        help="Diagnose CLI Python, companion dependencies, localhost policy, and model contract without spawning",
    )
    warm = sub.add_parser(
        "embed-warm", help="Start MiniLM; --keep-alive holds it in RAM until embed-cool or process exit",
    )
    warm.add_argument("--keep-alive", action="store_true")
    sub.add_parser("embed-cool", help="Return a warm MiniLM companion to its normal idle timeout")
    backfill = sub.add_parser(
        "embed-backfill", help="Spawn the MiniLM companion if needed and embed any missing/stale rows"
    )
    backfill.add_argument("--batch-size", type=positive_int, default=EMBED_BATCH_SIZE)
    sub.add_parser(
        "install-hooks",
        help="Copy the git-tracked hook sources (hooks/) into .git/hooks — run once per fresh clone",
    )
    boot = sub.add_parser(
        "bootstrap",
        help="One-call session start: latest handoff (null when none), embedding backfill, "
             "tracked-doc freshness counts, and hook state",
    )
    boot.add_argument("--project", required=True)
    boot.add_argument("--session", help="Explicit resumable session ID; must belong to --project")
    boot.add_argument("--json", action="store_true")
    boot.add_argument("--batch-size", type=positive_int, default=EMBED_BATCH_SIZE)
    boot.add_argument("--include-pending", action="store_true", help="Also include lifecycle-aware pending work")
    pack = sub.add_parser(
        "pack",
        help="One-call session briefing: handoff + open records + recent project knowledge "
             "+ recent activity, trimmed to a character budget",
    )
    pack.add_argument("--project", required=True)
    pack.add_argument("--session", help="Explicit resumable session ID; must belong to --project")
    pack.add_argument("--budget", dest="budget_chars", type=pack_budget, default=PACK_DEFAULT_BUDGET_CHARS)
    pack.add_argument("--json", action="store_true")
    pending = sub.add_parser(
        "pending", help="Read lifecycle-aware pending work without changing sessions or records",
    )
    pending_scope = pending.add_mutually_exclusive_group(required=True)
    pending_scope.add_argument("--project", type=nonempty_text)
    pending_scope.add_argument("--all-projects", action="store_true")
    pending.add_argument("--json", action="store_true")
    maintenance = sub.add_parser(
        "maintenance",
        help="VACUUM + PRAGMA optimize to reclaim disk space and refresh planner statistics. "
             "Manual-only, exclusive operation — do not run while another agent process may be writing.",
    )
    maintenance.add_argument(
        "--yes", action="store_true",
        help="Required confirmation; rewrites the whole database file and briefly blocks all writers",
    )
    activity = sub.add_parser(
        "activity",
        help=f"Write {ACTIVITY_EXPORT_NAME} — human-readable digest of the latest write actions "
             "(also auto-refreshed after every write command, at the default limit)",
    )
    activity.add_argument("--limit", type=positive_int, default=ACTIVITY_EXPORT_LIMIT)
    activity.add_argument("--stdout", action="store_true", help="Print the digest instead of writing the file")
    activity.add_argument(
        "--follow", action="store_true",
        help="Poll for new activity and print it as it lands (Ctrl-C to stop), instead of "
             "writing/printing the digest. Read-only; a human-watched near-real-time view of "
             "what the other agent is doing, not agent-to-agent messaging.",
    )
    activity.add_argument("--project", help="With --follow, only show activity for this project")
    activity.add_argument(
        "--interval", type=float, default=3.0,
        help="With --follow, seconds between polls (default 3.0)",
    )
    feedback = sub.add_parser("feedback", help="Record whether query results were useful")
    feedback.add_argument("--agent", required=True, choices=MEMORY_AGENT_CHOICES)
    feedback.add_argument("--query", required=True)
    feedback.add_argument(
        "--result", dest="selected_ids", action="append", type=parse_feedback_result_id, default=[],
        help="Selected result ID (integer Markdown ID or stable SQLite record ID)",
    )
    feedback.add_argument("--useful", choices=("yes", "no"))
    feedback.add_argument("--note", default="")

    event_add = sub.add_parser("event-add", help="Publish a durable host-consumable event")
    event_add.add_argument("--type", dest="event_type", required=True)
    event_add.add_argument("--project", required=True)
    event_add.add_argument("--subject", dest="subject_id", required=True)
    event_add.add_argument("--dedupe-key", required=True)
    event_add.add_argument("--payload", default="{}", help="JSON object")
    event_add.add_argument("--agent", required=True, choices=MEMORY_AGENT_CHOICES)

    event_poll = sub.add_parser("event-poll", help="Read durable events in monotonic ID order")
    event_poll.add_argument("--after", type=int, default=0)
    event_poll.add_argument("--project")
    event_poll.add_argument("--limit", type=positive_int, default=50)
    event_poll.add_argument("--include-acked", action="store_true")
    event_poll.add_argument("--json", action="store_true")

    event_ack = sub.add_parser("event-ack", help="Acknowledge a durable event after host handling")
    event_ack.add_argument("event_id", type=positive_int)
    event_ack.add_argument("--agent", required=True, choices=MEMORY_AGENT_CHOICES)

    record_add = sub.add_parser("record-add", help="Add a durable SQLite-native audit/fix/knowledge record")
    record_add.add_argument("--id", dest="record_id", help="Stable ID, for example AUDIT-MEM-001")
    record_add.add_argument("--project", required=True)
    record_add.add_argument("--type", dest="record_type", choices=MEMORY_RECORD_TYPES, required=True)
    record_add.add_argument("--title", required=True)
    record_add.add_argument("--content", help="Record body text; mutually exclusive with --content-file")
    record_add.add_argument(
        "--content-file", metavar="PATH",
        help="Read record body text from PATH ('-' for stdin) instead of --content, "
             "so long/multi-line content never has to survive shell quoting",
    )
    record_add.add_argument("--status", choices=MEMORY_RECORD_STATUSES, default="current")
    record_add.add_argument(
        "--action-state", choices=MEMORY_ACTION_STATES,
        help="Independent work-triage state; defaults by record type/status",
    )
    record_add.add_argument("--metadata", default="{}", help="JSON object")
    record_add.add_argument(
        "--source", metavar="PATH",
        help="Repo-relative path this record documents (e.g. a written report); "
             "stored as metadata.source so a later `record-show`/`query` result "
             "can be followed straight to the file, same convention as knowledge "
             "chunks' source_path",
    )
    record_add.add_argument(
        "--link", action="append", default=[], metavar="RELATION:TARGET",
        help="Create an internal relation atomically; may be repeated",
    )
    record_add.add_argument("--note", default="", help="Note applied to links created by this command")
    record_add.add_argument("--agent", required=True, choices=MEMORY_AGENT_CHOICES)

    record_update = sub.add_parser("record-update", help="Update the content or explicit status of a record")
    record_update.add_argument("record_id")
    record_update.add_argument("--project")
    record_update.add_argument("--type", dest="record_type", choices=MEMORY_RECORD_TYPES)
    record_update.add_argument("--title")
    record_update.add_argument("--content")
    record_update.add_argument("--status", choices=MEMORY_RECORD_STATUSES)
    record_update.add_argument("--action-state", choices=MEMORY_ACTION_STATES)
    record_update.add_argument("--metadata", help="Replacement JSON object")
    record_update.add_argument("--agent", required=True, choices=MEMORY_AGENT_CHOICES)

    record_link = sub.add_parser("record-link", help="Create a typed foreign-key relation between SQLite records")
    record_link.add_argument("source_id")
    record_link.add_argument("relation", choices=MEMORY_RELATIONS)
    record_link.add_argument("target_id")
    record_link.add_argument("--note", default="")
    record_link.add_argument("--agent", required=True, choices=MEMORY_AGENT_CHOICES)

    record_show = sub.add_parser("record-show", help="Show a record, current successor, conflicts, and relation graph")
    record_show.add_argument("record_id")
    record_show.add_argument("--depth", type=int, default=2)
    record_show.add_argument(
        "--max-records", type=int, default=MAX_MEMORY_CONTEXT_RECORDS,
        help=f"Bound graph expansion (1-{MAX_MEMORY_CONTEXT_RECORDS}; default: %(default)s)",
    )

    record_search = sub.add_parser("record-search", help="Full-text search SQLite-native records")
    record_search.add_argument("text")
    record_search.add_argument("--project")
    record_search.add_argument("--type", dest="record_type", choices=MEMORY_RECORD_TYPES)
    record_search.add_argument("--limit", type=int, default=10)
    record_search.add_argument("--current-only", action="store_true")

    start = sub.add_parser("session-start", help="Start a shared work session")
    start.add_argument("--project", required=True)
    start.add_argument("--goal", required=True)
    start.add_argument("--agent", required=True, choices=MEMORY_AGENT_CHOICES)

    checkpoint = sub.add_parser("checkpoint", help="Record resumable session state")
    checkpoint.add_argument("--session")
    checkpoint.add_argument("--project")
    checkpoint.add_argument("--agent", required=True, choices=MEMORY_AGENT_CHOICES)
    checkpoint.add_argument(
        "--goal",
        help="If --project has no active/paused/blocked session, start one with this "
             "goal before checkpointing, collapsing handoff+session-start+checkpoint "
             "into one call for the common single-session task",
    )
    checkpoint.add_argument("--payload", help="JSON payload file; CLI flags override it")
    checkpoint.add_argument("--summary")
    checkpoint.add_argument("--work-done")
    checkpoint.add_argument("--current-state")
    checkpoint.add_argument("--next-steps")
    checkpoint.add_argument("--blockers")
    checkpoint.add_argument("--status", choices=("active", "paused", "completed", "blocked"))
    checkpoint.add_argument("--file", dest="files_changed", action="append")
    checkpoint.add_argument(
        "--auto-files", dest="auto_files", action="store_true",
        help="Append git-status-detected changed files under ROOT/<project>/ to --file. "
             "Requires --project; a project label that isn't also a real top-level "
             "directory yields no files rather than guessing across the whole repo.",
    )
    checkpoint.add_argument("--command", dest="commands_run", action="append")
    checkpoint.add_argument("--verify", dest="verification", action="append")
    checkpoint.add_argument(
        "--pin", action="store_true",
        help="Mark this checkpoint important: exempt it from the sliding-window prune "
             "(add_checkpoint's per-session cap and prune_checkpoints_globally) so it "
             "survives indefinitely regardless of age or session status.",
    )

    pin_cp = sub.add_parser(
        "pin-checkpoint", help="Retroactively pin an existing checkpoint so pruning never deletes it",
    )
    pin_cp.add_argument("checkpoint_id", type=int)
    pin_cp.add_argument("--agent", required=True, choices=MEMORY_AGENT_CHOICES)

    unpin_cp = sub.add_parser(
        "unpin-checkpoint", help="Unpin a checkpoint, returning it to normal sliding-window pruning",
    )
    unpin_cp.add_argument("checkpoint_id", type=int)
    unpin_cp.add_argument("--agent", required=True, choices=MEMORY_AGENT_CHOICES)

    latest = sub.add_parser("handoff", help="Read the latest resumable checkpoint")
    latest.add_argument("--session")
    latest.add_argument("--project")
    latest.add_argument(
        "--all-paused", action="store_true",
        help="List the latest checkpoint for every paused session across all projects",
    )
    latest.add_argument("--json", action="store_true")

    timeline = sub.add_parser(
        "timeline",
        help="Read-only checkpoint timeline (who did what, affected files, statuses) across sessions",
    )
    timeline.add_argument("--project")
    timeline.add_argument("--agent", choices=MEMORY_AGENT_CHOICES)
    timeline.add_argument("--status", choices=("active", "paused", "completed", "blocked"))
    timeline.add_argument("--session")
    timeline.add_argument(
        "--limit", type=positive_int, default=100,
        help="Max checkpoints to return (default 100, capped at 500)",
    )
    timeline.add_argument(
        "--oldest-first", action="store_true", help="Sort ascending instead of the newest-first default",
    )
    timeline.add_argument("--json", action="store_true")

    close = sub.add_parser("session-close", help="Mark a session completed or blocked")
    close.add_argument("--session")
    close.add_argument("--project")
    close.add_argument("--agent", required=True, choices=MEMORY_AGENT_CHOICES)
    close.add_argument("--status", choices=("completed", "blocked"), default="completed")

    pstart = sub.add_parser(
        "presence-start",
        help="Announce (machine, agent, project[, instance]) as actively working "
             "(same-machine real-time; mirrored to a per-machine sidecar file for "
             "cross-machine visibility)",
    )
    pstart.add_argument("--agent", required=True, choices=MEMORY_AGENT_CHOICES)
    pstart.add_argument("--project", required=True)
    pstart.add_argument("--task", default="", help="Short free-text of what this process is doing")
    pstart.add_argument("--session")
    pstart.add_argument(
        "--instance", default="",
        help="Only needed to distinguish two concurrent instances of the same "
             "agent on the same project/machine; identity is otherwise "
             "(machine, agent, project), not pid -- each CLI call is its own "
             "short-lived subprocess",
    )
    pstart.add_argument("--pid", type=int, help="Informational only; defaults to this process's own PID")

    pheartbeat = sub.add_parser(
        "presence-heartbeat",
        help="Refresh the (machine, agent, project[, instance]) presence row (call it "
             "wherever checkpoint is already called -- do not add a new polling loop "
             "just for this)",
    )
    pheartbeat.add_argument("--agent", required=True, choices=MEMORY_AGENT_CHOICES)
    pheartbeat.add_argument("--project", required=True)
    pheartbeat.add_argument("--instance", default="")
    pheartbeat.add_argument("--task", help="Update the task description; omit to just refresh the timestamp")
    pheartbeat.add_argument("--pid", type=int, help="Informational only; defaults to this process's own PID")

    pstop = sub.add_parser("presence-stop", help="Mark the (machine, agent, project[, instance]) presence row stopped")
    pstop.add_argument("--agent", required=True, choices=MEMORY_AGENT_CHOICES)
    pstop.add_argument("--project", required=True)
    pstop.add_argument("--instance", default="")

    plist = sub.add_parser(
        "presence",
        help="List agents currently working: real-time local rows plus the last-known "
             "snapshot from every other machine's presence sidecar",
    )
    plist.add_argument("--project")
    plist.add_argument("--json", action="store_true")

    sync_status = sub.add_parser(
        "sync-status",
        help="Show the last-known write time per machine (informational freshness "
             "signal only; never a synchronization guarantee or write authorization)",
    )
    sync_status.add_argument("--json", action="store_true")
    return parser
