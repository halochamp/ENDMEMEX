"""Session lifecycle, handoff/pending-work read paths, and the
presence-sidecar/sync-freshness helpers that take their directory as a
parameter rather than reading a global.

Scoped by a transitive-closure AST call-graph check over session,
checkpoint, and presence-adjacent candidates in endeavor_db.py: some reach a monkeypatched config constant
(MAX_CHECKPOINTS, MAX_TOTAL_CHECKPOINTS, MAX_PINNED_CHECKPOINTS_WARN,
PRESENCE_DIR, SYNC_FRESHNESS_DIR) either directly or through another
candidate in the set (e.g. add_checkpoint -> prune_checkpoints_globally ->
MAX_TOTAL_CHECKPOINTS) and stay in endeavor_db.py. The 12 here are
transitively clean: zero monkeypatch references of their own, and no chain
through another candidate reaches a patched name. PRESENCE_STALE_SEC/
PRESENCE_ROW_MAX_AGE_DAYS/SIDECAR_TEMP_MAX_AGE_SEC are imported directly
(never monkeypatch targets, confirmed by grep against test_endeavor_db.py --
same tripwire-test treatment as MAX_MEMORY_CONTEXT_RECORDS).
"""
from __future__ import annotations

import fcntl
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import PRESENCE_ROW_MAX_AGE_DAYS, PRESENCE_STALE_SEC, SIDECAR_TEMP_MAX_AGE_SEC
from errors import AmbiguousSessionError, SessionNotFoundError
from primitives import json_text, now_utc


def _insert_session(
    conn: sqlite3.Connection, session_id: str, project: str, goal: str, agent: str,
    metadata: dict[str, Any], timestamp: str,
) -> None:
    """Bare inserts, no transaction management — callers control the
    surrounding transaction (see start_session vs. the locked auto-start
    path in resolve_or_start_checkpoint_session)."""
    conn.execute(
        "INSERT INTO sessions VALUES(?, ?, ?, 'active', ?, ?, ?, ?, ?)",
        (session_id, project, goal, agent, agent, json_text(metadata), timestamp, timestamp),
    )
    conn.execute(
        "INSERT INTO activity_log(agent, action, project, detail, created_at) VALUES(?, 'session_start', ?, ?, ?)",
        (agent, project, json_text({"session_id": session_id, "goal": goal}), timestamp),
    )


def start_session(conn: sqlite3.Connection, project: str, goal: str, agent: str, metadata: dict[str, Any]) -> str:
    session_id = str(uuid.uuid4())
    with conn:
        _insert_session(conn, session_id, project, goal, agent, metadata, now_utc())
    return session_id


def resolve_session(conn: sqlite3.Connection, session_id: str | None, project: str | None) -> sqlite3.Row:
    if session_id:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is not None and project and row["project"] != project:
            raise ValueError(f"session {session_id} belongs to project {row['project']}, not {project}")
    elif project:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE project = ? AND status IN ('active', 'paused', 'blocked') "
            "ORDER BY updated_at DESC, id DESC LIMIT 2",
            (project,),
        ).fetchall()
        if len(rows) > 1:
            raise AmbiguousSessionError(
                f"project {project} has multiple active/paused/blocked sessions; provide --session"
            )
        row = rows[0] if rows else None
    else:
        raise ValueError("provide --session or --project")
    if row is None:
        raise SessionNotFoundError("no matching active/paused/blocked session")
    return row


def resolve_or_start_checkpoint_session(
    conn: sqlite3.Connection, session_id: str | None, project: str | None, goal: str | None, agent: str,
) -> sqlite3.Row:
    """Resolve the checkpoint's target session, auto-starting one when there
    isn't one yet — collapsing handoff -> session-start -> checkpoint into a
    single call for the common single-session task.

    Only triggers when resolution was by --project (an explicit --session
    that doesn't exist is a real error, e.g. a typo, not "not created yet")
    and the caller opted in with --goal; otherwise behaves exactly like
    resolve_session.

    The check-then-create is serialized with BEGIN IMMEDIATE: without it, two
    agents both calling `checkpoint --project X --goal ...` for the same
    project's first checkpoint could each see "no active session" and each
    start one, splitting history into two parallel sessions.
    """
    if session_id or not project or not goal:
        return resolve_session(conn, session_id, project)
    conn.execute("BEGIN IMMEDIATE")
    try:
        try:
            row = resolve_session(conn, None, project)
        except SessionNotFoundError:
            new_session_id = str(uuid.uuid4())
            _insert_session(conn, new_session_id, project, goal, agent, {}, now_utc())
            row = resolve_session(conn, new_session_id, None)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return row


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("metadata", "files_changed", "commands_run", "verification"):
        if key in data:
            try:
                data[key] = json.loads(data[key])
            except (TypeError, json.JSONDecodeError):
                pass
    return data


