#!/usr/bin/env python3
"""Stdio MCP bridge for shared Endeavor project memory operations.

Read-only tools (query/readiness/handoff/pending/record_show/record_search/pack/presence_list/
sync_status/timeline) always run. Write tools (checkpoint/pin_checkpoint/bootstrap/
record_add/record_update/record_link/session_close/feedback/presence_start/
presence_heartbeat/presence_stop) shell out to endeavor_db.py, which
serializes concurrent local writers with SQLite WAL and a busy timeout. For a
remote-writer deployment, use write_gateway.py rather than sharing a writable
SQLite database through a filesystem sync service.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from config import MEMORY_AGENT_CHOICES, ROOT

HERE = Path(__file__).resolve().parent
DB = HERE / "endeavor_db.py"
DB_COMMAND_TIMEOUT_S = 60

WRITE_TOOLS = {
    "endeavor_memory_checkpoint", "endeavor_memory_pin_checkpoint", "endeavor_memory_bootstrap",
    "endeavor_memory_record_add", "endeavor_memory_record_update",
    "endeavor_memory_record_link", "endeavor_memory_session_close",
    "endeavor_memory_feedback", "endeavor_memory_event_ack", "endeavor_presence_start",
    "endeavor_presence_heartbeat", "endeavor_presence_stop",
}
SERVER_INSTRUCTIONS = (
    "ENDMEMEX is shared project memory. At the start of non-trivial work call bootstrap once, "
    "then query before rediscovering prior work or making high-impact decisions. Checkpoint after "
    "meaningful implementation, verified tests, decisions, handoffs, and Git commits. Use durable "
    "records for audit -> resolves:fix -> verifies:verification lifecycles. Never store secrets. "
    "Successful tools return JSON encoded in text content; errors start [error]. "
    "Keep writable SQLite local to one host; use authenticated write_gateway.py for remote mutations. "
    "Open cited sources before relying on material search results. "
    "Agent presence is opt-in: call presence_start/presence_heartbeat/presence_stop only when the "
    "user asks agents to announce work or is coordinating multiple concurrent sessions; otherwise "
    "do not create those shared-database writes. sync_status shows the last-known write time per machine. "
    "Readiness is the single read-only preflight for local-host identity, DB health, embedding coverage, ANN, "
    "tracked-document freshness, and ordered next actions."
)

READ_ONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
WRITE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
PROJECT_PROPERTY = {
    "type": "string",
    "minLength": 1,
    "description": "Stable project label used in ENDMEMEX, not a filesystem path (for example ENDMEMEX or MY_APP).",
}
SESSION_PROPERTY = {
    "type": "string",
    "minLength": 1,
    "description": "Explicit ENDMEMEX session ID selected from pending or an all-paused handoff.",
}
CONFIRM_PROPERTY = {
    "type": "boolean",
    "description": "Accepted for backward compatibility.",
}
AGENT_PROPERTY = {
    "type": "string",
    "enum": list(MEMORY_AGENT_CHOICES),
    "description": "Actor that performed the recorded work; use endeavor when the Endeavor runtime itself writes; do not claim another actor's work as your own.",
}
INSTANCE_PROPERTY = {
    "type": "string",
    "description": "Only needed to distinguish two concurrent instances of the same agent working the same "
                   "project on this machine at once. Identity is (machine, agent, project, instance), not "
                   "pid -- omit unless you know you need it.",
}

TOOLS = [
    {"name": "endeavor_memory_query", "title": "Search ENDMEMEX", "description": "READ ONLY. Use before rediscovering prior work or making a high-impact project decision. Searches indexed Markdown and current durable records. Inspect cited sources before trusting material results. Returns a JSON array as text; stale=true means open the source because its indexed copy drifted. Do not use it to write a checkpoint.", "annotations": READ_ONLY_ANNOTATIONS, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "query": {"type": "string", "minLength": 1, "description": "Natural-language or keyword search. Use the terms an existing note or record is likely to contain."},
        "project": PROJECT_PROPERTY,
        "category": {"type": "string", "minLength": 1, "description": "Optional knowledge category or durable record type used to narrow results."},
        "status": {"type": "string", "enum": ["open", "resolved", "accepted"], "description": "Optional Markdown-derived knowledge status filter."},
        "module": {"type": "string", "minLength": 1, "description": "Optional exact/literal module metadata filter, for example react.py."},
        "bug_id": {"type": "string", "minLength": 1, "description": "Optional exact bug-ID metadata filter."},
        "session_label": {"type": "string", "minLength": 1, "description": "Optional session-label metadata filter."},
        "semantic": {"type": "string", "enum": ["auto", "on", "off"], "description": "auto uses MiniLM only if warm; on starts/waits for vector search; off forces lexical FTS only. Default auto."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Maximum results to return (default 5)."},
        "compact": {"type": "boolean", "description": "Trim results to the ~8 fields read when browsing (default true)"},
        "check_stale": {"type": "boolean", "description": "Flag results whose source file has drifted from the indexed hash (default true; set false only for a measured latency-sensitive call)."},
    }, "required": ["query"]}},
    {"name": "endeavor_memory_readiness", "title": "Check ENDMEMEX project readiness", "description": "READ ONLY. Use this one-call preflight before a project session when you need machine role, database/FTS health, embedding coverage, ANN sidecar state, tracked-document freshness, and ordered actionable next_actions in one JSON result. It never bootstraps, backfills, warms a companion, builds ANN, or writes the database. Returns a JSON object as text; overall is ready, attention, or blocked. Follow next_actions in priority order and do not auto-prune orphaned documents.", "annotations": READ_ONLY_ANNOTATIONS, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "project": PROJECT_PROPERTY,
    }, "required": ["project"]}},
    {"name": "endeavor_memory_pack", "title": "Build ENDMEMEX briefing", "description": "READ ONLY. Use at the start of a non-trivial task when a handoff alone is insufficient. Returns JSON text containing the selected handoff, lifecycle-aware actionable_records, raw open_records, recent knowledge, and activity within the total serialized budget. budget_omitted_counts reports every eligible item omitted for space. open_records means only stored status=open and can include historical evidence resolved or superseded by lifecycle edges; use actionable_records for current follow-up work and record_show before relying on a material record. If pending_complete=false, inspect pending_warnings before acting. Pass session after the user selects a resumable session. Prefer handoff for a lightweight resume; do not call repeatedly during ordinary work.", "annotations": READ_ONLY_ANNOTATIONS, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "project": PROJECT_PROPERTY,
        "session": SESSION_PROPERTY,
        "budget": {"type": "integer", "minimum": 500, "maximum": 50000, "description": "Approximate character budget (default 6000; max 50000)."},
    }, "required": ["project"]}},
    {"name": "endeavor_memory_pending", "title": "List lifecycle-aware pending ENDMEMEX work", "description": "READ ONLY. Use to discover all pending work without confusing paused handoffs with audits. Returns active/last-known presence, resumable/blocked sessions, and only current unresolved durable records. Historical open records resolved by lifecycle edges are suppressed. Do not silently resume a listed session; ask the user to choose when one or more sessions are resumable.", "annotations": READ_ONLY_ANNOTATIONS, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "project": PROJECT_PROPERTY,
        "all_projects": {"type": "boolean", "description": "Set true to inspect every project. Exactly one of project or all_projects=true is required."},
    }, "required": [], "oneOf": [{"required": ["project"]}, {"required": ["all_projects"]}]}},
    {"name": "endeavor_memory_handoff", "title": "Read ENDMEMEX handoff", "description": "READ ONLY. Use project for an unambiguous project handoff, session after the user selects a specific resumable session, or all_paused=true for the authoritative cross-project resume queue. Returns JSON text with session and checkpoint; both are null when a project has no resumable session, which is normal. Ambiguous projects are rejected instead of silently choosing. Use pack when wider context is needed.", "annotations": READ_ONLY_ANNOTATIONS, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "project": PROJECT_PROPERTY,
        "session": SESSION_PROPERTY,
        "all_paused": {"type": "boolean", "description": "Set true to list every paused session across all projects."},
    }, "required": [], "oneOf": [{"required": ["project"]}, {"required": ["session"]}, {"required": ["all_paused"]}]}},
    {"name": "endeavor_memory_timeline", "title": "Read ENDMEMEX checkpoint timeline", "description": "READ ONLY. Use to see who did what across sessions: filterable by project, agent, session status, and session ID. Returns JSON text with records (checkpoint+session fields, files/commands/verification evidence), count, total_matching, truncated, order, filters, and a retention_notice -- results cover only currently-retained checkpoints, never activity_log. checkpoint_status per record is derived (\"current\" = latest checkpoint of its session, \"historical\" = superseded); session_status is the session's active/paused/completed/blocked lifecycle state. Newest-first by default.", "annotations": READ_ONLY_ANNOTATIONS, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "project": PROJECT_PROPERTY,
        "agent": AGENT_PROPERTY,
        "status": {"type": "string", "enum": ["active", "paused", "completed", "blocked"], "description": "Optional session-lifecycle status filter (sessions.status; checkpoints have no status of their own)."},
        "session": SESSION_PROPERTY,
        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "description": "Maximum checkpoints to return (default 100, max 500)."},
        "oldest_first": {"type": "boolean", "description": "Sort ascending instead of the newest-first default."},
    }, "required": []}},
    {"name": "endeavor_memory_record_show", "title": "Inspect ENDMEMEX record lifecycle", "description": "READ ONLY. Use before relying on a known durable record. Returns JSON text with the record and lifecycle relations so superseded or contradicted claims are not mistaken for current truth.", "annotations": READ_ONLY_ANNOTATIONS, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {"id": {"type": "string", "minLength": 1, "pattern": "^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$", "description": "Stable record ID, for example AUDIT-MEM-001."}}, "required": ["id"]}},
    {"name": "endeavor_memory_record_search", "title": "Search ENDMEMEX durable records", "description": "READ ONLY. Use when a durable record ID is unknown. Returns a JSON array as text. Prefer current_only=true when seeking current truth; call record_show before relying on a material result to inspect its lifecycle graph.", "annotations": READ_ONLY_ANNOTATIONS, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "query": {"type": "string", "minLength": 1, "description": "Full-text query for record titles/content."},
        "project": PROJECT_PROPERTY,
        "type": {"type": "string", "enum": ["audit", "fix", "verification", "decision", "knowledge"], "description": "Optional durable record type filter."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Maximum results (default 10)."},
        "current_only": {"type": "boolean", "description": "Resolve lifecycle heads and omit records that are no longer current."},
    }, "required": ["query"]}},
    {"name": "endeavor_memory_bootstrap", "title": "Bootstrap ENDMEMEX task context", "description": "WRITE. Use once at the start of a non-trivial task. Returns JSON text with latest handoff, embedding backfill diagnostics, document freshness, and hook state. It may initialize/update local database state. Do not call for a simple one-off question.", "annotations": WRITE_ANNOTATIONS, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "project": PROJECT_PROPERTY,
        "session": SESSION_PROPERTY,
        "include_pending": {"type": "boolean", "description": "Also return this project's lifecycle-aware pending-work view. Default false for response compatibility."},
        "confirm": CONFIRM_PROPERTY,
    }, "required": ["project"]}},
    {"name": "endeavor_memory_checkpoint", "title": "Write ENDMEMEX checkpoint", "description": "WRITE, NON-IDEMPOTENT. Use after every meaningful implementation, verified test, scope decision, handoff, and immediately after a Git commit. Returns the stored checkpoint as JSON text. State truth and exact next steps; paused means another agent can continue, completed requires real verification and no required work remaining. Never include secrets/raw logs.", "annotations": WRITE_ANNOTATIONS, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "project": PROJECT_PROPERTY,
        "session": SESSION_PROPERTY,
        "agent": AGENT_PROPERTY,
        "summary": {"type": "string", "minLength": 1, "description": "Short outcome-first summary of what changed or was verified."},
        "goal": {"type": "string", "description": "Auto-starts a session with this goal if none is active/paused/blocked yet"},
        "work_done": {"type": "string", "description": "Concrete completed work; do not include planned work."},
        "current_state": {"type": "string", "description": "Truthful state now, including partial or unverified behavior."},
        "next_steps": {"type": "string", "description": "Exact ordered continuation instructions; empty only when genuinely complete."},
        "blockers": {"type": "string", "description": "External blockers only; leave empty when unblocked."},
        "status": {"type": "string", "enum": ["active", "paused", "completed", "blocked"], "description": "active=continuing; paused=another agent can resume; completed=verified and no required work remains; blocked=external impasse."},
        "files": {"type": "array", "items": {"type": "string", "minLength": 1}, "description": "Changed file paths, preferably repository-relative."},
        "auto_files": {"type": "boolean", "description": "Append git-status-detected files under ROOT/<project>/ (opt-in, scoped)"},
        "commands": {"type": "array", "items": {"type": "string", "minLength": 1}, "description": "Important commands actually run, not suggested commands."},
        "verify": {"type": "array", "items": {"type": "string", "minLength": 1}, "description": "Observed verification results tied to commands/tests."},
        "pinned": {"type": "boolean", "description": "Mark this checkpoint important: exempt it from the sliding-window prune so it survives indefinitely regardless of age or session status. Use sparingly -- for the checkpoint you'd actually want to find months later, not every routine one."},
        "confirm": CONFIRM_PROPERTY,
    }, "required": ["project", "agent", "summary"]}},
    {"name": "endeavor_memory_pin_checkpoint", "title": "Pin/unpin an ENDMEMEX checkpoint", "description": "WRITE. Retroactively mark an existing checkpoint (by id, from a prior checkpoint call's response) important so pruning never deletes it, or undo that. Returns the updated checkpoint as JSON text.", "annotations": WRITE_ANNOTATIONS, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "checkpoint_id": {"type": "integer", "minimum": 1, "description": "The checkpoint's numeric id, as returned by endeavor_memory_checkpoint."},
        "agent": AGENT_PROPERTY,
        "pinned": {"type": "boolean", "description": "true to pin (default), false to unpin."},
        "confirm": CONFIRM_PROPERTY,
    }, "required": ["checkpoint_id", "agent"]}},
    {"name": "endeavor_memory_record_add", "title": "Add ENDMEMEX durable record", "description": "WRITE, NON-IDEMPOTENT. Use for durable lifecycle facts, not routine progress. Returns the stored record/context as JSON text. Link fixes with resolves:<AUDIT_ID> and verification with verifies:<FIX_ID>; inspect the current record before adding successors. Never store secrets.", "annotations": WRITE_ANNOTATIONS, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "id": {"type": "string", "pattern": "^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$", "description": "Optional stable ID, for example AUDIT-MEM-001; auto-generated if omitted."},
        "project": PROJECT_PROPERTY,
        "type": {"type": "string", "enum": ["audit", "fix", "verification", "decision", "knowledge"], "description": "Durable record role in the lifecycle."},
        "title": {"type": "string", "minLength": 1, "description": "Concise human-readable claim or finding title."},
        "content": {"type": "string", "minLength": 1, "description": "Durable evidence/decision text; concise, source-backed, and free of secrets."},
        "status": {"type": "string", "enum": ["open", "current", "resolved", "accepted", "superseded"], "description": "Truth/lifecycle status; omit to use current."},
        "action_state": {"type": "string", "enum": ["actionable", "deferred", "blocked", "nonactionable", "done"], "description": "Independent work state. Open audit/verification defaults actionable; decisions and knowledge default nonactionable."},
        "source": {"type": "string", "description": "Repo-relative path this record documents"},
        "link": {"type": "string", "pattern": "^(references|resolves|verifies|supersedes|contradicts|duplicates):[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$", "description": "One typed edge RELATION:TARGET_ID, for example resolves:AUDIT-MEM-001."},
        "links": {"type": "array", "items": {"type": "string", "pattern": "^(references|resolves|verifies|supersedes|contradicts|duplicates):[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$"}, "description": "Additional typed edges; use when creating more than one relation atomically."},
        "agent": AGENT_PROPERTY,
        "confirm": CONFIRM_PROPERTY,
    }, "required": ["project", "type", "title", "content", "agent"]}},
    {"name": "endeavor_memory_record_update", "title": "Update ENDMEMEX durable record", "description": "WRITE. Update an existing durable record's truth fields or independent action_state. Returns the updated record as JSON text. Resolving/accepting a record defaults action_state to done.", "annotations": WRITE_ANNOTATIONS, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "id": {"type": "string", "minLength": 1, "pattern": "^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$"},
        "project": PROJECT_PROPERTY,
        "type": {"type": "string", "enum": ["audit", "fix", "verification", "decision", "knowledge"]},
        "title": {"type": "string", "minLength": 1},
        "content": {"type": "string", "minLength": 1},
        "status": {"type": "string", "enum": ["open", "current", "resolved", "accepted", "superseded"]},
        "action_state": {"type": "string", "enum": ["actionable", "deferred", "blocked", "nonactionable", "done"]},
        "metadata": {"type": "object", "description": "Replacement metadata JSON object."},
        "agent": AGENT_PROPERTY,
        "confirm": CONFIRM_PROPERTY,
    }, "required": ["id", "agent"]}},
    {"name": "endeavor_memory_record_link", "title": "Link ENDMEMEX lifecycle records", "description": "WRITE. Create one typed lifecycle relation between existing durable records. Returns the source record context as JSON text.", "annotations": WRITE_ANNOTATIONS, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "source_id": {"type": "string", "pattern": "^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$"},
        "relation": {"type": "string", "enum": ["references", "resolves", "verifies", "supersedes", "contradicts", "duplicates"]},
        "target_id": {"type": "string", "pattern": "^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$"},
        "note": {"type": "string"},
        "agent": AGENT_PROPERTY,
        "confirm": CONFIRM_PROPERTY,
    }, "required": ["source_id", "relation", "target_id", "agent"]}},
    {"name": "endeavor_memory_session_close", "title": "Close ENDMEMEX session", "description": "WRITE. Mark exactly one session, selected by session ID or unambiguous project, completed or blocked. Returns the closed session identity/status as JSON text.", "annotations": WRITE_ANNOTATIONS, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "session": SESSION_PROPERTY,
        "project": PROJECT_PROPERTY,
        "status": {"type": "string", "enum": ["completed", "blocked"], "description": "Defaults to completed."},
        "agent": AGENT_PROPERTY,
        "confirm": CONFIRM_PROPERTY,
    }, "required": ["agent"], "oneOf": [{"required": ["session"]}, {"required": ["project"]}]}},
    {"name": "endeavor_memory_feedback", "title": "Record ENDMEMEX retrieval feedback", "description": "WRITE. Record whether returned Markdown IDs or durable record IDs were useful, so retrieval quality can be evaluated from production usage. Returns the feedback ID as JSON text.", "annotations": WRITE_ANNOTATIONS, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "agent": AGENT_PROPERTY,
        "query": {"type": "string", "minLength": 1},
        "results": {"type": "array", "items": {"oneOf": [{"type": "integer", "minimum": 1}, {"type": "string", "pattern": "^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$"}]}, "description": "Selected useful result IDs."},
        "useful": {"type": "boolean"},
        "note": {"type": "string"},
        "confirm": CONFIRM_PROPERTY,
    }, "required": ["agent", "query"]}},
    {"name": "endeavor_memory_event_poll", "title": "Poll durable ENDMEMEX events", "description": "READ ONLY. Returns unacknowledged completion/lifecycle events in monotonic ID order so a host can resume the owning conversation after background work. Persist the highest handled ID and acknowledge only after handling succeeds.", "annotations": READ_ONLY_ANNOTATIONS, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "after": {"type": "integer", "minimum": 0},
        "project": PROJECT_PROPERTY,
        "limit": {"type": "integer", "minimum": 1, "maximum": 500},
        "include_acked": {"type": "boolean"},
    }, "required": []}},
    {"name": "endeavor_memory_event_ack", "title": "Acknowledge durable ENDMEMEX event", "description": "WRITE, IDEMPOTENT. Acknowledge an event only after the host has handled it successfully. Returns the event with its durable acknowledgement as JSON text.", "annotations": WRITE_ANNOTATIONS | {"idempotentHint": True}, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "event_id": {"type": "integer", "minimum": 1},
        "agent": AGENT_PROPERTY,
        "confirm": CONFIRM_PROPERTY,
    }, "required": ["event_id", "agent"]}},
    {"name": "endeavor_presence_start", "title": "Announce active work (ENDMEMEX presence)", "description": "WRITE. Use only when the user explicitly asks agents to announce work or is coordinating multiple concurrent sessions. Announce (machine, agent, project[, instance]) as actively working, so a parallel local agent can see it via endeavor_presence_list instead of duplicating work. Returns the stored presence row as JSON text. Cross-host visibility is separate and best-effort through per-host sidecar files, never live. Once opted in, call once per task/session, not per turn.", "annotations": WRITE_ANNOTATIONS, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "agent": AGENT_PROPERTY,
        "project": PROJECT_PROPERTY,
        "task": {"type": "string", "description": "Short free-text of what this process is doing."},
        "instance": INSTANCE_PROPERTY,
        "session": {"type": "string", "minLength": 1, "description": "Optional ENDMEMEX session ID to associate with this presence row."},
        "confirm": CONFIRM_PROPERTY,
    }, "required": ["project", "agent"]}},
    {"name": "endeavor_presence_heartbeat", "title": "Refresh ENDMEMEX presence", "description": "WRITE. Only use after an opted-in presence_start. Refresh the (machine, agent, project[, instance]) presence row so it is not shown stale. Returns {\"updated\": bool} as JSON text; false means that identity has no active presence row -- it either never called presence_start or already called presence_stop (a no-op, not an error; a heartbeat never resurrects a stopped row, call presence_start again to resume). Refresh at the checkpoint cadence; do not add a polling loop.", "annotations": WRITE_ANNOTATIONS, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "agent": AGENT_PROPERTY,
        "project": PROJECT_PROPERTY,
        "task": {"type": "string", "description": "Update the task description; omit to just refresh the timestamp."},
        "instance": INSTANCE_PROPERTY,
        "confirm": CONFIRM_PROPERTY,
    }, "required": ["project", "agent"]}},
    {"name": "endeavor_presence_stop", "title": "Clear ENDMEMEX presence", "description": "WRITE. Stop a presence row created through the opt-in workflow, so it disappears from endeavor_presence_list. Returns {\"updated\": bool} as JSON text. Call at the natural end of that opted-in task/session; a crashed process without a stop call is still handled -- it just shows up flagged stale after 30 minutes rather than disappearing.", "annotations": WRITE_ANNOTATIONS, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "agent": AGENT_PROPERTY,
        "project": PROJECT_PROPERTY,
        "instance": INSTANCE_PROPERTY,
        "confirm": CONFIRM_PROPERTY,
    }, "required": ["project", "agent"]}},
    {"name": "endeavor_presence_list", "title": "List who is working (ENDMEMEX presence)", "description": "READ ONLY. Returns JSON text with \"local\" (this machine's live, real-time rows) and \"remote\" (every other machine's last-mirrored sidecar snapshot, source:\"sidecar\", staleness-flagged). Treat \"remote\" as last-known, never as proof of current activity. Call before starting parallel work on a project to check whether another agent is already on it.", "annotations": READ_ONLY_ANNOTATIONS, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
        "project": PROJECT_PROPERTY,
    }, "required": []}},
    {"name": "endeavor_sync_status", "title": "Check last-known write time per machine", "description": "READ ONLY. Returns JSON text with the last write time/command this host and other hosts have mirrored to their own sync-freshness sidecars. It is informational only: a lower bound on when another host wrote, never a synchronization guarantee or write authorization.", "annotations": READ_ONLY_ANNOTATIONS, "inputSchema": {"type": "object", "additionalProperties": False, "properties": {}, "required": []}},
]

TOOL_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def run(args: list[str]) -> str:
    """Run the database backend with this MCP server's interpreter and a bound.

    ``python3`` is not a portable executable name (notably on Windows), and
    can select a different environment from the MCP process on macOS/Linux.
    """
    try:
        result = subprocess.run(
            [sys.executable, str(DB), *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=DB_COMMAND_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return f"[error] endeavor_db command timed out after {DB_COMMAND_TIMEOUT_S}s"
    except OSError as exc:
        detail = str(exc) or exc.__class__.__name__
        return f"[error] failed to launch endeavor_db: {detail}"
    return result.stdout.strip() if result.returncode == 0 else f"[error] {result.stderr.strip() or result.stdout.strip()}"


def _validate_value(name: str, value: object, schema: dict) -> str | None:
    alternatives = schema.get("oneOf")
    if alternatives:
        if any(_validate_value(name, value, option) is None for option in alternatives):
            return None
        return f"{name} does not match any allowed type"
    expected = schema.get("type")
    if expected == "string":
        if not isinstance(value, str):
            return f"{name} must be a string"
        if len(value) < schema.get("minLength", 0):
            return f"{name} must not be empty"
        if schema.get("pattern") and not re.fullmatch(schema["pattern"], value):
            return f"{name} has an invalid format"
    elif expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{name} must be an integer"
        if "minimum" in schema and value < schema["minimum"]:
            return f"{name} must be >= {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{name} must be <= {schema['maximum']}"
    elif expected == "boolean" and not isinstance(value, bool):
        return f"{name} must be a boolean"
    elif expected == "array":
        if not isinstance(value, list):
            return f"{name} must be an array"
        item_schema = schema.get("items", {})
        for index, item in enumerate(value):
            error = _validate_value(f"{name}[{index}]", item, item_schema)
            if error:
                return error
    elif expected == "object" and not isinstance(value, dict):
        return f"{name} must be an object"
    if "enum" in schema and value not in schema["enum"]:
        return f"{name} must be one of: {', '.join(schema['enum'])}"
    return None


def validate_arguments(name: str, args: object) -> str | None:
    tool = TOOL_BY_NAME.get(name)
    if tool is None:
        return f"unknown Endeavor memory tool: {name}"
    if not isinstance(args, dict):
        return "arguments must be an object"
    schema = tool["inputSchema"]
    properties = schema.get("properties", {})
    unknown = sorted(set(args) - set(properties))
    if unknown:
        return f"unknown argument(s): {', '.join(unknown)}"
    missing = [key for key in schema.get("required", []) if key not in args]
    if missing:
        return f"missing required argument(s): {', '.join(missing)}"
    for key, value in args.items():
        error = _validate_value(key, value, properties[key])
        if error:
            return error
    if name == "endeavor_memory_pending":
        has_project = bool(args.get("project"))
        all_projects = args.get("all_projects") is True
        if has_project == all_projects:
            return "provide exactly one of project or all_projects:true"
    if name == "endeavor_memory_handoff":
        modes = sum((bool(args.get("project")), bool(args.get("session")), args.get("all_paused") is True))
        if modes != 1:
            return "provide exactly one of project, session, or all_paused:true"
    if name == "endeavor_memory_record_update":
        mutable = {"project", "type", "title", "content", "status", "action_state", "metadata"}
        if not mutable.intersection(args):
            return "provide at least one record field to update"
    if name == "endeavor_memory_session_close":
        if bool(args.get("project")) == bool(args.get("session")):
            return "provide exactly one of project or session"
    return None


def call(name: str, args: object) -> str:
    validation_error = validate_arguments(name, args)
    if validation_error:
        return f"[error] {validation_error}"
    assert isinstance(args, dict)
    if name == "endeavor_memory_query":
        cmd = ["query", args["query"], "--json"]
        if args.get("project"): cmd += ["--project", args["project"]]
        if args.get("category"): cmd += ["--category", args["category"]]
        if args.get("status"): cmd += ["--status", args["status"]]
        if args.get("module"): cmd += ["--module", args["module"]]
        if args.get("bug_id"): cmd += ["--bug-id", args["bug_id"]]
        if args.get("session_label"): cmd += ["--session-label", args["session_label"]]
        if args.get("semantic"): cmd += ["--semantic", args["semantic"]]
        if args.get("limit"): cmd += ["--limit", str(args["limit"])]
        if args.get("compact", True): cmd += ["--compact"]
        cmd += ["--check-stale"] if args.get("check_stale", True) else ["--no-check-stale"]
        return run(cmd)
    if name == "endeavor_memory_readiness":
        return run(["readiness", "--project", args["project"], "--json"])
    if name == "endeavor_memory_pack":
        cmd = ["pack", "--project", args["project"], "--json"]
        if args.get("session"): cmd += ["--session", args["session"]]
        if args.get("budget"): cmd += ["--budget", str(args["budget"])]
        return run(cmd)
    if name == "endeavor_memory_pending":
        has_project = bool(args.get("project"))
        cmd = ["pending", "--json"]
        cmd += ["--project", args["project"]] if has_project else ["--all-projects"]
        return run(cmd)
    if name == "endeavor_memory_handoff":
        if args.get("all_paused") is True:
            return run(["handoff", "--all-paused", "--json"])
        scope = ["--session", args["session"]] if args.get("session") else ["--project", args["project"]]
        return run(["handoff", *scope, "--json"])
    if name == "endeavor_memory_timeline":
        cmd = ["timeline", "--json"]
        if args.get("project"): cmd += ["--project", args["project"]]
        if args.get("agent"): cmd += ["--agent", args["agent"]]
        if args.get("status"): cmd += ["--status", args["status"]]
        if args.get("session"): cmd += ["--session", args["session"]]
        if args.get("limit"): cmd += ["--limit", str(args["limit"])]
        if args.get("oldest_first"): cmd += ["--oldest-first"]
        return run(cmd)
    if name == "endeavor_memory_record_show":
        return run(["record-show", args["id"]])
    if name == "endeavor_memory_record_search":
        cmd = ["record-search", args["query"]]
        if args.get("project"): cmd += ["--project", args["project"]]
        if args.get("type"): cmd += ["--type", args["type"]]
        if args.get("limit"): cmd += ["--limit", str(args["limit"])]
        if args.get("current_only"): cmd += ["--current-only"]
        return run(cmd)
    if name == "endeavor_memory_bootstrap":
        cmd = ["bootstrap", "--project", args["project"], "--json"]
        if args.get("session"): cmd += ["--session", args["session"]]
        if args.get("include_pending"): cmd += ["--include-pending"]
        return run(cmd)
    if name == "endeavor_memory_checkpoint":
        cmd = ["checkpoint", "--project", args["project"], "--agent", args["agent"], "--summary", args["summary"]]
        if args.get("session"): cmd += ["--session", args["session"]]
        if args.get("goal"): cmd += ["--goal", args["goal"]]
        if args.get("work_done"): cmd += ["--work-done", args["work_done"]]
        if args.get("current_state"): cmd += ["--current-state", args["current_state"]]
        if args.get("next_steps"): cmd += ["--next-steps", args["next_steps"]]
        if args.get("blockers"): cmd += ["--blockers", args["blockers"]]
        if args.get("status"): cmd += ["--status", args["status"]]
        for f in args.get("files") or []: cmd += ["--file", f]
        if args.get("auto_files"): cmd += ["--auto-files"]
        for c in args.get("commands") or []: cmd += ["--command", c]
        for v in args.get("verify") or []: cmd += ["--verify", v]
        if args.get("pinned"): cmd += ["--pin"]
        return run(cmd)
    if name == "endeavor_memory_pin_checkpoint":
        subcmd = "pin-checkpoint" if args.get("pinned", True) else "unpin-checkpoint"
        return run([subcmd, str(args["checkpoint_id"]), "--agent", args["agent"]])
    if name == "endeavor_memory_record_add":
        cmd = [
            "record-add", "--project", args["project"], "--type", args["type"],
            "--title", args["title"], "--content", args["content"], "--agent", args["agent"],
        ]
        if args.get("id"): cmd += ["--id", args["id"]]
        if args.get("status"): cmd += ["--status", args["status"]]
        if args.get("action_state"): cmd += ["--action-state", args["action_state"]]
        if args.get("source"): cmd += ["--source", args["source"]]
        if args.get("link"): cmd += ["--link", args["link"]]
        for link in args.get("links") or []: cmd += ["--link", link]
        return run(cmd)
    if name == "endeavor_memory_record_update":
        cmd = ["record-update", args["id"], "--agent", args["agent"]]
        if args.get("project"): cmd += ["--project", args["project"]]
        if args.get("type"): cmd += ["--type", args["type"]]
        if args.get("title"): cmd += ["--title", args["title"]]
        if args.get("content"): cmd += ["--content", args["content"]]
        if args.get("status"): cmd += ["--status", args["status"]]
        if args.get("action_state"): cmd += ["--action-state", args["action_state"]]
        if "metadata" in args: cmd += ["--metadata", json.dumps(args["metadata"], ensure_ascii=False)]
        return run(cmd)
    if name == "endeavor_memory_record_link":
        cmd = [
            "record-link", args["source_id"], args["relation"], args["target_id"],
            "--agent", args["agent"],
        ]
        if args.get("note"): cmd += ["--note", args["note"]]
        return run(cmd)
    if name == "endeavor_memory_session_close":
        cmd = ["session-close", "--agent", args["agent"]]
        cmd += ["--session", args["session"]] if args.get("session") else ["--project", args["project"]]
        if args.get("status"): cmd += ["--status", args["status"]]
        return run(cmd)
    if name == "endeavor_memory_feedback":
        cmd = ["feedback", "--agent", args["agent"], "--query", args["query"]]
        for result_id in args.get("results") or []: cmd += ["--result", str(result_id)]
        if "useful" in args: cmd += ["--useful", "yes" if args["useful"] else "no"]
        if args.get("note"): cmd += ["--note", args["note"]]
        return run(cmd)
    if name == "endeavor_memory_event_poll":
        cmd = ["event-poll", "--json"]
        if args.get("after") is not None: cmd += ["--after", str(args["after"])]
        if args.get("project"): cmd += ["--project", args["project"]]
        if args.get("limit"): cmd += ["--limit", str(args["limit"])]
        if args.get("include_acked"): cmd += ["--include-acked"]
        return run(cmd)
    if name == "endeavor_memory_event_ack":
        return run(["event-ack", str(args["event_id"]), "--agent", args["agent"]])
    if name == "endeavor_presence_start":
        cmd = ["presence-start", "--agent", args["agent"], "--project", args["project"]]
        if args.get("task"): cmd += ["--task", args["task"]]
        if args.get("instance"): cmd += ["--instance", args["instance"]]
        if args.get("session"): cmd += ["--session", args["session"]]
        return run(cmd)
    if name == "endeavor_presence_heartbeat":
        cmd = ["presence-heartbeat", "--agent", args["agent"], "--project", args["project"]]
        if args.get("task") is not None: cmd += ["--task", args["task"]]
        if args.get("instance"): cmd += ["--instance", args["instance"]]
        return run(cmd)
    if name == "endeavor_presence_stop":
        cmd = ["presence-stop", "--agent", args["agent"], "--project", args["project"]]
        if args.get("instance"): cmd += ["--instance", args["instance"]]
        return run(cmd)
    if name == "endeavor_presence_list":
        cmd = ["presence", "--json"]
        if args.get("project"): cmd += ["--project", args["project"]]
        return run(cmd)
    if name == "endeavor_sync_status":
        return run(["sync-status", "--json"])
    return "[error] unknown Endeavor memory tool"


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def serve() -> None:
    for line in sys.stdin:
        request: object = {}
        request_id = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            method, request_id = request.get("method"), request.get("id")
            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "endmemex", "version": "1.2"},
                    "instructions": SERVER_INSTRUCTIONS,
                }
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "ping":
                result = {}
            elif method == "tools/call":
                params = request.get("params", {})
                output = call(params.get("name", ""), params.get("arguments", {}))
                result = {"content": [{"type": "text", "text": output}]}
                if output.startswith("[error]"):
                    result["isError"] = True
            else:
                if request_id is None:
                    continue
                raise JsonRpcError(-32601, f"method not found: {method}")
            if request_id is not None:
                print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)
        except JsonRpcError as exc:
            print(json.dumps({
                "jsonrpc": "2.0", "id": request_id,
                "error": {"code": exc.code, "message": str(exc)},
            }), flush=True)
        except Exception as exc:
            print(json.dumps({
                "jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32000, "message": str(exc)},
            }), flush=True)


if __name__ == "__main__":
    serve()
