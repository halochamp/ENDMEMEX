"""Activity-log formatting, export, and retention primitives.

Every function here takes its dependencies as parameters rather than reading
a module global (e.g. a retention cap or export limit) directly. That keeps
the existing `endeavor_db.py` facade functions -- which tests patch via
`mock.patch.object(endeavor_db, ...)` -- as the real, load-bearing seam: a
facade wrapper resolves its own patchable default and forwards it in, so a
patch on `endeavor_db.MAX_ACTIVITY_LOG_ROWS` (or on the facade function
itself) still controls real behavior after this extraction.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

ACTIVITY_EXPORT_NAME = "ACTIVITY.md"
ACTIVITY_EXPORT_LIMIT = 50
ACTIVITY_EXPORT_MAX = 500


def activity_export_path(db_path: Path) -> Path:
    """The export lives next to the SQLite file it describes, so test
    databases write into their own temp directory, never the real one."""
    return db_path.parent / ACTIVITY_EXPORT_NAME


def local_stamp(iso_timestamp: str) -> str:
    try:
        return datetime.fromisoformat(iso_timestamp).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso_timestamp


def one_line(text: str, max_chars: int = 160) -> str:
    collapsed = re.sub(r"\s+", " ", str(text)).strip()
    return collapsed if len(collapsed) <= max_chars else collapsed[: max_chars - 1] + "…"


def describe_activity(conn: sqlite3.Connection, action: str, detail: dict[str, Any]) -> str:
    """One human sentence per logged action, enriched from the live tables.

    Every branch tolerates missing rows (retention may have pruned an old
    checkpoint; a record may have been deleted by hand) -- the log line then
    falls back to its raw identifiers instead of failing the whole export.
    """
    if action == "checkpoint":
        sequence = detail.get("sequence")
        row = conn.execute(
            "SELECT summary FROM checkpoints WHERE session_id = ? AND sequence = ?",
            (detail.get("session_id"), sequence),
        ).fetchone()
        session = conn.execute(
            "SELECT goal, status FROM sessions WHERE id = ?", (detail.get("session_id"),)
        ).fetchone()
        summary = f'"{one_line(row["summary"])}"' if row else "(checkpoint pruned by retention)"
        suffix = f" [session {session['status']}: {one_line(session['goal'], 60)}]" if session else ""
        return f"#{sequence} {summary}{suffix}"
    if action == "session_start":
        return f'goal: "{one_line(detail.get("goal", ""))}"'
    if action == "session_close":
        return f"status: {detail.get('status', '?')}"
    if action in ("memory_record_add", "memory_record_update"):
        record_id = detail.get("record_id", "?")
        row = conn.execute(
            "SELECT record_type, status, title FROM memory_records WHERE id = ?", (record_id,)
        ).fetchone()
        if row is None:
            return record_id
        return f'{record_id} [{row["record_type"]}/{row["status"]}] "{one_line(row["title"])}"'
    if action == "memory_relation_add":
        return f"{detail.get('source', '?')} {detail.get('relation', '?')} {detail.get('target', '?')}"
    if action == "ingest":
        return f"{detail.get('source', '?')} ({detail.get('entries', '?')} entries)"
    if action == "prune":
        return f"{detail.get('documents', '?')} indexed document(s) removed"
    if action == "embedding_batch_failed":
        return f"reason: {detail.get('reason', '?')}"
    return one_line(json.dumps(detail, ensure_ascii=False, sort_keys=True)) if detail else ""


def render_activity(conn: sqlite3.Connection, limit: int, *, export_max: int = ACTIVITY_EXPORT_MAX) -> str:
    limit = max(1, min(limit, export_max))
    total = conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0]
    rows = conn.execute(
        "SELECT agent, action, project, detail, created_at FROM activity_log ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    lines = [
        "# ENDMEMEX — Recent Activity",
        "",
        f"Latest {len(rows)} of {total} logged write actions, newest first; times are local.",
        "Auto-refreshed after every write command; regenerate manually with "
        "`python3 ENDMEMEX/endeavor_db.py activity`. Do not edit — generated file.",
        "",
    ]
    for row in rows:
        try:
            detail = json.loads(row["detail"])
        except (TypeError, json.JSONDecodeError):
            detail = {}
        description = describe_activity(conn, row["action"], detail if isinstance(detail, dict) else {})
        project = row["project"] or "-"
        lines.append(
            f"- {local_stamp(row['created_at'])} · {row['agent']} · {row['action']} · {project}"
            + (f" — {description}" if description else "")
        )
    if not rows:
        lines.append("(no activity logged yet)")
    return "\n".join(lines) + "\n"


def activity_line(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Format one activity_log row the same way render_activity does, for
    reuse by `activity --follow`'s poll loop."""
    try:
        detail = json.loads(row["detail"])
    except (TypeError, json.JSONDecodeError):
        detail = {}
    description = describe_activity(conn, row["action"], detail if isinstance(detail, dict) else {})
    project = row["project"] or "-"
    return (
        f"{local_stamp(row['created_at'])} · {row['agent']} · {row['action']} · {project}"
        + (f" — {description}" if description else "")
    )


def poll_activity_since(
    conn: sqlite3.Connection, after_id: int, project: str | None = None, limit: int = 200,
) -> list[sqlite3.Row]:
    """New activity_log rows with id > after_id, oldest first -- the single
    query `activity --follow` re-runs on each poll tick."""
    filters = ["id > ?"]
    params: list[Any] = [after_id]
    if project:
        filters.append("project = ?")
        params.append(project)
    params.append(max(1, min(limit, 500)))
    return conn.execute(
        f"SELECT id, agent, action, project, detail, created_at FROM activity_log "
        f"WHERE {' AND '.join(filters)} ORDER BY id ASC LIMIT ?",
        params,
    ).fetchall()


def prune_activity_log(conn: sqlite3.Connection, keep: int) -> int:
    """Keep only the newest `keep` activity_log rows. Unlike memory_records
    (a durable audit trail meant to persist forever) or checkpoints (already
    capped per-session in add_checkpoint), activity_log is a low-value write
    log feeding only the human-readable digest and `activity --follow` --
    nothing references an old row by foreign key, so trimming the tail is
    safe. A no-op query under the cap; cheap enough to run on every write.

    Always takes an explicit `keep` -- the caller (the endeavor_db facade)
    resolves the default from its own patchable MAX_ACTIVITY_LOG_ROWS global
    at call time, the same way test_prune_activity_log_keeps_only_newest_rows
    patches it, so that patch point must stay in endeavor_db.py, not here.
    """
    with conn:
        cursor = conn.execute(
            """DELETE FROM activity_log WHERE id IN (
                   SELECT id FROM activity_log ORDER BY id DESC LIMIT -1 OFFSET ?
               )""",
            (keep,),
        )
    return cursor.rowcount