def handoff(conn: sqlite3.Connection, session_id: str | None, project: str | None) -> dict[str, Any]:
    session = resolve_session(conn, session_id, project)
    checkpoint = conn.execute(
        "SELECT * FROM checkpoints WHERE session_id = ? ORDER BY sequence DESC LIMIT 1", (session["id"],)
    ).fetchone()
    return {"session": row_dict(session), "checkpoint": row_dict(checkpoint) if checkpoint else None}


def checkpoint_timeline(
    conn: sqlite3.Connection,
    *,
    project: str | None = None,
    agent: str | None = None,
    session_status: str | None = None,
    session_id: str | None = None,
    limit: int = 100,
    oldest_first: bool = False,
    per_session_cap: int,
    total_cap: int,
) -> dict[str, Any]:
    """Read-only checkpoint timeline: one row per checkpoint, joined to its
    session for project/goal/status context.

    Caps are taken as explicit parameters (never read from config directly)
    so this function stays in the transitively-clean set described in the
    module docstring -- the caller (endeavor_db.py's facade) resolves the
    real MAX_CHECKPOINTS/MAX_TOTAL_CHECKPOINTS and passes them in, the same
    way a test can monkeypatch those constants and still control this
    function's retention_notice text.

    checkpoints has no status column of its own (schema.sql), so
    ``checkpoint_status`` here is derived per row -- "current" for the
    newest (highest-sequence) checkpoint of its session, "historical"
    otherwise -- not a stored value. ``session_status`` filters on
    sessions.status (the same active/paused/completed/blocked enum the
    `checkpoint`/`session-close` subcommands already write).
    """
    clauses: list[str] = []
    params: list[Any] = []
    if project:
        clauses.append("s.project = ?")
        params.append(project)
    if agent:
        clauses.append("c.agent = ?")
        params.append(agent)
    if session_status:
        clauses.append("s.status = ?")
        params.append(session_status)
    if session_id:
        clauses.append("c.session_id = ?")
        params.append(session_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM checkpoints c JOIN sessions s ON s.id = c.session_id {where}",
        params,
    ).fetchone()[0]

    effective_limit = max(1, min(limit, 500))
    direction = "ASC" if oldest_first else "DESC"
    rows = conn.execute(
        f"""SELECT
                c.id AS checkpoint_id, c.session_id AS session_id, c.sequence AS sequence,
                s.project AS project, s.goal AS goal, c.agent AS agent,
                CASE WHEN c.sequence = (
                    SELECT MAX(c2.sequence) FROM checkpoints c2 WHERE c2.session_id = c.session_id
                ) THEN 'current' ELSE 'historical' END AS checkpoint_status,
                s.status AS session_status, c.summary AS summary, c.work_done AS work_done,
                c.current_state AS current_state, c.blockers AS blockers, c.next_steps AS next_steps,
                c.files_changed AS files_changed, c.commands_run AS commands_run,
                c.verification AS verification, c.pinned AS pinned, c.created_at AS created_at
            FROM checkpoints c JOIN sessions s ON s.id = c.session_id {where}
            ORDER BY c.created_at {direction}, c.id {direction}
            LIMIT ?""",
        [*params, effective_limit],
    ).fetchall()

    def _record(row: sqlite3.Row) -> dict[str, Any]:
        record = row_dict(row)
        # New writes validate these shapes, but historical/manual rows can
        # still contain a scalar instead of an array or non-string members
        # inside an array. Normalize both layers so timeline JSON keeps a
        # string-array contract and its Markdown renderer cannot fail at
        # join()/Path(item).
        for key in ("files_changed", "commands_run", "verification"):
            if not isinstance(record[key], list):
                record[key] = [record[key]] if record[key] else []
            record[key] = [
                item if isinstance(item, str) else json_text(item)
                for item in record[key]
            ]
        return record

    return {
        "records": [_record(row) for row in rows],
        "count": len(rows),
        "total_matching": total,
        "truncated": total > len(rows),
        "limit": effective_limit,
        "order": "oldest_first" if oldest_first else "newest_first",
        "filters": {
            "project": project, "agent": agent, "status": session_status, "session_id": session_id,
        },
        "retention_notice": (
            f"Covers only currently-retained checkpoints (up to {per_session_cap} per session and "
            f"{total_cap} total globally; pinned checkpoints are exempt from both caps). "
            "activity_log is pruned more aggressively and is never the source of truth here."
        ),
    }


