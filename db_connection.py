"""Low-level SQLite connection and schema probes for ENDEAVOR Memory."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Callable

_SQL_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_SQL_LINE_COMMENT_RE = re.compile(r"--[^\n]*")


def open_connection(
    path: Path,
    *,
    read_only: bool = False,
    connect_fn: Callable[..., sqlite3.Connection] = sqlite3.connect,
) -> sqlite3.Connection:
    """Open and configure a database without depending on the CLI façade."""
    if read_only:
        # ``Path.as_uri`` percent-encodes URI metacharacters in real filenames
        # (notably ?, #, %, and spaces). Interpolating ``path`` directly made
        # SQLite treat the first ? as a query boundary, so a read-only request
        # could silently open/create a sibling database with a truncated name.
        conn = connect_fn(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=15)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = connect_fn(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if read_only:
        conn.execute("PRAGMA query_only = ON")
    else:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?", (name,)
    ).fetchone() is not None


def table_count(
    conn: sqlite3.Connection,
    name: str,
    *,
    table_exists_fn: Callable[[sqlite3.Connection, str], bool] = table_exists,
) -> int:
    return conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] if table_exists_fn(conn, name) else 0


def execute_sql_script(
    conn: sqlite3.Connection,
    script: str,
    *,
    complete_statement_fn: Callable[[str], bool] = sqlite3.complete_statement,
) -> None:
    """Execute a SQL script without sqlite3.executescript's implicit COMMIT."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if complete_statement_fn(statement):
            if statement.strip():
                conn.execute(statement)
            statement = ""
    # A trailing SQL comment (no statement after it) never completes per
    # sqlite3.complete_statement, but it isn't an incomplete statement either
    # -- strip comments before deciding whether real, unexecuted SQL remains.
    if _SQL_LINE_COMMENT_RE.sub("", _SQL_BLOCK_COMMENT_RE.sub("", statement)).strip():
        raise sqlite3.OperationalError("incomplete SQL statement in schema")


def database_schema_version(
    conn: sqlite3.Connection,
    *,
    table_exists_fn: Callable[[sqlite3.Connection, str], bool] = table_exists,
) -> str | None:
    if not table_exists_fn(conn, "database_meta"):
        return None
    row = conn.execute(
        "SELECT value FROM database_meta WHERE key = 'schema_version'"
    ).fetchone()
    return row[0] if row else None