def render_checkpoint_timeline(data: dict[str, Any], *, root: Path | None = None) -> str:
    """Human-readable Markdown for checkpoint_timeline's return value.

    Pure formatting -- no database access -- so it takes the already-built
    dict rather than a connection. ``root`` is optional and, when given,
    lets files_changed entries that resolve to a real file on disk (either
    already absolute or relative to root) render as clickable file:// links;
    anything that doesn't resolve stays plain text rather than guessing.
    """
    def _evidence_list(items: list[str], *, as_links: bool) -> str:
        if not items:
            return "(none)"
        if not as_links or root is None:
            return ", ".join(items)
        rendered = []
        for item in items:
            path = Path(item)
            candidate = path if path.is_absolute() else root / item
            try:
                exists = candidate.exists()
            except OSError:
                exists = False
            rendered.append(f"[{item}](file://{candidate.resolve()})" if exists else item)
        return ", ".join(rendered)

    filters = data["filters"]
    filter_line = ", ".join(f"{key}={value or 'any'}" for key, value in filters.items())
    lines = [
        "# ENDMEMEX — Checkpoint Timeline",
        "",
        f"Showing {data['count']} of {data['total_matching']} matching checkpoint(s), "
        f"{data['order']} order" + (", truncated" if data["truncated"] else "") + ".",
        f"Filters: {filter_line}.",
        data["retention_notice"],
        "",
    ]
    if not data["records"]:
        lines.append("(no matching checkpoints)")
    for record in data["records"]:
        pinned_note = " · pinned" if record["pinned"] else ""
        lines.append(
            f"## #{record['checkpoint_id']} — {record['project']} · {record['agent']} · {record['created_at']}"
        )
        lines.append(
            f"- Session: {record['session_id']} ({record['session_status']}) · "
            f"Checkpoint: {record['checkpoint_status']}{pinned_note}"
        )
        lines.append(f"- Goal: {record['goal']}")
        lines.append(f"- Summary: {record['summary']}")
        lines.append(f"- Work done: {record['work_done'] or '(not recorded)'}")
        lines.append(f"- Current state: {record['current_state'] or '(not recorded)'}")
        lines.append(f"- Next steps: {record['next_steps'] or '(not recorded)'}")
        lines.append(f"- Blockers: {record['blockers'] or '(not recorded)'}")
        lines.append(f"- Files changed: {_evidence_list(record['files_changed'], as_links=True)}")
        lines.append(f"- Commands run: {_evidence_list(record['commands_run'], as_links=False)}")
        lines.append(f"- Verification: {_evidence_list(record['verification'], as_links=False)}")
        lines.append("")
    return "\n".join(lines) + "\n"


def paused_handoffs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return the latest resumable checkpoint for every paused session.

    A project-scoped handoff succeeds only when one session matches. Session
    discovery needs this separate path so an agent can present every paused
    task for explicit user selection before carrying its session ID forward.
    """
    sessions = conn.execute(
        "SELECT * FROM sessions WHERE status = 'paused' ORDER BY updated_at DESC, id DESC"
    ).fetchall()
    result: list[dict[str, Any]] = []
    for session in sessions:
        checkpoint = conn.execute(
            "SELECT * FROM checkpoints WHERE session_id = ? ORDER BY sequence DESC LIMIT 1", (session["id"],)
        ).fetchone()
        result.append({"session": row_dict(session), "checkpoint": row_dict(checkpoint) if checkpoint else None})
    return result


def _pending_session_entries(conn: sqlite3.Connection, status: str, project: str | None) -> list[dict[str, Any]]:
    """Return durable session state for the pending-work view.

    This is intentionally separate from presence: a session can remain active
    after its process crashes, and a remote sidecar is only last-known state.
    """
    clauses = ["status = ?"]
    params: list[Any] = [status]
    if project:
        clauses.append("project = ?")
        params.append(project)
    sessions = conn.execute(
        f"SELECT * FROM sessions WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC, id",
        params,
    ).fetchall()
    result: list[dict[str, Any]] = []
    for session in sessions:
        checkpoint = conn.execute(
            "SELECT * FROM checkpoints WHERE session_id = ? ORDER BY sequence DESC LIMIT 1", (session["id"],)
        ).fetchone()
        checkpoint_data = row_dict(checkpoint) if checkpoint else None
        result.append({
            "kind": "session", "id": session["id"], "project": session["project"],
            "title": session["goal"], "updated_at": session["updated_at"], "status": status,
            "last_agent": session["last_agent"], "checkpoint_id": checkpoint_data["id"] if checkpoint_data else None,
            "checkpoint": checkpoint_data,
            "rank_reason": (
                "paused session can be resumed" if status == "paused"
                else "session has an explicit blocker" if status == "blocked"
                else "durable active session; presence is checked separately"
            ),
            "warning": "session has no checkpoint" if checkpoint_data is None else None,
        })
    return result


def _presence_row_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(data["last_heartbeat"])).total_seconds()
    data["stale"] = age > PRESENCE_STALE_SEC
    data["age_seconds"] = int(age)
    return data


def _prune_expired_presence_rows(conn: sqlite3.Connection, machine: str) -> None:
    """Bound presence growth by deleting this machine's rows older than three days.

    A real agent refreshes its heartbeat at least every 15 minutes; an active
    row older than this limit is a crashed/abandoned publisher, not live work.
    The same bound applies to stopped rows so fresh ``--instance`` values
    cannot accumulate indefinitely.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=PRESENCE_ROW_MAX_AGE_DAYS)).isoformat(timespec="seconds")
    conn.execute(
        "DELETE FROM agent_presence WHERE machine = ? AND last_heartbeat < ?",
        (machine, cutoff),
    )


@contextmanager
def _sidecar_lock(directory: Path, machine: str):
    """Same-machine callers (separate Codex/Claude/CLI subprocesses) can
    publish this machine's sidecar concurrently. Without serializing them,
    two writers sharing one temp path can interleave truncate/write/rename,
    and even with unique temp names the *last* os.replace to land wins
    regardless of which snapshot is actually newest -- a process that read
    the source data first but is scheduled to replace last can overwrite a
    fresher snapshot with a stale one. An advisory flock around
    read-snapshot + write + replace fixes both: only one publisher runs the
    sequence at a time, so whichever one enters last (necessarily after the
    others' reads have already happened) always publishes the freshest
    available data.

    Used by BOTH sidecar writers (presence and sync-freshness) -- they have
    the identical concurrency shape, so neither may skip it."""
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / f"{machine}.lock"
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _reap_stale_sidecar_temps(directory: Path) -> None:
    """A crash (or a failed os.replace) between write_text and replace leaves
    a `.<machine>.<pid>.<uuid>.json.tmp` behind. Those never match the
    `*.json` glob the readers use, so they accumulate invisibly. Only reap
    ones older than SIDECAR_TEMP_MAX_AGE_SEC: a concurrent publisher's
    in-flight temp is seconds old and must never be deleted out from under
    it. Best-effort -- a failure here is not worth failing a caller over."""
    cutoff = time.time() - SIDECAR_TEMP_MAX_AGE_SEC
    try:
        for temp in directory.glob("*.json.tmp"):
            try:
                if temp.stat().st_mtime < cutoff:
                    temp.unlink()
            except OSError:
                continue
    except OSError:
        pass
