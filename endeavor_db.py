#!/usr/bin/env python3
"""Shared SQLite knowledge and handoff store for Codex and Claude Code.

Standard-library only. The database uses WAL mode and a busy timeout so two
local agent processes can safely read/write the same workspace database.

Semantic search is an optional, gracefully-degrading enhancement: this file
never imports sentence-transformers/torch/numpy directly. Embedding vectors
are computed by a separate lazy-loaded companion process, embed_server.py
(see embed_config.py for the shared port/model constants), and this file
only talks to it over stdlib urllib — if the companion can't be reached,
every command still works with lexical FTS only.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import getpass
import hashlib
import json
import math
import os
import re
import shlex
import sqlite3
import struct
import subprocess
import sys
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import URLError
from urllib.request import Request, urlopen

from embed_config import EMBED_BASE_URL, EMBED_DIM, MODEL_NAME as EMBED_MODEL_NAME
from config import (
    # KNOWLEDGE_CATEGORIES itself has no reader here (only
    # KNOWLEDGE_FTS_IDENTITY_EXPRESSION, its derived value, is used below) --
    # kept as a deliberate facade re-export so `endeavor_db.KNOWLEDGE_CATEGORIES`
    # stays a valid identity-pinned name for test_config_extraction.py and any
    # external caller expecting it on this module. Not dead code; do not remove.
    # SQL_BATCH_SIZE likewise: every reader moved to record_lifecycle.py with
    # the record_lifecycle.py slice, kept here as the same class of facade name.
    # PRESENCE_ROW_MAX_AGE_DAYS/SIDECAR_TEMP_MAX_AGE_SEC likewise: every
    # reader moved to sessions.py with the sessions.py slice.
    ANN_BUILD_TIMEOUT_SEC, ANN_CANDIDATE_LIMIT, ANN_HELPER_PATH, ANN_QUERY_TIMEOUT_SEC,
    DEFAULT_DB, EMBED_BATCH_SIZE, EMBED_HEALTH_TIMEOUT_SEC, EMBED_LOG_PATH,
    EMBED_PYTHON_ENV, EMBED_PYTHON_PROBE_TIMEOUT_SEC, EMBED_REQUEST_TIMEOUT_SEC,
    EMBED_REQUIRED_MODULES, EMBED_START_LOCK_PATH, EMBED_STARTUP_TIMEOUT_SEC,
    HERE, INDEX_VERSION, KNOWLEDGE_CATEGORIES, KNOWLEDGE_FTS_IDENTITY_EXPRESSION,
    MAX_ACTIVITY_LOG_ROWS, MAX_CHECKPOINTS, MAX_CHUNK_CHARS, MAX_TEXTS_PER_REQUEST,
    MAX_MEMORY_CONTEXT_RECORDS, MAX_PINNED_CHECKPOINTS_WARN,
    MAX_TOTAL_CHECKPOINTS, MEMORY_ACTION_STATES, MEMORY_RECORD_STATUSES, MEMORY_RECORD_TYPES,
    MEMORY_RELATIONS, PACK_DEFAULT_BUDGET_CHARS, PRESENCE_DIR, PRESENCE_ROW_MAX_AGE_DAYS,
    PRESENCE_STALE_SEC, RESULT_CANDIDATES, ROOT, SCHEMA_PATH, SCHEMA_VERSION,
    SEMANTIC_CHUNKS_PER_RECORD_LIMIT, SEMANTIC_FULL_SCAN_LIMIT,
    SEED_SOURCES, SIDECAR_TEMP_MAX_AGE_SEC, SQL_BATCH_SIZE, SYNC_FRESHNESS_DIR,
)
from db_connection import (
    database_schema_version as _database_schema_version,
    execute_sql_script as _execute_sql_script,
    open_connection,
    table_count as _table_count,
    table_exists as _table_exists,
)
from activity import (
    ACTIVITY_EXPORT_MAX, ACTIVITY_EXPORT_NAME, ACTIVITY_EXPORT_LIMIT,
    activity_export_path as _activity_export_path,
    activity_line as _activity_line_text,
    poll_activity_since as _poll_activity_since,
    prune_activity_log as _prune_activity_log_rows,
    render_activity as _render_activity_text,
)
from documents import (
    MarkdownChunk,
    classify as _classify,
    document_title as _document_title,
    extract_metadata as _extract_metadata,
    markdown_chunks as _markdown_chunks_impl,
    split_large_section as _split_large_section_impl,
)
# AmbiguousSessionError: raised only inside sessions.py's resolve_session
# now, but tested directly as db.AmbiguousSessionError in test_endeavor_db.py
# -- kept as a facade re-export, same class as KNOWLEDGE_CATEGORIES above.
from errors import AmbiguousSessionError, SessionNotFoundError
from retrieval import (
    detect_intent,
    fts_expression,
    query_terms,
    typo_corrected_terms as _typo_corrected_terms_impl,
)
from migrations import (
    dedupe_symmetric_relations as _dedupe_symmetric_relations,
    ensure_memory_components as _ensure_memory_components,
    migrate_v5_fts as _migrate_v5_fts_impl,
    migrate_v9_fts as _migrate_v9_fts_impl,
    rebuild_fts as _rebuild_fts_impl,
)
from embeddings import (
    embed_identity_matches as _embed_identity_matches,
    embedding_hash,
    localhost_permission_denied as _localhost_permission_denied,
    pack_embedding,
    unpack_embedding,
    valid_embedding_blob,  # deliberate facade re-export: predates this refactor, still part of the public import surface
)
from cli_parser import build_parser as _build_parser_impl
from cli_validators import nonempty_text, pack_budget, positive_int
from primitives import (
    batched as _batched,
    json_text,
    normalize_memory_id,
    now_utc,
    parse_feedback_result_id,
    parse_relation_spec,
)
from record_lifecycle import (
    add_memory_relation,
    annotate_staleness as _annotate_staleness_impl,
    compact_result,
    memory_record_context,
    memory_relation_health,
    search_memory_records,
    _current_ids_map,
    _insert_memory_relation,
    # _memory_record_dict/_terminal_current_ids: no internal reader remains in
    # this module (both were already dead code before this extraction), but
    # _terminal_current_ids is tested directly as db._terminal_current_ids in
    # test_endeavor_db.py -- kept as facade re-exports, same as
    # KNOWLEDGE_CATEGORIES/SQL_BATCH_SIZE above.
    _memory_record_dict,
    _memory_records_dicts,
    _terminal_current_ids,
)
from sessions import (
    # _insert_session has no reader here or in test_endeavor_db.py -- both
    # of its original callers (start_session, resolve_or_start_checkpoint_
    # session) moved with it, so it is not re-exported at all.
    _pending_session_entries,
    _presence_row_dict,
    _prune_expired_presence_rows,
    _reap_stale_sidecar_temps,
    _sidecar_lock,
    checkpoint_timeline,
    handoff,
    paused_handoffs,
    render_checkpoint_timeline,
    resolve_or_start_checkpoint_session,
    resolve_session,
    row_dict,
    start_session,
)


def annotate_staleness(conn: sqlite3.Connection, results: list[dict[str, Any]]) -> None:
    return _annotate_staleness_impl(conn, results, root=ROOT)


def database_path(value: str | None = None) -> Path:
    return Path(value or os.getenv("ENDEAVOR_DB_PATH") or DEFAULT_DB).expanduser().resolve()


def connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    return open_connection(path, read_only=read_only, connect_fn=sqlite3.connect)


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return _table_exists(conn, name)


def table_count(conn: sqlite3.Connection, name: str) -> int:
    return _table_count(conn, name, table_exists_fn=table_exists)


def execute_sql_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute a SQL script without sqlite3.executescript's implicit COMMIT."""
    return _execute_sql_script(
        conn, script, complete_statement_fn=sqlite3.complete_statement
    )


def database_schema_version(conn: sqlite3.Connection) -> str | None:
    return _database_schema_version(conn, table_exists_fn=table_exists)


def initialize(conn: sqlite3.Connection, *, force: bool = False) -> None:
    if not force and database_schema_version(conn) == SCHEMA_VERSION:
        return
    if conn.in_transaction:
        raise sqlite3.OperationalError("database initialization requires no active transaction")
    try:
        # Keep DDL, component backfill, FTS migration, and the version marker
        # atomic. A rejected v3 lifecycle must not leave v4 immutability
        # triggers behind and make the old branch impossible to repair.
        conn.execute("BEGIN IMMEDIATE")
        execute_sql_script(conn, SCHEMA_PATH.read_text(encoding="utf-8"))
        _finish_initialize(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _finish_initialize(conn: sqlite3.Connection) -> None:
    _ensure_memory_components(conn)
    _dedupe_symmetric_relations(conn)
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_relations_symmetric
                    ON memory_relations(
                        relation,
                        CASE WHEN source_id < target_id THEN source_id ELSE target_id END,
                        CASE WHEN source_id < target_id THEN target_id ELSE source_id END
                    ) WHERE relation IN ('contradicts', 'duplicates')""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(knowledge)")}
    required_columns = {
        "metadata": "TEXT NOT NULL DEFAULT '{}'",
        "status": "TEXT NOT NULL DEFAULT ''",
        "bug_id": "TEXT NOT NULL DEFAULT ''",
        "module": "TEXT NOT NULL DEFAULT ''",
        "session_label": "TEXT NOT NULL DEFAULT ''",
        "parent_heading": "TEXT NOT NULL DEFAULT ''",
        "embedding": "BLOB",
        "embedding_hash": "TEXT NOT NULL DEFAULT ''",
    }
    for name, definition in required_columns.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE knowledge ADD COLUMN {name} {definition}")
    document_columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
    if "index_version" not in document_columns:
        conn.execute("ALTER TABLE documents ADD COLUMN index_version TEXT NOT NULL DEFAULT '1'")
    checkpoint_columns = {row[1] for row in conn.execute("PRAGMA table_info(checkpoints)")}
    if "pinned" not in checkpoint_columns:
        conn.execute("ALTER TABLE checkpoints ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
    record_columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_records)")}
    added_action_state = "action_state" not in record_columns
    if added_action_state:
        conn.execute(
            "ALTER TABLE memory_records ADD COLUMN action_state "
            "TEXT NOT NULL DEFAULT 'nonactionable'"
        )
        # Preserve the old explicit metadata escape hatch while making work
        # triage first-class.  Do not infer actionability from prose such as
        # 'FIXED'; that recreates the heuristic failure this migration fixes.
        conn.execute("""
            UPDATE memory_records
            SET action_state = CASE
                WHEN json_extract(metadata, '$.work_state') = 'blocked' THEN 'blocked'
                WHEN json_extract(metadata, '$.work_state') = 'deferred' THEN 'deferred'
                WHEN status = 'open' AND record_type IN ('audit', 'verification') THEN 'actionable'
                WHEN status = 'open' AND record_type = 'knowledge' AND title LIKE '[TO:%' THEN 'actionable'
                ELSE 'nonactionable'
            END
        """)
    execute_sql_script(conn, """
        CREATE INDEX IF NOT EXISTS idx_knowledge_status ON knowledge(status);
        CREATE INDEX IF NOT EXISTS idx_knowledge_bug_id ON knowledge(bug_id);
        CREATE INDEX IF NOT EXISTS idx_knowledge_module ON knowledge(module);
        CREATE INDEX IF NOT EXISTS idx_knowledge_session_label ON knowledge(session_label);
        CREATE INDEX IF NOT EXISTS idx_knowledge_embedding_ready
            ON knowledge(id) WHERE embedding IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_checkpoints_pinned
            ON checkpoints(pinned) WHERE pinned = 1;
        CREATE INDEX IF NOT EXISTS idx_memory_records_action_state
            ON memory_records(action_state, updated_at DESC);
    """)
    # v8: materialize native chunks during migration even when the companion
    # is unavailable; backfill can then report their pending coverage.
    if table_exists(conn, "memory_record_embeddings"):
        _refresh_all_memory_record_embedding_chunks(conn)

    existing_version = conn.execute(
        "SELECT value FROM database_meta WHERE key = 'schema_version'"
    ).fetchone()
    if existing_version is None or existing_version[0] == "1":
        _rebuild_fts(conn)
    if existing_version is not None and int(existing_version[0]) < 5:
        _migrate_v5_fts(conn)
    if existing_version is not None and int(existing_version[0]) < 9:
        _migrate_v9_fts(conn)
    timestamp = now_utc()
    conn.execute(
        """INSERT INTO database_meta(key, value, updated_at) VALUES('schema_version', ?, ?)
           ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
           WHERE database_meta.value != excluded.value""",
        (SCHEMA_VERSION, timestamp),
    )


def _rebuild_fts(conn: sqlite3.Connection) -> None:
    return _rebuild_fts_impl(conn, execute_sql_script_fn=execute_sql_script)


def _migrate_v5_fts(conn: sqlite3.Connection) -> None:
    return _migrate_v5_fts_impl(conn, execute_sql_script_fn=execute_sql_script)


def _migrate_v9_fts(conn: sqlite3.Connection) -> None:
    return _migrate_v9_fts_impl(conn, execute_sql_script_fn=execute_sql_script)


def display_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


GIT_HOOK_SOURCES = {"pre-commit": HERE / "hooks" / "pre-commit"}


def _git_hooks_dir() -> Path | None:
    """Resolve the live hooks directory via git so worktrees also work."""
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--git-path", "hooks"],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def hook_status() -> dict[str, str]:
    """Compare each git-tracked hook source against the live .git/hooks copy.

    The live copy is untracked by nature, so a fresh clone silently loses the
    advisory doc-sync unless something surfaces the gap — this feeds both
    `doctor` and `bootstrap`. Statuses: installed | differs | missing |
    tracked_copy_missing | not_a_git_repo.
    """
    hooks_dir = _git_hooks_dir()
    statuses: dict[str, str] = {}
    for name, source in GIT_HOOK_SOURCES.items():
        if not source.is_file():
            statuses[name] = "tracked_copy_missing"
        elif hooks_dir is None:
            statuses[name] = "not_a_git_repo"
        elif not (hooks_dir / name).is_file():
            statuses[name] = "missing"
        elif (hooks_dir / name).read_bytes() != source.read_bytes():
            statuses[name] = "differs"
        else:
            statuses[name] = "installed"
    return statuses


def hooks_ok(statuses: dict[str, str] | None = None) -> bool:
    # Outside a git repo there is nothing to install, so that state passes.
    return all(
        status in ("installed", "not_a_git_repo")
        for status in (statuses if statuses is not None else hook_status()).values()
    )


def install_hooks() -> dict[str, str]:
    """Copy every tracked hook source into the live .git/hooks directory."""
    hooks_dir = _git_hooks_dir()
    if hooks_dir is None:
        raise OSError(f"{ROOT} is not a git repository; nothing to install hooks into")
    hooks_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, str] = {}
    for name, source in GIT_HOOK_SOURCES.items():
        if not source.is_file():
            raise OSError(f"tracked hook source is missing: {source}")
        target = hooks_dir / name
        target.write_bytes(source.read_bytes())
        target.chmod(0o755)
        results[name] = "installed"
    return results


def _split_large_section(text: str, start_line: int) -> Iterable[tuple[str, int, int]]:
    return _split_large_section_impl(text, start_line, MAX_CHUNK_CHARS)


def markdown_chunks(text: str, fallback_title: str) -> list[MarkdownChunk]:
    return _markdown_chunks_impl(text, fallback_title, max_chars=MAX_CHUNK_CHARS)


def classify(kind: str, heading: str, content: str) -> str:
    return _classify(kind, heading, content)


def extract_metadata(heading: str, content: str, category: str) -> dict[str, Any]:
    return _extract_metadata(heading, content, category)


def _embed_health_probe(
    timeout: float = EMBED_HEALTH_TIMEOUT_SEC,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Probe health without erasing the distinction between policy and outage."""
    try:
        with urlopen(f"{EMBED_BASE_URL}/health", timeout=timeout) as resp:
            return json.loads(resp.read()), None
    except (URLError, OSError, ValueError) as exc:
        cause: Any = exc.reason if isinstance(exc, URLError) else exc
        return None, {
            "type": type(cause).__name__,
            "errno": getattr(cause, "errno", None),
            "message": str(cause),
        }


def _embed_health(timeout: float = EMBED_HEALTH_TIMEOUT_SEC) -> dict[str, Any] | None:
    health, _error = _embed_health_probe(timeout)
    return health


def _embed_ready(health: dict[str, Any] | None) -> bool:
    return bool(health and health.get("status") == "ready" and _embed_identity_matches(health))


def embed_companion_ready() -> bool:
    """True only if the MiniLM companion is already warm. Never spawns —
    callers that must not stall a fast path (a git hook, a plain CLI
    invocation) use this instead of ensure_embed_server(wait=True)."""
    return _embed_ready(_embed_health())


def embed_failure_reason() -> str:
    """Return a short actionable companion-start failure from its local log."""
    try:
        lines = EMBED_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "embedding companion did not become ready; inspect embed_server.log"
    for line in reversed(lines[-250:]):
        stripped = line.strip()
        if stripped.startswith(("RuntimeError:", "OSError:", "ImportError:", "ModuleNotFoundError:")):
            return stripped[:300]
    return "embedding companion did not become ready; inspect embed_server.log"


@contextmanager
def embed_start_lock() -> Iterable[None]:
    """Serialize MiniLM startup across local Codex/Claude processes."""
    lock_fd = open(EMBED_START_LOCK_PATH, "a+")
    try:
        if os.name == "posix":
            import fcntl
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "posix":
            import fcntl
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        lock_fd.close()


def _embed_python_candidates() -> list[Path]:
    """Return possible companion interpreters in preference order.

    The database CLI deliberately remains stdlib-only, so the Python running
    it need not own the optional ML packages. An explicit override is
    authoritative; otherwise prefer the current/active Conda interpreters,
    then the known shared ENDEAVOR environments.
    """
    override = os.getenv(EMBED_PYTHON_ENV)
    if override:
        return [Path(override).expanduser()]

    candidates = [Path(sys.executable)]
    conda_prefix = os.getenv("CONDA_PREFIX")
    if conda_prefix:
        candidates.append(Path(conda_prefix) / ("python.exe" if os.name == "nt" else "bin/python"))
    if os.name == "nt":
        candidates.extend((HERE / ".venv/Scripts/python.exe", ROOT / ".venv/Scripts/python.exe"))
    else:
        candidates.extend((
            HERE / ".venv/bin/python",
            ROOT / ".venv/bin/python",
            Path("/opt/homebrew/anaconda3/envs/endeavor/bin/python"),
            Path("/opt/homebrew/anaconda3/bin/python3"),
            Path("/opt/homebrew/anaconda3/envs/mlx/bin/python"),
            Path.home() / "anaconda3/envs/endeavor/bin/python",
            Path.home() / "miniconda3/envs/endeavor/bin/python",
        ))

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate.expanduser().resolve(strict=False))
        if key not in seen:
            seen.add(key)
            unique.append(Path(key))
    return unique


def _embed_python_has_dependencies(python: Path) -> bool:
    if not python.is_file() or not os.access(python, os.X_OK):
        return False
    modules = repr(EMBED_REQUIRED_MODULES)
    probe = (
        "import importlib.util,sys;"
        f"sys.exit(0 if all(importlib.util.find_spec(m) is not None for m in {modules}) else 1)"
    )
    try:
        completed = subprocess.run(
            [str(python), "-c", probe],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=EMBED_PYTHON_PROBE_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def resolve_embed_python() -> Path:
    """Find a Python that can host the optional embedding companion."""
    candidates = _embed_python_candidates()
    for candidate in candidates:
        if _embed_python_has_dependencies(candidate):
            return candidate
    checked = ", ".join(str(path) for path in candidates)
    override_hint = f" Set {EMBED_PYTHON_ENV} to a compatible Python."
    raise OSError(
        "no Python with fastapi, uvicorn, pydantic, and sentence-transformers was found; "
        f"checked: {checked}.{override_hint} Lexical search remains available."
    )


def embedding_diagnostics() -> dict[str, Any]:
    """Return evidence for agents; never collapse sandbox denial into absence."""
    candidate_reports: list[dict[str, Any]] = []
    selected: Path | None = None
    for candidate in _embed_python_candidates():
        has_dependencies = _embed_python_has_dependencies(candidate)
        candidate_reports.append({
            "path": str(candidate),
            "exists": candidate.is_file(),
            "executable": candidate.is_file() and os.access(candidate, os.X_OK),
            "has_dependencies": has_dependencies,
        })
        if selected is None and has_dependencies:
            selected = candidate

    health, health_error = _embed_health_probe(timeout=1.0)
    if _embed_ready(health):
        diagnosis = "ready"
        next_action = "No action required."
    elif health is not None and not _embed_identity_matches(health):
        diagnosis = "incompatible_service_on_port"
        next_action = "Inspect the process on the embedding port; do not start a second companion."
    elif health is not None:
        diagnosis = "companion_not_ready"
        next_action = "Wait briefly and rerun embed-diagnose; inspect embed_server.log if it persists."
    elif _localhost_permission_denied(health_error):
        diagnosis = "localhost_permission_denied"
        next_action = (
            "Rerun embed-diagnose/embed-status with localhost permission or outside the sandbox; "
            "do not install packages, change interpreters, or restart the companion from this result."
        )
    elif selected is None:
        diagnosis = "companion_dependencies_missing"
        next_action = (
            f"Install the required packages in one companion environment or set {EMBED_PYTHON_ENV}; "
            "lexical search remains available."
        )
    else:
        diagnosis = "companion_unreachable"
        next_action = "Run embed-backfill once; if it fails, inspect its structured diagnostics and log reason."

    return {
        "diagnosis": diagnosis,
        "next_action": next_action,
        "cli_python": sys.executable,
        "cli_python_version": sys.version.split()[0],
        "required_modules": list(EMBED_REQUIRED_MODULES),
        "selected_companion_python": str(selected) if selected else None,
        "companion_candidates": candidate_reports,
        "health": health,
        "health_error": health_error,
        "model": EMBED_MODEL_NAME,
        "model_load_policy": "local_files_only",
        "agent_rule": "Never infer missing packages or a stopped server from companion_warm=false alone.",
    }


def _spawn_embed_server() -> subprocess.Popen[Any]:
    log_fd = open(EMBED_LOG_PATH, "a")
    kwargs: dict[str, Any] = dict(cwd=HERE, stdout=log_fd, stderr=log_fd, stdin=subprocess.DEVNULL)
    if os.name == "posix":
        kwargs["start_new_session"] = True
    try:
        try:
            python = resolve_embed_python()
        except OSError as exc:
            log_fd.write(f"OSError: {exc}\n")
            log_fd.flush()
            raise
        log_fd.write(f"{now_utc()} starting embedding companion with {python}\n")
        log_fd.flush()
        process = subprocess.Popen([str(python), str(HERE / "embed_server.py")], **kwargs)
    finally:
        log_fd.close()
    return process


def ensure_embed_server(wait: bool = True, timeout: float = EMBED_STARTUP_TIMEOUT_SEC) -> bool:
    """Best-effort: use an already-warm companion, or spawn+wait for one.

    Mirrors AGENT_UI_VOX's startF5Companion(): the companion loads only when
    something actually needs it. Safe to call unconditionally — a missing
    sentence-transformers install or spawn failure just returns False, and
    callers fall back to lexical-only search.
    """
    health = _embed_health(timeout=1.0 if wait else EMBED_HEALTH_TIMEOUT_SEC)
    if _embed_ready(health):
        return True
    if health is not None and not _embed_identity_matches(health):
        return False
    if not wait:
        return False
    with embed_start_lock():
        # Another local agent may have started the companion while this caller
        # waited for the advisory lock. Recheck before spawning a second model.
        health = _embed_health(timeout=1.0)
        if _embed_ready(health):
            return True
        if health is not None and not _embed_identity_matches(health):
            return False
        spawned: subprocess.Popen[Any] | None = None
        if health is None:
            try:
                spawned = _spawn_embed_server()
            except OSError as exc:
                print(f"warning: could not start embed_server.py: {exc}", file=sys.stderr)
                return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if spawned is not None and spawned.poll() is not None:
                return False
            health = _embed_health(timeout=1.0)
            if _embed_ready(health):
                return True
            if health is not None and not _embed_identity_matches(health):
                return False
            time.sleep(0.5)
    return False


def set_embed_keep_warm(enabled: bool) -> dict[str, Any]:
    """Explicitly hold or release the already-running MiniLM companion.

    No automatic path calls this: `semantic=auto` remains non-spawning and
    normal embedding work still uses the one-hour idle release.  This is a
    user-directed memory/RAM trade-off, not a global default.
    """
    if not ensure_embed_server(wait=True):
        return {"ready": False, "keep_warm": False, "error": embed_failure_reason()}
    try:
        payload = json.dumps({"keep_warm": enabled}).encode("utf-8")
        request = Request(
            f"{EMBED_BASE_URL}/warm-mode", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urlopen(request, timeout=EMBED_REQUEST_TIMEOUT_SEC) as response:
            body = json.loads(response.read())
    except (OSError, URLError, json.JSONDecodeError) as exc:
        return {"ready": False, "keep_warm": False, "error": f"could not set warm mode: {exc}"}
    return {"ready": body.get("status") == "ready", "keep_warm": bool(body.get("keep_warm"))}


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Returns None (not []) when the companion is unreachable, so callers can
    distinguish "nothing to embed" from "semantic pass unavailable"."""
    if not texts:
        return []
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[start:start + EMBED_BATCH_SIZE]
        try:
            payload = json.dumps({"texts": batch}).encode("utf-8")
            req = Request(f"{EMBED_BASE_URL}/embed", data=payload, headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=EMBED_REQUEST_TIMEOUT_SEC) as resp:
                body = json.loads(resp.read())
            batch_vectors = body["embeddings"]
            if (
                not isinstance(batch_vectors, list)
                or len(batch_vectors) != len(batch)
                or any(
                    not isinstance(vector, list)
                    or len(vector) != EMBED_DIM
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                        for value in vector
                    )
                    for vector in batch_vectors
                )
            ):
                return None
        except (URLError, OSError, TypeError, ValueError, KeyError):
            return None
        vectors.extend(batch_vectors)
    return vectors


def _embed_knowledge_rows_result(conn: sqlite3.Connection, ids: list[int]) -> dict[str, Any]:
    """Embed one batch and retain failure details without touching lexical rows."""
    started_at = now_utc()
    if not ids:
        return {"attempts": 0, "candidates": 0, "embedded": 0, "started_at": started_at, "finished_at": now_utc()}
    rows = conn.execute(
        f"SELECT id, project, content FROM knowledge WHERE id IN ({','.join('?' * len(ids))})", ids
    ).fetchall()

    def failure(reason: str, *, embedded: int = 0) -> dict[str, Any]:
        result = {
            "attempts": 1,
            "candidates": len(rows),
            "embedded": embedded,
            "reason": reason,
            "started_at": started_at,
            "finished_at": now_utc(),
        }
        with conn:
            conn.execute(
                "INSERT INTO activity_log(agent, action, project, detail, created_at) VALUES(?, ?, ?, ?, ?)",
                (
                    "system", "embedding_batch_failed",
                    ",".join(sorted({row["project"] for row in rows})),
                    json_text(result), result["finished_at"],
                ),
            )
        return result

    vectors = embed_texts([row["content"] for row in rows])
    if vectors is None or len(vectors) != len(rows) or any(len(vector) != EMBED_DIM for vector in vectors):
        return failure("embedding_request_failed")
    try:
        packed_vectors = [pack_embedding(vector) for vector in vectors]
    except (OverflowError, TypeError, ValueError, struct.error):
        return failure("invalid_embedding_vector")
    timestamp = now_utc()
    embedded = 0
    with conn:
        for row, packed in zip(rows, packed_vectors):
            cursor = conn.execute(
                "UPDATE knowledge SET embedding = ?, embedding_hash = ?, updated_at = ? "
                "WHERE id = ? AND content = ?",
                (packed, embedding_hash(row["content"]), timestamp, row["id"], row["content"]),
            )
            embedded += cursor.rowcount
    if embedded != len(rows):
        return failure("content_changed_during_embedding", embedded=embedded)
    return {
        "attempts": 1,
        "candidates": len(rows),
        "embedded": embedded,
        "started_at": started_at,
        "finished_at": timestamp,
    }


def _refresh_memory_record_embedding_chunks(conn: sqlite3.Connection, record_id: str) -> None:
    """Replace a native record's derived embedding chunks after its text changes."""
    row = conn.execute("SELECT content FROM memory_records WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        return
    pieces = [piece for piece, _, _ in _split_large_section(row["content"], 1) if piece]
    existing = [item["content"] for item in conn.execute(
        "SELECT content FROM memory_record_embeddings WHERE record_id = ? ORDER BY chunk_index", (record_id,)
    )]
    if existing == pieces:
        return
    conn.execute("DELETE FROM memory_record_embeddings WHERE record_id = ?", (record_id,))
    conn.executemany(
        "INSERT INTO memory_record_embeddings(record_id, chunk_index, content) VALUES(?, ?, ?)",
        [(record_id, index, piece) for index, piece in enumerate(pieces)],
    )


def _refresh_all_memory_record_embedding_chunks(conn: sqlite3.Connection) -> None:
    if not table_exists(conn, "memory_record_embeddings"):
        return
    for row in conn.execute("SELECT id FROM memory_records ORDER BY id"):
        _refresh_memory_record_embedding_chunks(conn, row["id"])


def _embed_memory_record_chunks_result(
    conn: sqlite3.Connection, keys: list[tuple[str, int]],
) -> dict[str, Any]:
    started_at = now_utc()
    if not keys:
        return {"attempts": 0, "candidates": 0, "embedded": 0, "started_at": started_at, "finished_at": now_utc()}
    rows = []
    for record_id, chunk_index in keys:
        row = conn.execute(
            "SELECT e.record_id, e.chunk_index, e.content, r.project FROM memory_record_embeddings e "
            "JOIN memory_records r ON r.id = e.record_id WHERE e.record_id = ? AND e.chunk_index = ?",
            (record_id, chunk_index),
        ).fetchone()
        if row is not None:
            rows.append(row)
    vectors = embed_texts([row["content"] for row in rows])
    if vectors is None or len(vectors) != len(rows) or any(len(vector) != EMBED_DIM for vector in vectors):
        return {"attempts": 1, "candidates": len(rows), "embedded": 0, "reason": "embedding_request_failed", "started_at": started_at, "finished_at": now_utc()}
    try:
        packed = [pack_embedding(vector) for vector in vectors]
    except (OverflowError, TypeError, ValueError, struct.error):
        return {"attempts": 1, "candidates": len(rows), "embedded": 0, "reason": "invalid_embedding_vector", "started_at": started_at, "finished_at": now_utc()}
    timestamp = now_utc()
    embedded = 0
    with conn:
        for row, vector in zip(rows, packed):
            embedded += conn.execute(
                "UPDATE memory_record_embeddings SET embedding = ?, embedding_hash = ?, updated_at = ? "
                "WHERE record_id = ? AND chunk_index = ? AND content = ?",
                (vector, embedding_hash(row["content"]), timestamp, row["record_id"], row["chunk_index"], row["content"]),
            ).rowcount
    result = {"attempts": 1, "candidates": len(rows), "embedded": embedded, "started_at": started_at, "finished_at": timestamp}
    if embedded != len(rows):
        result["reason"] = "content_changed_during_embedding"
    return result


def embed_knowledge_rows(conn: sqlite3.Connection, ids: list[int]) -> int:
    """Compute+store embeddings for the given knowledge row ids, or return zero.

    This compatibility wrapper deliberately keeps the old best-effort integer
    contract; callers that must report failures use the structured helper.
    """
    return int(_embed_knowledge_rows_result(conn, ids)["embedded"])


def embedding_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    # Fetch only blob lengths. Pulling 100k x 384-d float16 vectors into
    # Python just to validate their shape wastes ~75 MB and adds no signal.
    rows = conn.execute(
        "SELECT content, length(embedding) AS embedding_length, embedding_hash FROM knowledge"
    ).fetchall()
    embedded = 0
    invalid_blobs = 0
    stale_hashes = 0
    for row in rows:
        blob_length = row["embedding_length"]
        if blob_length is None:
            continue
        if blob_length != EMBED_DIM * 2:
            invalid_blobs += 1
            continue
        if row["embedding_hash"] != embedding_hash(row["content"]):
            stale_hashes += 1
            continue
        embedded += 1
    health = _embed_health()
    native_rows = conn.execute(
        "SELECT content, length(embedding) AS embedding_length, embedding_hash FROM memory_record_embeddings"
    ).fetchall() if table_exists(conn, "memory_record_embeddings") else []
    native_embedded = sum(
        row["embedding_length"] == EMBED_DIM * 2 and row["embedding_hash"] == embedding_hash(row["content"])
        for row in native_rows
    )
    native_invalid = sum(row["embedding_length"] is not None and row["embedding_length"] != EMBED_DIM * 2 for row in native_rows)
    native_stale = sum(row["embedding_length"] == EMBED_DIM * 2 and row["embedding_hash"] != embedding_hash(row["content"]) for row in native_rows)
    return {
        "knowledge_rows": len(rows),
        "embedded": embedded,
        "pending": len(rows) - embedded,
        "invalid_blobs": invalid_blobs + native_invalid,
        "stale_hashes": stale_hashes + native_stale,
        "companion_warm": _embed_ready(health),
        "memory_record_chunks": len(native_rows),
        "memory_record_embedded": native_embedded,
        "memory_record_pending": len(native_rows) - native_embedded,
    }


def stale_embedding_ids(conn: sqlite3.Connection, document_id: int | None = None) -> list[int]:
    sql = "SELECT id, content, length(embedding) AS embedding_length, embedding_hash FROM knowledge"
    params: tuple[Any, ...] = ()
    if document_id is not None:
        sql += " WHERE document_id = ?"
        params = (document_id,)
    rows = conn.execute(sql, params).fetchall()
    return [
        row["id"]
        for row in rows
        if row["embedding_length"] != EMBED_DIM * 2
        or row["embedding_hash"] != embedding_hash(row["content"])
    ]


def backfill_embeddings(conn: sqlite3.Connection, batch_size: int = EMBED_BATCH_SIZE) -> dict[str, Any]:
    """Embed every row missing an embedding or whose stored hash no longer
    matches its content (self-heal, same shape as ENDEAVOR_RAG_MAX's BM25
    mtime self-heal). Spawns/waits for the companion if it isn't warm."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if not ensure_embed_server(wait=True):
        return {
            "status": "companion_unavailable",
            "embedded": 0,
            "reason": embed_failure_reason(),
            "diagnostics": embedding_diagnostics(),
        }
    with conn:
        _refresh_all_memory_record_embedding_chunks(conn)
    stale_ids = stale_embedding_ids(conn)
    # Content-addressed hashes are checked in Python so NULL and malformed
    # values follow the same contract as Markdown-derived knowledge.
    stale_native = [
        (row["record_id"], row["chunk_index"])
        for row in conn.execute(
            "SELECT record_id, chunk_index, content, embedding, embedding_hash FROM memory_record_embeddings"
        )
        if row["embedding"] is None
        or len(row["embedding"]) != EMBED_DIM * 2
        or row["embedding_hash"] != embedding_hash(row["content"])
    ]
    embedded = 0
    attempts = 0
    failures: list[dict[str, Any]] = []
    for start in range(0, len(stale_ids), batch_size):
        batch = _embed_knowledge_rows_result(conn, stale_ids[start:start + batch_size])
        embedded += batch["embedded"]
        attempts += batch["attempts"]
        if "reason" in batch:
            failures.append(batch)
    for start in range(0, len(stale_native), batch_size):
        batch = _embed_memory_record_chunks_result(conn, stale_native[start:start + batch_size])
        embedded += batch["embedded"]
        attempts += batch["attempts"]
        if "reason" in batch:
            failures.append(batch)
    result = {"status": "ok", "candidates": len(stale_ids) + len(stale_native), "embedded": embedded, "attempts": attempts}
    if failures:
        reasons = sorted({failure["reason"] for failure in failures})
        result.update({
            "status": "partial_failure",
            "reason": reasons[0] if len(reasons) == 1 else "multiple_embedding_failures",
            "failure_reasons": reasons,
            "failed_batches": len(failures),
        })
    return result


def _embed_result(conn: sqlite3.Connection, result: dict[str, Any], ids: list[int]) -> None:
    """Attach best-effort embedding outcome to an ingest result."""
    if not ids:
        result["embedded"] = 0
    elif ensure_embed_server(wait=True):
        embedding = _embed_knowledge_rows_result(conn, ids)
        result["embedded"] = embedding["embedded"]
        result["embedding_attempts"] = embedding["attempts"]
        if "reason" in embedding:
            result["embedding_warning"] = embedding["reason"]
    else:
        result["embedded"] = 0
        result["embedding_warning"] = embed_failure_reason()


def ingest_markdown(
    conn: sqlite3.Connection, source: Path, project: str, kind: str, *, embed: bool = True
) -> dict[str, Any]:
    source = source.expanduser().resolve()
    raw = source.read_bytes()
    text = raw.decode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    source_name = display_path(source)
    # Fence-aware: a scanner that took the first `#` line unconditionally
    # titled a document after a shell comment inside its first code block.
    title = _document_title(text, source.stem)
    existing = conn.execute(
        "SELECT id, content_hash, index_version, project, kind FROM documents WHERE source_path = ?", (source_name,)
    ).fetchone()
    if (
        existing
        and existing["content_hash"] == digest
        and existing["index_version"] == INDEX_VERSION
        and existing["project"] == project
        and existing["kind"] == kind
    ):
        count = conn.execute("SELECT COUNT(*) FROM knowledge WHERE document_id = ?", (existing["id"],)).fetchone()[0]
        result = {"source": source_name, "status": "unchanged", "entries": count}
        if embed:
            _embed_result(conn, result, stale_embedding_ids(conn, existing["id"]))
        return result

    timestamp = now_utc()
    with conn:
        conn.execute(
            """INSERT INTO documents(source_path, title, kind, project, content_hash, index_version, source_mtime, imported_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_path) DO UPDATE SET
                   title=excluded.title, kind=excluded.kind, project=excluded.project,
                   content_hash=excluded.content_hash, index_version=excluded.index_version, source_mtime=excluded.source_mtime,
                   imported_at=excluded.imported_at""",
            (source_name, title, kind, project, digest, INDEX_VERSION, source.stat().st_mtime, timestamp),
        )
        document_id = conn.execute("SELECT id FROM documents WHERE source_path = ?", (source_name,)).fetchone()[0]
        conn.execute("DELETE FROM knowledge WHERE document_id = ?", (document_id,))
        chunks = markdown_chunks(text, title)
        for chunk in chunks:
            category = classify(kind, chunk.heading, chunk.content)
            metadata = extract_metadata(chunk.heading, chunk.content, category)
            tags = [kind, project, category, metadata["status"], metadata["module"], metadata["session"], *metadata["bug_ids"]]
            tags += [part.strip().lower() for part in chunk.heading.split(" > ")[-2:]]
            tags = [item for item in tags if item]
            conn.execute(
                """INSERT INTO knowledge(
                       document_id, project, category, title, content, tags, metadata, status, bug_id,
                       module, session_label, parent_heading, source_path, source_heading,
                       source_line_start, source_line_end, created_at, updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    document_id, project, category, chunk.title, chunk.content,
                    json_text(sorted(set(tags))), json_text(metadata), metadata["status"],
                    ",".join(metadata["bug_ids"]), metadata["module"], metadata["session"],
                    metadata["parent_heading"], source_name, chunk.heading, chunk.line_start,
                    chunk.line_end, timestamp, timestamp,
                ),
            )
        conn.execute(
            "INSERT INTO activity_log(agent, action, project, detail, created_at) VALUES(?, ?, ?, ?, ?)",
            ("system", "ingest", project, json_text({"source": source_name, "entries": len(chunks)}), timestamp),
        )
    result = {"source": source_name, "status": "imported", "entries": len(chunks)}
    # Lexical ingest above is already committed and must never depend on this:
    # embedding is best-effort. Ingest is a natural "first use" moment (run
    # far less often than `query`), so it warms the companion by default.
    if embed:
        ids = [row["id"] for row in conn.execute(
            "SELECT id FROM knowledge WHERE document_id = ?", (document_id,)
        ).fetchall()]
        _embed_result(conn, result, ids)
    return result


def seed(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [ingest_markdown(conn, source, project, kind) for source, project, kind in SEED_SOURCES]


def prune_documents(conn: sqlite3.Connection, allowed_source_paths: set[str]) -> int:
    """Delete indexed documents not present in an explicitly reviewed source set.

    Callers must build ``allowed_source_paths`` from a trusted manifest, and
    normalized the SAME way ingest stores ``source_path`` (via display_path /
    sync_tracked._database_source_path) — a set built from raw manifest keys
    that don't match the stored format would prune everything. This is
    intentionally separate from normal ingest because deleting an ad-hoc
    document must always be an explicit operation.

    Removes rows in a SINGLE atomic ``DELETE ... WHERE source_path NOT IN
    (allowed)`` statement — no separate select-then-delete step (Python
    sqlite3's default deferred mode runs a SELECT OUTSIDE the write
    transaction, so a select-then-delete pattern is not atomic). Deleting by
    the ``source_path`` predicate rather than by numeric id also avoids a
    reusable-rowid hazard: documents.id is an INTEGER PRIMARY KEY (not
    AUTOINCREMENT), so a captured-id delete could remove a replacement that
    reused a freed rowid. The ON DELETE CASCADE + knowledge_ad trigger clean
    the knowledge rows and all three FTS shadow tables.

    ``allowed_source_paths`` is a curated manifest of tracked docs (currently
    ~10^2), well within SQLite's bound-parameter limit; this is not sized for
    tens of thousands of allowed paths.
    """
    timestamp = now_utc()
    allowed = list(allowed_source_paths)
    with conn:
        if allowed:
            placeholders = ",".join("?" * len(allowed))
            cursor = conn.execute(
                f"DELETE FROM documents WHERE source_path NOT IN ({placeholders})", allowed
            )
        else:
            cursor = conn.execute("DELETE FROM documents")
        removed = cursor.rowcount
        if removed:
            conn.execute(
                "INSERT INTO activity_log(agent, action, project, detail, created_at) VALUES(?, ?, ?, ?, ?)",
                ("system", "prune", "ENDMEMEX", json_text({"documents": removed}), timestamp),
            )
    return removed


def delete_documents_by_source_paths(conn: sqlite3.Connection, source_paths: Iterable[str]) -> int:
    """Delete an EXPLICIT, caller-safeguarded list of indexed documents by
    exact ``source_path`` (cascade + knowledge_ad trigger remove their
    knowledge chunks and every FTS shadow table).

    Distinct from ``prune_documents``' manifest-diff: this takes a specific
    allowlist of paths the caller has already decided to remove — used for
    rename cleanup (delete only the OLD name of a git-detected rename), never
    for deleted files (whose indexed snapshot is retained on purpose). Deletes
    by source_path for the same reusable-rowid safety reason as prune_documents.
    """
    wanted = sorted({path for path in source_paths if path})
    if not wanted:
        return 0
    timestamp = now_utc()
    with conn:
        cursor = conn.execute(
            f"DELETE FROM documents WHERE source_path IN ({','.join('?' * len(wanted))})", wanted
        )
        removed = cursor.rowcount
        if removed:
            conn.execute(
                "INSERT INTO activity_log(agent, action, project, detail, created_at) VALUES(?, ?, ?, ?, ?)",
                ("system", "prune_rename", "ENDMEMEX", json_text({"documents": removed}), timestamp),
            )
    return removed


def _typo_corrected_terms(
    conn: sqlite3.Connection, terms: list[str], vocab_table: str,
) -> tuple[list[str], list[str]]:
    return _typo_corrected_terms_impl(conn, terms, vocab_table, table_exists_fn=table_exists)


def search(
    conn: sqlite3.Connection,
    query: str,
    project: str | None,
    category: str | None,
    limit: int,
    *,
    status: str | None = None,
    module: str | None = None,
    bug_id: str | None = None,
    session_label: str | None = None,
    semantic: str = "auto",
) -> list[dict[str, Any]]:
    """Multi-pass lexical retrieval (RRF + deterministic reranking) with an
    optional MiniLM semantic pass fused in by the same RRF scheme.

    `semantic`: "auto" uses the embedding companion only if it is already
    warm (a ~0.3s health check, never spawns — a plain query must not eat an
    ~11s cold-start stall); "on" spawns/waits for it; "off" never attempts it.
    """
    intent = detect_intent(query)
    terms = query_terms(query)

    def scoped_filters(alias: str) -> tuple[list[str], list[Any]]:
        """Return every caller-visible filter for either retrieval path."""
        filters: list[str] = []
        params: list[Any] = []
        if project:
            filters.append(f"{alias}.project = ?")
            params.append(project)
        if category:
            filters.append(f"{alias}.category = ?")
            params.append(category)
        if status:
            filters.append(f"{alias}.status = ?")
            params.append(status)
        if module:
            # `module` is the display primary module; `metadata.modules`
            # preserves every module mentioned by a source chunk.
            filters.append(
                f"({alias}.module LIKE ? ESCAPE '\\' OR EXISTS ("
                f"SELECT 1 FROM json_each({alias}.metadata, '$.modules') "
                f"WHERE CAST(value AS TEXT) LIKE ? ESCAPE '\\'))"
            )
            escaped_module = module.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.extend((f"%{escaped_module}%", f"%{escaped_module}%"))
        if bug_id:
            filters.append(
                f"(upper({alias}.bug_id) = ? OR EXISTS ("
                f"SELECT 1 FROM json_each({alias}.metadata, '$.bug_ids') "
                f"WHERE upper(CAST(value AS TEXT)) = ?))"
            )
            normalized_bug_id = bug_id.upper()
            params.extend((normalized_bug_id, normalized_bug_id))
        if session_label:
            filters.append(f"{alias}.session_label = ?")
            params.append(session_label)
        return filters, params

    def run_fts(table: str, expression: str, strategy: str, weight: float) -> list[tuple[sqlite3.Row, str, float]]:
        if not expression:
            return []
        filters = [f"{table} MATCH ?"]
        params: list[Any] = [expression]
        scope, scope_params = scoped_filters("k")
        filters.extend(scope)
        params.extend(scope_params)
        params.append(RESULT_CANDIDATES)
        rows = conn.execute(
            f"""SELECT k.*, bm25({table}, 4.0, 1.0, 0.8) AS bm25_rank,
                       snippet({table}, 1, '[', ']', ' … ', 36) AS excerpt
                FROM {table} JOIN knowledge k ON k.id = {table}.rowid
                WHERE {' AND '.join(filters)} ORDER BY bm25_rank LIMIT ?""",
            params,
        ).fetchall()
        return [(row, strategy, weight / (60 + rank)) for rank, row in enumerate(rows, start=1)]

    def run_semantic(
        candidate_ids: Iterable[int], weight: float = 1.0,
    ) -> list[tuple[sqlite3.Row, str, float]]:
        """Run exact semantic retrieval while it is cheap, then stay bounded.

        SQLite has no built-in ANN index. Exact scanning preserves zero-term
        paraphrase recall on today's small index. Above the safety threshold,
        semantic search reranks lexical candidates so a 100k-row database
        never unpacks every vector or allocates hundreds of MB per query.
        """
        if not query.strip():
            return []
        ids = list(dict.fromkeys(candidate_ids))
        scope, scope_params = scoped_filters("k")
        filters = ["length(k.embedding) = ?"] + scope
        semantic_params: list[Any] = [EMBED_DIM * 2, *scope_params]
        embedded_count = conn.execute(
            f"SELECT COUNT(*) FROM knowledge k WHERE {' AND '.join(filters)}",
            semantic_params,
        ).fetchone()[0]
        if embedded_count == 0:
            return []
        exact_scan = embedded_count <= SEMANTIC_FULL_SCAN_LIMIT
        if not exact_scan and not ids:
            ann_state = ann_helper(conn, "status")
            if not ann_state.get("available") or not ann_state.get("fresh"):
                return []
        query_vectors = embed_texts([query])
        if not query_vectors:
            return []
        qvec = query_vectors[0]
        if not exact_scan:
            # ANN searches the entire sidecar and returns the high-scoring
            # neighborhood, restoring zero-keyword-overlap recall. A missing
            # or stale optional sidecar simply contributes no IDs; lexical
            # candidates remain the bounded fallback.
            ann_ids = ann_candidate_ids(conn, qvec, "knowledge")
            ids = list(dict.fromkeys([*ann_ids, *ids]))[:ANN_CANDIDATE_LIMIT]
            if not ids:
                return []
        rows: list[sqlite3.Row] = []
        if exact_scan:
            rows = conn.execute(
                f"SELECT k.*, substr(k.content, 1, 500) AS excerpt FROM knowledge k "
                f"WHERE {' AND '.join(filters)}",
                semantic_params,
            ).fetchall()
        else:
            for batch in _batched(ids):
                placeholders = ",".join("?" for _ in batch)
                rows.extend(conn.execute(
                    f"SELECT k.*, substr(k.content, 1, 500) AS excerpt FROM knowledge k "
                    f"WHERE k.id IN ({placeholders}) AND {' AND '.join(filters)}",
                    [*batch, *semantic_params],
                ).fetchall())
        scored = []
        for row in rows:
            if row["embedding_hash"] != embedding_hash(row["content"]):
                continue
            try:
                vector = unpack_embedding(row["embedding"])
            except ValueError:
                continue
            # Vectors are stored pre-normalized, so a dot product is cosine
            # similarity; ranked (not raw-score) fusion keeps this on the
            # same reciprocal-rank scale as every FTS pass above.
            similarity = sum(a * b for a, b in zip(qvec, vector))
            scored.append((similarity, row))
        scored.sort(key=lambda pair: -pair[0])
        return [
            (row, "semantic", weight / (60 + rank))
            for rank, (_, row) in enumerate(scored[:RESULT_CANDIDATES], start=1)
        ]

    strict = fts_expression(query, "AND")
    broad = fts_expression(query, "OR")
    candidates: dict[int, dict[str, Any]] = {}
    passes = [
        ("knowledge_fts", strict, "all_terms", 1.6),
        ("knowledge_fts", broad, "any_terms", 0.8),
        ("knowledge_fts_porter", strict, "porter", 1.2),
    ]
    raw_compact = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", query).casefold().strip())

    for table, expression, strategy, weight in passes:
        for row, name, score in run_fts(table, expression, strategy, weight):
            item = candidates.setdefault(row["id"], {"row": row, "score": 0.0, "reasons": []})
            item["score"] += score
            item["reasons"].append(name)

    # Correct spelling only after the exact AND query failed.  The correction
    # is drawn from the FTS vocabulary and must be a unique nearby ASCII word;
    # IDs, paths, numbers, Thai, and ambiguous terms remain literal.
    strict_found = any("all_terms" in item["reasons"] for item in candidates.values())
    if strict and not strict_found:
        corrected_terms, typo_reasons = _typo_corrected_terms(conn, terms, "knowledge_fts_terms")
        if typo_reasons:
            corrected = fts_expression(" ".join(corrected_terms), "AND")
            for row, name, score in run_fts("knowledge_fts", corrected, "typo", 1.15):
                item = candidates.setdefault(row["id"], {"row": row, "score": 0.0, "reasons": []})
                item["score"] += score
                item["reasons"].extend((name, *typo_reasons))

    # Trigram is large (~10x unicode61 in the production DB), so it is a
    # recall rescue only.  It is never queried for one/two-character input,
    # and it cannot displace a healthy strict lexical result on its own.
    trigram_text = raw_compact.replace('"', "")
    if len(candidates) < min(limit, 3) and len(trigram_text.replace(" ", "")) >= 3:
        for row, name, score in run_fts(
            "knowledge_fts_trigram", f'"{trigram_text}"', "trigram", 0.55,
        ):
            item = candidates.setdefault(row["id"], {"row": row, "score": 0.0, "reasons": []})
            item["score"] += score
            item["reasons"].append(name)

    semantic_ready = semantic == "ready" or (
        semantic != "off" and ensure_embed_server(wait=(semantic == "on"))
    )
    if semantic_ready:
        for row, name, score in run_semantic(candidates):
            item = candidates.setdefault(row["id"], {"row": row, "score": 0.0, "reasons": []})
            item["score"] += score
            item["reasons"].append(name)

    # Metadata-only rescue for bug/module/session identifiers that may not be
    # tokenized well by FTS (especially punctuation-heavy IDs).
    if bug_id or module or session_label:
        metadata_filters, metadata_params = scoped_filters("k")
        rows = conn.execute(
            f"SELECT k.*, 0.0 AS bm25_rank, substr(k.content, 1, 500) AS excerpt "
            f"FROM knowledge k WHERE {' AND '.join(metadata_filters)} ORDER BY k.id LIMIT ?",
            metadata_params + [RESULT_CANDIDATES],
        ).fetchall()
        for rank, row in enumerate(rows, start=1):
            item = candidates.setdefault(row["id"], {"row": row, "score": 0.0, "reasons": []})
            item["score"] += 1.5 / (60 + rank)
            item["reasons"].append("metadata")

    normalized_query = raw_compact
    intent_categories = {
        "bug_fix": {"debugging"}, "training_method": {"agent_training"},
        "testing": {"testing"}, "checkpoint_resume": {"session_history"},
    }
    ranked: list[dict[str, Any]] = []
    for item in candidates.values():
        row = item["row"]
        score = item["score"]
        title_lower = row["title"].lower()
        if normalized_query and normalized_query in title_lower:
            score += 0.45
            item["reasons"].append("exact_title")
        if terms and all(term in f"{title_lower} {row['content'].lower()}" for term in terms):
            score += 0.25
            item["reasons"].append("all_terms_present")
        if intent_categories.get(intent) and row["category"] in intent_categories[intent]:
            # Intent is deliberately stronger for explicit training concepts:
            # reusable guidance should outrank a historical incident that
            # happens to use the same wording.
            score += 0.50 if intent == "training_method" else 0.22
            item["reasons"].append(f"intent:{intent}")
        if row["status"] == "resolved":
            score += 0.12
            item["reasons"].append("resolved")
        if row["status"] == "accepted":
            score -= 0.05
        if row["bug_id"] and any(term.upper() in row["bug_id"] for term in terms):
            score += 0.35
            item["reasons"].append("bug_id")
        ranked.append({**item, "score": score})

    ranked.sort(key=lambda item: (-item["score"], item["row"]["id"]))
    selected: list[dict[str, Any]] = []
    parent_counts: dict[tuple[int, str], int] = {}
    seen_content: set[tuple[str, str]] = set()
    for item in ranked:
        row = item["row"]
        content_key = (row["title"], row["content"])
        if content_key in seen_content:
            continue
        parent = (row["document_id"], row["parent_heading"] or row["source_heading"])
        if parent_counts.get(parent, 0) >= 2:
            continue
        parent_counts[parent] = parent_counts.get(parent, 0) + 1
        seen_content.add(content_key)
        selected.append(item)
        if len(selected) >= limit:
            break

    return [{
        "id": item["row"]["id"], "project": item["row"]["project"], "category": item["row"]["category"],
        "title": item["row"]["title"], "excerpt": item["row"]["excerpt"], "source_path": item["row"]["source_path"],
        "source_heading": item["row"]["source_heading"], "line_start": item["row"]["source_line_start"],
        "line_end": item["row"]["source_line_end"], "status": item["row"]["status"],
        "bug_id": item["row"]["bug_id"], "module": item["row"]["module"], "session": item["row"]["session_label"],
        "rank": round(float(item["score"]), 5), "match_reasons": sorted(set(item["reasons"])),
    } for item in selected]


def evaluate_queries(
    conn: sqlite3.Connection, path: Path, limit: int = 5, semantic: str = "auto",
    pipeline: str = "unified",
) -> dict[str, Any]:
    """Defaults to "auto" (never spawns) so plain library/test callers stay
    fast and hermetic. The `evaluate` CLI command passes --semantic on by
    default instead, so the benchmark deterministically exercises the full
    pipeline rather than whatever idle state happened to be warm."""
    if pipeline not in {"markdown", "unified"}:
        raise ValueError("evaluation pipeline must be markdown or unified")
    cases = json.loads(path.read_text(encoding="utf-8"))
    semantic_available: bool | None = None
    effective_semantic = semantic
    if semantic == "on":
        # Probe/warm once for the whole suite. Retrying a failed 30-second
        # cold start for every case made an unavailable companion turn a
        # 14-query evaluation into a multi-minute stall.
        semantic_available = ensure_embed_server(wait=True)
        effective_semantic = "ready" if semantic_available else "off"
    details = []
    reciprocal_ranks = []
    hits = 0
    for case in cases:
        search_fn = search_all if pipeline == "unified" else search
        results = search_fn(
            conn, case["query"], case.get("project"), case.get("category"), limit,
            semantic=effective_semantic,
        )
        expected = {key: value for key, value in case.items() if key.startswith("expected_")}
        found_rank = None
        for rank, result in enumerate(results, start=1):
            if all(result.get(key.removeprefix("expected_")) == value for key, value in expected.items()):
                found_rank = rank
                break
        if found_rank is not None:
            hits += 1
            reciprocal_ranks.append(1 / found_rank)
        else:
            reciprocal_ranks.append(0.0)
        source_mix: dict[str, int] = {}
        for result in results:
            source = result.get("source_kind", "markdown")
            source_mix[source] = source_mix.get(source, 0) + 1
        details.append({
            "query": case["query"], "expected": expected, "rank": found_rank,
            "top_ids": [r["id"] for r in results], "source_mix": source_mix,
        })
    total = len(cases)
    return {
        "queries": total,
        "pipeline": pipeline,
        "semantic_requested": semantic,
        "semantic_available": semantic_available,
        f"recall_at_{limit}": round(hits / total, 4) if total else 0.0,
        "mrr": round(sum(reciprocal_ranks) / total, 4) if total else 0.0,
        "details": details,
    }


def ann_helper(
    conn: sqlite3.Connection, command: str, *, source: str | None = None,
    vector: list[float] | None = None, limit: int = ANN_CANDIDATE_LIMIT,
) -> dict[str, Any]:
    """Call the optional ANN process without importing ML packages here."""
    database = conn.execute("PRAGMA database_list").fetchone()[2]
    args = [sys.executable, str(ANN_HELPER_PATH), "--db", database, command]
    if command == "query":
        args += ["--source", source or "knowledge", "--limit", str(max(1, min(limit, 1000)))]

    def unavailable(reason: str) -> dict[str, Any]:
        # Query callers deliberately fall back to lexical retrieval. Status
        # callers, however, need an explicit unknown state so readiness never
        # turns a failed probe into an unsupported dependency diagnosis.
        if command != "status":
            return {}
        return {"status": "unavailable", "available": None, "fresh": False, "error": reason}

    try:
        result = subprocess.run(
            args, cwd=ROOT, input=json_text(vector) if vector is not None else None,
            text=True, capture_output=True,
            timeout=ANN_BUILD_TIMEOUT_SEC if command == "build" else ANN_QUERY_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return unavailable("ANN helper timed out")
    except OSError:
        return unavailable("ANN helper could not be started")
    if not result.stdout.strip():
        return unavailable("ANN helper returned no output")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return unavailable("ANN helper returned invalid JSON")
    if not isinstance(payload, dict):
        return unavailable("ANN helper returned a non-object JSON payload")
    if command == "status" and result.returncode and not payload.get("status"):
        return unavailable(f"ANN helper exited with status {result.returncode}")
    return payload


def ann_candidate_ids(
    conn: sqlite3.Connection, vector: list[float], source: str,
    limit: int = ANN_CANDIDATE_LIMIT,
) -> list[Any]:
    if not isinstance(vector, list) or len(vector) != EMBED_DIM or not all(
        isinstance(value, (int, float)) for value in vector
    ):
        return []
    payload = ann_helper(conn, "query", source=source, vector=vector, limit=limit)
    ids = payload.get("ids")
    return ids if isinstance(ids, list) else []


def record_feedback(
    conn: sqlite3.Connection, agent: str, query_text: str, selected_ids: list[Any], useful: bool | None, note: str,
) -> int:
    with conn:
        cursor = conn.execute(
            """INSERT INTO query_feedback(agent, query_text, selected_ids, useful, note, created_at)
               VALUES(?, ?, ?, ?, ?, ?)""",
            (agent, query_text, json_text(selected_ids), None if useful is None else int(useful), note, now_utc()),
        )
    return cursor.lastrowid


def publish_event(
    conn: sqlite3.Connection, event_type: str, project: str, subject_id: str,
    payload: dict[str, Any], dedupe_key: str, agent: str,
) -> dict[str, Any]:
    """Publish once by dedupe_key and return the existing/new durable event."""
    for label, value in (
        ("event_type", event_type), ("project", project),
        ("subject_id", subject_id), ("dedupe_key", dedupe_key),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must not be empty")
    if not isinstance(payload, dict):
        raise ValueError("event payload must be a JSON object")
    timestamp = now_utc()
    with conn:
        conn.execute(
            """INSERT INTO durable_events(
                   event_type, project, subject_id, payload, dedupe_key, created_at
               ) VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(dedupe_key) DO NOTHING""",
            (event_type, project, subject_id, json_text(payload), dedupe_key, timestamp),
        )
        row = conn.execute(
            "SELECT * FROM durable_events WHERE dedupe_key = ?", (dedupe_key,),
        ).fetchone()
        conn.execute(
            "INSERT INTO activity_log(agent, action, project, detail, created_at) VALUES(?, 'event_publish', ?, ?, ?)",
            (agent, project, json_text({"event_id": row["id"], "dedupe_key": dedupe_key}), timestamp),
        )
    result = row_dict(row)
    result["payload"] = json.loads(result["payload"])
    return result


def poll_events(
    conn: sqlite3.Connection, after_id: int = 0, project: str | None = None,
    limit: int = 50, include_acked: bool = False,
) -> list[dict[str, Any]]:
    clauses = ["id > ?"]
    params: list[Any] = [max(0, after_id)]
    if project:
        clauses.append("project = ?")
        params.append(project)
    if not include_acked:
        clauses.append("acknowledged_at IS NULL")
    params.append(max(1, min(limit, 500)))
    rows = conn.execute(
        f"SELECT * FROM durable_events WHERE {' AND '.join(clauses)} ORDER BY id LIMIT ?",
        params,
    ).fetchall()
    results = []
    for row in rows:
        item = row_dict(row)
        item["payload"] = json.loads(item["payload"])
        results.append(item)
    return results


def acknowledge_event(conn: sqlite3.Connection, event_id: int, agent: str) -> dict[str, Any]:
    timestamp = now_utc()
    with conn:
        cursor = conn.execute(
            """UPDATE durable_events
               SET acknowledged_at = COALESCE(acknowledged_at, ?),
                   acknowledged_by = COALESCE(acknowledged_by, ?)
               WHERE id = ?""",
            (timestamp, agent, event_id),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"no durable event with id {event_id}")
        row = conn.execute("SELECT * FROM durable_events WHERE id = ?", (event_id,)).fetchone()
    result = row_dict(row)
    result["payload"] = json.loads(result["payload"])
    return result


def create_memory_record(
    conn: sqlite3.Connection,
    record_id: str | None,
    project: str,
    record_type: str,
    title: str,
    content: str,
    status: str,
    agent: str,
    metadata: dict[str, Any] | None = None,
    links: Iterable[tuple[str, str, str]] = (),
    action_state: str | None = None,
) -> str:
    record_id = normalize_memory_id(record_id or f"MEM-{uuid.uuid4().hex[:12].upper()}")
    if record_type not in MEMORY_RECORD_TYPES:
        raise ValueError(f"unsupported record type: {record_type}")
    if status not in MEMORY_RECORD_STATUSES:
        raise ValueError(f"unsupported record status: {status}")
    if action_state is None:
        action_state = (
            "actionable" if status == "open" and record_type in ("audit", "verification")
            else "actionable" if status == "open" and record_type == "knowledge" and title.startswith("[TO:")
            else "nonactionable"
        )
    if action_state not in MEMORY_ACTION_STATES:
        raise ValueError(f"unsupported action state: {action_state}")
    if not project.strip() or not title.strip() or not content.strip():
        raise ValueError("project, title, and content must not be empty")
    timestamp = now_utc()
    with conn:
        conn.execute(
            """INSERT INTO memory_records(
                   id, fts_rowid, project, record_type, title, content, status, action_state,
                   metadata, created_by, created_at, updated_at
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record_id, uuid.uuid4().int >> 65, project.strip(), record_type, title.strip(), content.strip(), status,
                action_state, json_text(metadata or {}), agent, timestamp, timestamp,
            ),
        )
        _refresh_memory_record_embedding_chunks(conn, record_id)
        for relation, target_id, note in links:
            _insert_memory_relation(conn, record_id, relation, target_id, note, agent)
        conn.execute(
            "INSERT INTO activity_log(agent, action, project, detail, created_at) VALUES(?, ?, ?, ?, ?)",
            (agent, "memory_record_add", project.strip(), json_text({"record_id": record_id}), timestamp),
        )
    return record_id


def update_memory_record(
    conn: sqlite3.Connection, record_id: str, agent: str, **changes: Any
) -> dict[str, Any]:
    record_id = normalize_memory_id(record_id)
    allowed = {"project", "record_type", "title", "content", "status", "action_state", "metadata"}
    updates = {key: value for key, value in changes.items() if key in allowed and value is not None}
    if not updates:
        raise ValueError("record update requires at least one changed field")
    if updates.get("record_type") not in (None, *MEMORY_RECORD_TYPES):
        raise ValueError(f"unsupported record type: {updates['record_type']}")
    if updates.get("status") not in (None, *MEMORY_RECORD_STATUSES):
        raise ValueError(f"unsupported record status: {updates['status']}")
    if updates.get("action_state") not in (None, *MEMORY_ACTION_STATES):
        raise ValueError(f"unsupported action state: {updates['action_state']}")
    if updates.get("status") in ("resolved", "accepted") and "action_state" not in updates:
        updates["action_state"] = "done"
    if "metadata" in updates:
        updates["metadata"] = json_text(updates["metadata"])
    if any(key in updates and isinstance(updates[key], str) and not updates[key].strip()
           for key in ("project", "title", "content")):
        raise ValueError("project, title, and content must not be empty")
    updates["updated_at"] = now_utc()
    assignments = ", ".join(f"{key} = ?" for key in updates)
    with conn:
        cursor = conn.execute(
            f"UPDATE memory_records SET {assignments} WHERE id = ?", [*updates.values(), record_id]
        )
        if cursor.rowcount != 1:
            raise ValueError(f"memory record not found: {record_id}")
        if "title" in updates or "content" in updates:
            _refresh_memory_record_embedding_chunks(conn, record_id)
        project = conn.execute("SELECT project FROM memory_records WHERE id = ?", (record_id,)).fetchone()[0]
        conn.execute(
            "INSERT INTO activity_log(agent, action, project, detail, created_at) VALUES(?, ?, ?, ?, ?)",
            (agent, "memory_record_update", project, json_text({"record_id": record_id}), now_utc()),
        )
    return memory_record_context(conn, record_id, depth=0)["record"]


def semantic_memory_records(
    conn: sqlite3.Connection, query: str, project: str | None, record_type: str | None, limit: int,
) -> list[dict[str, Any]]:
    """Rank current native truth by the best valid lifecycle-member chunk.

    Historical wording remains useful evidence after a record is resolved or
    superseded, so its semantic score is transferred to the current component
    head instead of being discarded.  Exact scanning is retained while the
    native embedding table is small; above the safety threshold only lexical
    candidates are reranked, matching the bounded Markdown retrieval path.
    """
    if not query.strip() or not table_exists(conn, "memory_record_embeddings"):
        return []
    filters = ["length(e.embedding) = ?"]
    params: list[Any] = [EMBED_DIM * 2]
    if project:
        filters.append("r.project = ?")
        params.append(project)
    if record_type:
        filters.append("r.record_type = ?")
        params.append(record_type)
    count = conn.execute(
        f"SELECT COUNT(*) FROM memory_records r JOIN memory_record_embeddings e ON e.record_id = r.id "
        f"WHERE {' AND '.join(filters)}", params,
    ).fetchone()[0]
    exact_scan = count <= SEMANTIC_FULL_SCAN_LIMIT
    candidate_ids: list[str] = []
    if not exact_scan:
        candidate_ids = [
            item["id"] for item in search_memory_records(
                conn, query, project, record_type,
                min(50, max(RESULT_CANDIDATES, limit * 5)), current_only=False,
            )
        ]
        if not candidate_ids:
            ann_state = ann_helper(conn, "status")
            if not ann_state.get("available") or not ann_state.get("fresh"):
                return []
    vectors = embed_texts([query])
    if not vectors:
        return []
    qvec = vectors[0]
    if not exact_scan:
        ann_ids = [
            item for item in ann_candidate_ids(conn, qvec, "native")
            if isinstance(item, str)
        ]
        candidate_ids = list(dict.fromkeys([*ann_ids, *candidate_ids]))[:ANN_CANDIDATE_LIMIT]
        if not candidate_ids:
            return []
    rows: list[sqlite3.Row] = []
    if exact_scan:
        rows = conn.execute(
            f"SELECT r.*, e.content AS chunk_content, e.embedding AS chunk_embedding, e.embedding_hash AS chunk_hash "
            f"FROM memory_records r JOIN memory_record_embeddings e ON e.record_id = r.id "
            f"WHERE {' AND '.join(filters)}",
            params,
        ).fetchall()
    else:
        # Keep the fallback bounded by both record count and chunk count. A
        # single unusually large native record must not turn a 50-record
        # lexical candidate set back into an unbounded vector scan.
        for record_id in candidate_ids:
            rows.extend(conn.execute(
                f"SELECT r.*, e.content AS chunk_content, e.embedding AS chunk_embedding, e.embedding_hash AS chunk_hash "
                f"FROM memory_records r JOIN memory_record_embeddings e ON e.record_id = r.id "
                f"WHERE {' AND '.join(filters)} AND r.id = ? "
                "ORDER BY e.chunk_index LIMIT ?",
                [*params, record_id, SEMANTIC_CHUNKS_PER_RECORD_LIMIT],
            ).fetchall())
    best: dict[str, tuple[float, sqlite3.Row]] = {}
    for row in rows:
        if row["chunk_hash"] != embedding_hash(row["chunk_content"]):
            continue
        try:
            score = sum(a * b for a, b in zip(qvec, unpack_embedding(row["chunk_embedding"])))
        except ValueError:
            continue
        if row["id"] not in best or score > best[row["id"]][0]:
            best[row["id"]] = (score, row)
    current_ids = _current_ids_map(conn, best)
    head_ids = sorted({head for origin in best for head in current_ids.get(origin, [origin])})
    if not head_ids:
        return []
    head_rows: list[sqlite3.Row] = []
    for batch in _batched(head_ids):
        placeholders = ",".join("?" for _ in batch)
        head_rows.extend(conn.execute(
            f"SELECT * FROM memory_records WHERE id IN ({placeholders})", batch,
        ).fetchall())
    heads = _memory_records_dicts(conn, head_rows)
    ranked_by_head: dict[str, tuple[float, dict[str, Any]]] = {}
    for origin_id, (score, _) in best.items():
        for head_id in current_ids.get(origin_id, [origin_id]):
            record = heads.get(head_id)
            if record is None or not record["is_current"]:
                continue
            if project and record["project"] != project:
                continue
            if record_type and record["record_type"] != record_type:
                continue
            item = dict(record)
            if origin_id != head_id:
                item["matched_via_record_id"] = origin_id
            prior = ranked_by_head.get(head_id)
            if prior is None or score > prior[0] or (
                score == prior[0]
                and item.get("matched_via_record_id", head_id) < prior[1].get("matched_via_record_id", head_id)
            ):
                ranked_by_head[head_id] = (score, item)
    ranked = sorted(ranked_by_head.values(), key=lambda item: (-item[0], item[1]["id"]))
    return [record | {"_semantic": True} for _, record in ranked[:limit]]


def search_all(
    conn: sqlite3.Connection,
    query: str,
    project: str | None,
    category: str | None,
    limit: int,
    *,
    status: str | None = None,
    module: str | None = None,
    bug_id: str | None = None,
    session_label: str | None = None,
    semantic: str = "auto",
) -> list[dict[str, Any]]:
    """Fuse Markdown-derived knowledge and current SQLite-native truth.

    Native records participate in the normal query path unless a filter is
    specific to the Markdown index. Reciprocal-rank fusion keeps scores from
    the two independent FTS indexes comparable without a table-size bias.
    """
    limit = max(1, min(limit, 50))
    candidate_limit = min(50, max(limit * 2, 10))
    markdown = search(
        conn, query, project, category, candidate_limit,
        status=status, module=module, bug_id=bug_id,
        session_label=session_label, semantic=semantic,
    )
    native: list[dict[str, Any]] = []
    native_type = category if category in MEMORY_RECORD_TYPES else None
    native_allowed = (
        table_exists(conn, "memory_records")
        and (category is None or native_type is not None)
        and not any((status, module, bug_id, session_label))
    )
    if native_allowed:
        lexical_native = search_memory_records(
            conn, query, project, native_type, candidate_limit, current_only=True,
        )
        semantic_native = semantic_memory_records(conn, query, project, native_type, candidate_limit) if (
            semantic != "off" and ensure_embed_server(wait=(semantic == "on"))
        ) else []
        merged_native: dict[str, dict[str, Any]] = {}
        # Each retrieval channel gets its own rank sequence.  Concatenating
        # semantic after lexical made the first semantic hit look like rank
        # 31+ whenever lexical returned a full candidate set.
        for rows, is_semantic in ((lexical_native, False), (semantic_native, True)):
            for rank, item in enumerate(rows, start=1):
                prior = merged_native.get(item["id"])
                if prior is None:
                    merged_native[item["id"]] = dict(item) | {
                        "_native_score": 1.0 / (60 + rank), "_semantic": is_semantic,
                    }
                else:
                    prior["_native_score"] += 1.0 / (60 + rank)
                    prior["_semantic"] = prior.get("_semantic") or is_semantic
        native = sorted(merged_native.values(), key=lambda item: (-item["_native_score"], item["id"]))

    fused: dict[tuple[str, Any], dict[str, Any]] = {}
    for source, rows in (("markdown", markdown), ("sqlite", native)):
        for rank, item in enumerate(rows, start=1):
            key = (source, item["id"])
            entry = fused.setdefault(key, {"item": item, "score": 0.0, "source": source})
            entry["score"] += 1.0 / (60 + rank)

    ranked = sorted(
        fused.values(),
        key=lambda entry: (
            -entry["score"], 0 if entry["source"] == "sqlite" else 1,
            str(entry["item"]["id"]),
        ),
    )[:limit]
    results: list[dict[str, Any]] = []
    for entry in ranked:
        item = dict(entry["item"])
        if entry["source"] == "sqlite":
            item.update({
                "category": item["record_type"],
                "excerpt": re.sub(r"\s+", " ", item["content"]).strip()[:500],
                "source_path": "SQLite:memory_records",
                "source_heading": item["id"],
                "line_start": None,
                "line_end": None,
                "status": item["effective_status"],
                "bug_id": "",
                "module": "",
                "session": "",
                "match_reasons": ["sqlite_native", "current_truth"] + (["semantic"] if item.get("_semantic") else []),
            })
        item["source_kind"] = entry["source"]
        item["rank"] = round(float(entry["score"]), 5)
        results.append(item)
    return results


def add_checkpoint(conn: sqlite3.Connection, session: sqlite3.Row, agent: str, payload: dict[str, Any]) -> int:
    # ``add_checkpoint`` is also a public/programmatic write seam used
    # directly by callers and tests, not only by the validated CLI path.
    # Keep the storage boundary strict so no alternate caller can persist a
    # shape that later crashes handoff/timeline readers.
    _validate_checkpoint_payload(payload)
    timestamp = now_utc()
    # Serialize the short sequence-allocation + insert transaction. Deferred
    # transactions let two agents both read MAX(sequence)=N before either
    # writes; BEGIN IMMEDIATE makes the second writer wait at the boundary.
    conn.execute("BEGIN IMMEDIATE")
    try:
        current_status = conn.execute(
            "SELECT status FROM sessions WHERE id = ?", (session["id"],)
        ).fetchone()
        if current_status is None:
            raise ValueError("session no longer exists")
        if current_status["status"] == "completed":
            raise ValueError("cannot checkpoint a completed session")
        sequence = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM checkpoints WHERE session_id = ?", (session["id"],)
        ).fetchone()[0]
        cursor = conn.execute(
            """INSERT INTO checkpoints(
                   session_id, sequence, agent, summary, work_done, current_state, next_steps,
                   blockers, files_changed, commands_run, verification, metadata, created_at, pinned
               ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session["id"], sequence, agent, payload.get("summary", ""), payload.get("work_done", ""),
                payload.get("current_state", ""), payload.get("next_steps", ""), payload.get("blockers", ""),
                json_text(payload.get("files_changed", [])), json_text(payload.get("commands_run", [])),
                json_text(payload.get("verification", [])), json_text(payload.get("metadata", {})), timestamp,
                1 if payload.get("pinned") else 0,
            ),
        )
        # Checkpoints are resumable context, not permanent query history.
        # Retain each session independently so active work cannot evict a
        # paused session's handoff context. Pinned rows are excluded from the
        # candidate set entirely, so the offset counts only unpinned rows --
        # the newest MAX_CHECKPOINTS unpinned checkpoints survive, plus every
        # pinned one regardless of age.
        conn.execute(
            """DELETE FROM checkpoints WHERE id IN (
                   SELECT id FROM checkpoints WHERE session_id = ? AND pinned = 0
                   ORDER BY sequence DESC LIMIT -1 OFFSET ?
               )""",
            (session["id"], MAX_CHECKPOINTS),
        )
        conn.execute(
            "UPDATE sessions SET last_agent = ?, status = ?, updated_at = ? WHERE id = ?",
            (agent, payload.get("status", current_status["status"]), timestamp, session["id"]),
        )
        # Deliberately AFTER the status update above: this very checkpoint may
        # be the one that closes the session, and the guard below must read
        # the status this checkpoint just set, not the one it replaced.
        prune_checkpoints_globally(conn)
        conn.execute(
            "INSERT INTO activity_log(agent, action, project, detail, created_at) VALUES(?, 'checkpoint', ?, ?, ?)",
            (agent, session["project"], json_text({"session_id": session["id"], "sequence": sequence}), timestamp),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return cursor.lastrowid


def prune_checkpoints_globally(conn: sqlite3.Connection, keep: int | None = None) -> int:
    """Second retention tier above MAX_CHECKPOINTS' per-session cap. That cap
    alone bounds nothing overall: it is scoped by session_id, and sessions
    themselves are never pruned, so the real ceiling was 500 x (a session
    count that only ever grows). At the observed ~28 checkpoints/day that is
    unbounded growth in practice, not just in theory.

    The reason a plain "keep the newest N" was not acceptable: it deletes the
    globally oldest rows, which includes the LAST checkpoint of a session
    paused months ago -- and that row is exactly what handoff() returns. The
    result would be a resumable session whose resume context silently became
    null. So the newest checkpoint of every session that is not 'completed'
    is exempt and never counts against the budget.

    Written as `status != 'completed'` rather than
    `status IN ('active','paused','blocked')` so the failure direction is
    safe: a status added to the schema later is protected by default instead
    of silently becoming prunable. Exempt rows sit ON TOP of `keep`, so the
    table settles at keep + (one row per open session) + (pinned rows) -- a
    handful today, and overshooting the budget is the correct way to lose
    this trade.

    A pinned checkpoint (pinned=1, set via `pin-checkpoint` or --pin at
    creation) is exempt the same way: it never counts against `keep` and is
    never deleted by this query, regardless of age or session status. That
    is the whole point of pinning -- a checkpoint an agent/human marked
    important must survive the sliding window even after its session is
    long completed and no longer gets the "newest per open session" pass.

    `keep` is re-read at call time (module default MAX_TOTAL_CHECKPOINTS) for
    the same reason prune_activity_log does it: a default bound at
    definition time could not be monkeypatched by tests."""
    if keep is None:
        keep = MAX_TOTAL_CHECKPOINTS
    cursor = conn.execute(
        """DELETE FROM checkpoints WHERE id IN (
               SELECT id FROM checkpoints
               WHERE pinned = 0
               AND id NOT IN (
                   -- Newest row per open session. id and sequence are both
                   -- monotonic within a session (sequence = MAX+1 at insert),
                   -- so MAX(id) is the same row handoff() reads by MAX(sequence).
                   SELECT MAX(id) FROM checkpoints
                   WHERE session_id IN (SELECT id FROM sessions WHERE status != 'completed')
                   GROUP BY session_id
               )
               ORDER BY id DESC LIMIT -1 OFFSET ?
           )""",
        (keep,),
    )
    return cursor.rowcount


def set_checkpoint_pinned(conn: sqlite3.Connection, checkpoint_id: int, pinned: bool, agent: str) -> dict[str, Any]:
    """Toggle a checkpoint's pinned flag so it's exempted from (or restored
    to) the sliding-window prune in add_checkpoint/prune_checkpoints_globally.

    Retroactive counterpart to --pin at creation time -- an agent often only
    realizes a checkpoint was the important one after later checkpoints have
    already piled up on top of it."""
    with conn:
        cursor = conn.execute(
            "UPDATE checkpoints SET pinned = ? WHERE id = ?", (1 if pinned else 0, checkpoint_id),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"no checkpoint with id {checkpoint_id}")
        row = conn.execute(
            """SELECT c.*, s.project AS session_project FROM checkpoints c
               JOIN sessions s ON s.id = c.session_id WHERE c.id = ?""",
            (checkpoint_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO activity_log(agent, action, project, detail, created_at) VALUES(?, ?, ?, ?, ?)",
            (
                agent, "pin_checkpoint" if pinned else "unpin_checkpoint", row["session_project"],
                json_text({"checkpoint_id": checkpoint_id, "session_id": row["session_id"]}), now_utc(),
            ),
        )
    result = row_dict(row)
    if pinned:
        warning = pinned_checkpoint_warning(conn)
        if warning is not None:
            result["pin_warning"] = warning
    return result


def pinned_checkpoint_warning(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """None when the pinned count is within MAX_PINNED_CHECKPOINTS_WARN, else
    an advisory dict naming the oldest pinned checkpoint to unpin.

    Never blocks anything -- pinning has no hard cap by design (a hard cap
    would defeat the purpose: the whole point is a checkpoint the user
    explicitly marked as must-survive). This is the visibility half of that
    trade: surfaced at the point a pin happens and in `stats`, so silent
    unbounded growth via over-pinning turns into a nudge instead."""
    count = conn.execute("SELECT COUNT(*) FROM checkpoints WHERE pinned = 1").fetchone()[0]
    if count <= MAX_PINNED_CHECKPOINTS_WARN:
        return None
    oldest = conn.execute(
        "SELECT id, session_id, summary, created_at FROM checkpoints WHERE pinned = 1 ORDER BY id ASC LIMIT 1"
    ).fetchone()
    return {
        "pinned_total": count,
        "threshold": MAX_PINNED_CHECKPOINTS_WARN,
        "message": (
            f"{count} checkpoints are pinned, over the advisory threshold of "
            f"{MAX_PINNED_CHECKPOINTS_WARN}. Pinned rows are never auto-pruned, so this "
            "grows the database with no ceiling -- consider unpinning the oldest one."
        ),
        "suggested_unpin": row_dict(oldest) if oldest else None,
    }


def build_pending_worklist(conn: sqlite3.Connection, project: str | None = None) -> dict[str, Any]:
    """Return the lifecycle-aware pending-work view without changing handoff.

    Stored record status alone is not workflow truth: an open audit can be a
    historical member of a component whose current head is a fix.  Resolve
    through the materialized component index and emit one current head only.
    """
    health = memory_relation_health(conn)
    warnings: list[str] = []
    lifecycle_complete = health["available"] and not any(
        health[key] for key in (
            "lifecycle_cycles", "lifecycle_branch_targets", "component_errors",
            "ambiguous_current_records", "invalid_typed_relations",
        )
    )
    if not lifecycle_complete:
        warnings.append("memory-record lifecycle index is unavailable or inconsistent; actionable records omitted")

    actionable: list[dict[str, Any]] = []
    blocked_records: list[dict[str, Any]] = []
    deferred_records: list[dict[str, Any]] = []
    historical_open_suppressed = 0
    if lifecycle_complete:
        rows = conn.execute("SELECT * FROM memory_records ORDER BY updated_at DESC, id").fetchall()
        records = _memory_records_dicts(conn, rows)
        for record in records.values():
            # Scope the entire component outcome by the record's current head
            # project. A project report must not count another project's old
            # audit as one of its suppressed historical records, so a
            # superseded record is judged by where its lifecycle now lives,
            # not by the project it was originally filed under.
            heads = record["current_record_ids"] or [record["id"]]
            head_projects = {
                records[head]["project"] if head in records else record["project"]
                for head in heads
            }
            if project and project not in head_projects:
                continue
            if not record["is_current"]:
                if record["status"] == "open":
                    historical_open_suppressed += 1
                continue
            action_state = record.get("action_state", "nonactionable")
            unresolved = action_state in {"actionable", "blocked", "deferred"} or record["has_unresolved_conflict"]
            if not unresolved:
                continue
            metadata = record["metadata"] if isinstance(record["metadata"], dict) else {}
            entry = {
                "kind": "record", "id": record["id"], "project": record["project"],
                "title": record["title"], "updated_at": record["updated_at"],
                "record_type": record["record_type"], "stored_status": record["status"],
                "action_state": action_state,
                "effective_status": record["effective_status"], "current_record_id": record["id"],
                "action_reasons": ([f"action_state_{action_state}"] if action_state in {"actionable", "blocked", "deferred"} else [])
                                  + (["unresolved_conflict"] if record["has_unresolved_conflict"] else []),
                "conflicts_with": record["conflicts_with"], "source": metadata.get("source"),
                "rank_reason": "unresolved current-record conflict" if record["has_unresolved_conflict"]
                               else f"current {record['record_type']} record marked {action_state}",
            }
            if record["has_unresolved_conflict"] and action_state not in {"blocked", "deferred"}:
                actionable.append(entry)
            elif action_state == "blocked":
                blocked_records.append(entry)
            elif action_state == "deferred":
                deferred_records.append(entry)
            elif action_state == "actionable":
                actionable.append(entry)

    type_order = {"audit": 0, "verification": 1, "fix": 2, "decision": 3, "knowledge": 4}
    # Stable sorts keep severity first while using newest-first and then ID
    # as deterministic tie-breakers within the same severity/type bucket.
    actionable.sort(key=lambda item: item["id"])
    actionable.sort(key=lambda item: item["updated_at"], reverse=True)
    actionable.sort(key=lambda item: type_order[item["record_type"]])
    actionable.sort(key=lambda item: 0 if "unresolved_conflict" in item["action_reasons"] else 1)
    for entries in (blocked_records, deferred_records):
        entries.sort(key=lambda item: item["id"])
        entries.sort(key=lambda item: item["updated_at"], reverse=True)

    presence = list_presence(conn, project)
    active_presence = [item for item in presence["local"] if not item["stale"]]
    last_known_presence = presence["remote"] + [item for item in presence["local"] if item["stale"]]
    last_known_presence.sort(key=lambda item: item["agent"])
    last_known_presence.sort(key=lambda item: item["last_heartbeat"], reverse=True)
    last_known_presence.sort(key=lambda item: 0 if item.get("source") == "local" else 1)
    resumable = _pending_session_entries(conn, "paused", project)
    active_sessions = _pending_session_entries(conn, "active", project)
    blocked_sessions = _pending_session_entries(conn, "blocked", project)
    return {
        "schema_version": 1, "generated_at": now_utc(),
        "scope": {"project": project, "all_projects": project is None},
        "complete": lifecycle_complete,
        "summary": {
            "active_presence": len(active_presence), "last_known_presence": len(last_known_presence),
            "active_sessions": len(active_sessions), "resumable_sessions": len(resumable),
            "actionable_records": len(actionable), "blocked_sessions": len(blocked_sessions),
            "blocked_records": len(blocked_records), "deferred_records": len(deferred_records),
            "historical_open_suppressed": historical_open_suppressed,
        },
        "active_presence": active_presence, "last_known_presence": last_known_presence,
        "active_sessions": active_sessions, "resumable_sessions": resumable,
        "actionable_records": actionable, "blocked_sessions": blocked_sessions,
        "blocked_records": blocked_records, "deferred_records": deferred_records,
        "requires_user_selection": len(resumable) > 0,
        "warnings": warnings,
    }


def local_machine() -> str:
    return getpass.getuser()


def _presence_sidecar_path(machine: str) -> Path:
    return PRESENCE_DIR / f"{machine}.json"


@contextmanager
def _presence_sidecar_lock(machine: str):
    with _sidecar_lock(PRESENCE_DIR, machine):
        yield


def _write_presence_sidecar(conn: sqlite3.Connection, machine: str) -> None:
    """Mirror this machine's own active rows to a file only THIS machine ever
    writes. One writer per sidecar file prevents cross-host overwrite of that
    file. This does not make a shared SQLite database safe for concurrent
    writes. Best-effort: a failure here must never fail the presence call that
    triggered it -- by the time this runs the caller's DB write has already
    committed, so raising would report a failure for work that actually
    succeeded. Hence the broad `except`, matching refresh_activity_export's
    house pattern: warn on stderr so a real bug stays visible, but never
    propagate.

    Serialized across same-machine writers -- see _sidecar_lock. The SELECT
    is deliberately INSIDE the lock: it is a local, busy-timeout-bounded
    read, and hoisting it out would reintroduce exactly the stale-snapshot
    overwrite the lock exists to prevent. If it does raise (another local
    process holding the write lock past the busy timeout), the sidecar simply
    is not refreshed this round and the next heartbeat republishes it."""
    try:
        with _presence_sidecar_lock(machine):
            rows = conn.execute(
                "SELECT * FROM agent_presence WHERE machine = ? AND status = 'active' ORDER BY last_heartbeat DESC",
                (machine,),
            ).fetchall()
            payload = {"machine": machine, "updated_at": now_utc(), "agents": [dict(r) for r in rows]}
            tmp_path = PRESENCE_DIR / f".{machine}.{os.getpid()}.{uuid.uuid4().hex}.json.tmp"
            tmp_path.write_text(json_text(payload), encoding="utf-8")
            tmp_path.replace(_presence_sidecar_path(machine))
        _reap_stale_sidecar_temps(PRESENCE_DIR)
    except Exception as exc:
        print(f"warning: could not refresh presence sidecar: {exc}", file=sys.stderr)


def presence_start(
    conn: sqlite3.Connection, machine: str, agent: str, project: str, task: str, pid: int | None = None,
    instance: str = "", session_id: str | None = None,
) -> dict[str, Any]:
    """Identity is (machine, agent, project, instance) -- NOT pid. Every CLI
    invocation, including MCP writes that shell out to the CLI, is its own
    short-lived subprocess, so pid cannot survive across a
    start -> heartbeat -> stop sequence made of separate calls. `pid` is
    stored only as an informational "last process" value."""
    timestamp = now_utc()
    with conn:
        conn.execute(
            """INSERT INTO agent_presence(machine, agent, project, instance, pid, task, session_id, status, started_at, last_heartbeat)
               VALUES(?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
               ON CONFLICT(machine, agent, project, instance) DO UPDATE SET
                   pid=excluded.pid, task=excluded.task,
                   session_id=excluded.session_id, status='active',
                   started_at=excluded.started_at, last_heartbeat=excluded.last_heartbeat""",
            (machine, agent, project, instance, pid, task, session_id, timestamp, timestamp),
        )
        conn.execute(
            "INSERT INTO activity_log(agent, action, project, detail, created_at) VALUES(?, 'presence_start', ?, ?, ?)",
            (agent, project, json_text({"machine": machine, "instance": instance, "task": task}), timestamp),
        )
        _prune_expired_presence_rows(conn, machine)
    _write_presence_sidecar(conn, machine)
    return {"machine": machine, "agent": agent, "project": project, "instance": instance, "task": task, "started_at": timestamp}


def presence_heartbeat(
    conn: sqlite3.Connection, machine: str, agent: str, project: str, pid: int | None = None,
    task: str | None = None, instance: str = "",
) -> int:
    """Finds the row by (machine, agent, project, instance) -- see
    presence_start's docstring for why not pid. Returns 0 (a no-op, not an
    error) if that identity has no *active* row: either it never called
    presence_start, or it already called presence_stop. `task=None` means
    "just refresh the timestamp", distinct from `task=""` which clears it.

    Only 'active' rows are matched, so a late heartbeat racing a
    presence_stop cannot resurrect the row. That mattered more than it
    looks: only ``presence_start`` deliberately resumes work after a stop.
    Restarting work is presence_start's job (it already reactivates on
    conflict)."""
    timestamp = now_utc()
    set_clauses = ["last_heartbeat = ?"]
    params: list[Any] = [timestamp]
    if task is not None:
        set_clauses.append("task = ?")
        params.append(task)
    if pid is not None:
        set_clauses.append("pid = ?")
        params.append(pid)
    params += [machine, agent, project, instance]
    with conn:
        cursor = conn.execute(
            f"UPDATE agent_presence SET {', '.join(set_clauses)} "
            "WHERE machine = ? AND agent = ? AND project = ? AND instance = ? AND status = 'active'",
            params,
        )
    if cursor.rowcount:
        _write_presence_sidecar(conn, machine)
    return cursor.rowcount


def presence_stop(
    conn: sqlite3.Connection, machine: str, agent: str, project: str, instance: str = "",
) -> int:
    """Finds the row by (machine, agent, project, instance) -- see
    presence_start's docstring for why not pid. Returns 0 (a no-op, not an
    error) if that identity was never started or is already stopped; a
    stopped row is left in place until a later presence_start prunes it once
    it is older than three days."""
    timestamp = now_utc()
    with conn:
        cursor = conn.execute(
            "UPDATE agent_presence SET status = 'stopped', last_heartbeat = ? "
            "WHERE machine = ? AND agent = ? AND project = ? AND instance = ? AND status = 'active'",
            (timestamp, machine, agent, project, instance),
        )
        if cursor.rowcount:
            conn.execute(
                "INSERT INTO activity_log(agent, action, project, detail, created_at) VALUES(?, 'presence_stop', ?, ?, ?)",
                (agent, project, json_text({"machine": machine, "instance": instance}), timestamp),
            )
    if cursor.rowcount:
        _write_presence_sidecar(conn, machine)
    return cursor.rowcount


def _read_presence_sidecars(local_machine_name: str) -> list[dict[str, Any]]:
    """Every OTHER machine's last-mirrored snapshot. Never this machine's own
    sidecar -- the live `agent_presence` table is always authoritative for
    the local machine, so mixing in a stale self-write would only ever make
    the local view worse, never better.

    A sidecar file is untrusted input from another host's process (or a
    partially propagated, renamed, or conflicted copy):
    a single malformed file must degrade to "skip this one file", never crash
    the whole presence listing. Any exception type a bad payload can raise
    (KeyError/TypeError/ValueError from missing keys, wrong container types,
    or unparsable timestamps -- not just OSError/JSONDecodeError) is caught
    per-file and per-entry."""
    remote: list[dict[str, Any]] = []
    if not PRESENCE_DIR.is_dir():
        return remote
    for path in sorted(PRESENCE_DIR.glob("*.json")):
        if path.stem == local_machine_name:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("machine") != path.stem:
                # Filename and declared identity disagree (e.g. a renamed or
                # conflicted copy) -- untrustworthy, skip the whole file
                # rather than risk mislabeling a stray local entry as remote.
                continue
            snapshot_age = (datetime.now(timezone.utc) - datetime.fromisoformat(payload["updated_at"])).total_seconds()
            agents = payload.get("agents", [])
            if not isinstance(agents, list):
                continue
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        for entry in agents:
            try:
                item = dict(entry)
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(item["last_heartbeat"])).total_seconds()
            except (KeyError, TypeError, ValueError):
                continue
            item["stale"] = age > PRESENCE_STALE_SEC
            item["age_seconds"] = int(age)
            item["source"] = "sidecar"
            item["sidecar_age_seconds"] = int(snapshot_age)
            remote.append(item)
    return remote


def list_presence(conn: sqlite3.Connection, project: str | None = None) -> dict[str, Any]:
    """`agent_presence` is local database state. Only rows stamped with THIS
    machine's own identity are real-time; remote observations must come from
    the sidecar path (see _read_presence_sidecars) so they are correctly
    labeled "last known" rather than misread as live.

    `presence` is a read-only CLI command, so it never runs `initialize()` --
    on a database still at the pre-agent_presence schema version (v5), the
    table simply doesn't exist yet. Degrade to "no local agents" rather than
    raising OperationalError; the next write command migrates the schema
    normally and presence starts working without any special action."""
    machine = local_machine()
    if not table_exists(conn, "agent_presence"):
        local_rows: list[dict[str, Any]] = []
    else:
        query = "SELECT * FROM agent_presence WHERE status = 'active' AND machine = ?"
        params: list[Any] = [machine]
        if project:
            query += " AND project = ?"
            params.append(project)
        query += " ORDER BY last_heartbeat DESC"
        local_rows = [_presence_row_dict(row) | {"source": "local"} for row in conn.execute(query, params).fetchall()]
    remote_rows = _read_presence_sidecars(machine)
    if project:
        remote_rows = [r for r in remote_rows if r.get("project") == project]
    return {"machine": machine, "local": local_rows, "remote": remote_rows}


def _sync_freshness_path(machine: str) -> Path:
    return SYNC_FRESHNESS_DIR / f"{machine}.json"


def write_sync_freshness_signal(machine: str, command: str) -> None:
    """Record "this machine just wrote the shared .sqlite3" to a file only
    THIS machine ever writes -- same single-writer-per-file safety property
    as the presence sidecar (see _write_presence_sidecar), applied to the
    general write path instead of just agent_presence. This is informational
    only: it does not gate or authorize any write; it gives an operator a
    concrete lower bound on when another host last wrote.

    Serialized and uniquely-named for the same reason as the presence
    sidecar: several local processes (Codex, Claude, a plain CLI call) can
    run write commands at once, and a shared temp path plus no lock lets a
    slower writer's older snapshot land last and overwrite a fresher one --
    a lost update that would silently understate how recently this machine
    wrote. Best-effort: must never fail the write that triggered it."""
    try:
        with _sidecar_lock(SYNC_FRESHNESS_DIR, machine):
            payload = {"machine": machine, "last_write_at": now_utc(), "last_command": command}
            tmp_path = SYNC_FRESHNESS_DIR / f".{machine}.{os.getpid()}.{uuid.uuid4().hex}.json.tmp"
            tmp_path.write_text(json_text(payload), encoding="utf-8")
            tmp_path.replace(_sync_freshness_path(machine))
        _reap_stale_sidecar_temps(SYNC_FRESHNESS_DIR)
    except Exception as exc:
        print(f"warning: could not write sync-freshness signal: {exc}", file=sys.stderr)


def sync_freshness_report(local_machine_name: str) -> dict[str, Any]:
    """Last-known write time for every machine that has ever run a write
    command, read from each machine's own sidecar (never guessed from a
    shared database file's mtime, which does not identify its last writer).
    `age_seconds` on an entry other than `local_machine_name` is only as
    fresh as any external propagation has carried that file -- it is a lower
    bound on how long ago that host actually wrote, never an upper bound.

    Each file is untrusted input from another machine's process (or a
    partially-propagated/renamed/conflicted copy of one), exactly like the
    presence sidecars -- so it gets the identical hardening as
    _read_presence_sidecars, and for the same reason: one malformed file
    must degrade to "skip this one file", never crash the whole report and
    take every valid sibling down with it. That means catching the full set
    a bad payload can raise (KeyError/TypeError/ValueError from missing
    keys, wrong container types, or unparsable timestamps -- not just
    OSError/JSONDecodeError), and rejecting a file whose declared identity
    disagrees with its own filename."""
    machines: dict[str, Any] = {}
    if SYNC_FRESHNESS_DIR.is_dir():
        for path in sorted(SYNC_FRESHNESS_DIR.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict) or payload.get("machine") != path.stem:
                    continue
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(payload["last_write_at"])).total_seconds()
                entry = {
                    "last_write_at": payload["last_write_at"],
                    "last_command": payload.get("last_command", ""),
                    "age_seconds": int(age),
                    "is_local": payload["machine"] == local_machine_name,
                }
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            machines[payload["machine"]] = entry
    return {"machine": local_machine_name, "machines": machines}


def _tracked_docs_summary(conn: sqlite3.Connection, project: str | None = None) -> dict[str, Any]:
    """Freshness counts of the tracked-doc manifest, compact enough for a
    session-start report; run `sync_tracked.py --check` for the full paths.

    A project preflight narrows current/stale/missing/metadata counts to that
    project's tracked documents and its indexed orphan rows. A bootstrap with
    no project scope retains the historical workspace-wide report.
    """
    try:
        # Deferred: sync_tracked imports this module (top-level import would be
        # circular) and its manifest discovery shells out to git ls-files.
        import sync_tracked
        tracked = sync_tracked.discover_knowledge_docs()
        if project is not None:
            tracked = {
                rel_path: metadata for rel_path, metadata in tracked.items()
                if metadata[0] == project
            }
        report = sync_tracked.freshness_report(
            ROOT, conn, tracked, include_orphans=project is None,
        )
        if project is not None:
            expected_paths = {
                sync_tracked._database_source_path(ROOT, rel_path)  # noqa: SLF001 - shared manifest contract
                for rel_path in tracked
            }
            report["orphaned"] = sorted(
                row["source_path"]
                for row in conn.execute(
                    "SELECT source_path FROM documents WHERE project = ?", (project,),
                )
                if row["source_path"] not in expected_paths
            )
    except Exception as exc:  # git unavailable, manifest error — never block bootstrap
        return {"ok": None, "error": f"{type(exc).__name__}: {exc}"}
    summary: dict[str, Any] = {key: len(values) for key, values in report.items()}
    if project is not None:
        summary["scope"] = project
    summary["ok"] = not any(
        report[key] for key in ("stale", "missing", "orphaned", "metadata_mismatch")
    )
    return summary


def doctor_report(conn: sqlite3.Connection, path: Path) -> dict[str, Any]:
    """Return the read-only health report shared by ``doctor`` and readiness.

    Keeping the health predicate in one place prevents the compact preflight
    from declaring a database healthy under a weaker definition than the
    detailed diagnostic command. Embedding availability remains informative,
    not a database-health gate.
    """
    schema_version = database_schema_version(conn)
    schema_current = schema_version == SCHEMA_VERSION
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    foreign_key_issues = [list(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()]
    knowledge_count = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
    fts_identity = {
        table: fts_index_identity(
            conn, "knowledge", "id", f"{table}_vocab",
            fts_table=table, match_expression=KNOWLEDGE_FTS_IDENTITY_EXPRESSION,
        )
        for table in ("knowledge_fts", "knowledge_fts_porter", "knowledge_fts_trigram")
    }
    fts_counts = {table: item["indexed"] for table, item in fts_identity.items()}
    embedding = embedding_stats(conn)
    memory_relations = memory_relation_health(conn)
    hooks = hook_status()
    return {
        "database": str(path),
        "schema_version": schema_version,
        "schema_current": schema_current,
        "integrity": integrity,
        "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
        "foreign_keys": bool(conn.execute("PRAGMA foreign_keys").fetchone()[0]),
        "foreign_key_issues": foreign_key_issues,
        "knowledge_rows": knowledge_count,
        "fts_rows": fts_counts,
        "fts_identity": fts_identity,
        "embedding": embedding,
        "memory_relations": memory_relations,
        "hooks": hooks,
        "ok": (
            schema_current
            and integrity == "ok"
            and not foreign_key_issues
            and all(count == knowledge_count for count in fts_counts.values())
            and all(item["verified"] for item in fts_identity.values())
            and all(not item["missing"] and not item["extra"] for item in fts_identity.values())
            and embedding["invalid_blobs"] == 0
            and memory_relations["ok"]
            and hooks_ok(hooks)
        ),
    }


def readiness_report(conn: sqlite3.Connection, project: str, path: Path) -> dict[str, Any]:
    """Build an actionable, non-mutating preflight for one project.

    The report deliberately never calls bootstrap, backfill, or warm: it is
    safe on both Macs and reports what needs attention before a session starts.
    ANN status is inspected only; no sidecar is built here.
    """
    doctor = doctor_report(conn, path)
    embedding = doctor["embedding"]
    docs = _tracked_docs_summary(conn, project)
    ann_status = ann_helper(conn, "status")
    if not isinstance(ann_status, dict) or not ann_status.get("status"):
        ann_status = {
            "status": "unavailable", "available": None, "fresh": False,
            "error": "ANN helper returned no structured status",
        }
    else:
        ann_status = {
            **ann_status, "available": ann_status.get("available"),
            "fresh": ann_status.get("fresh") is True,
        }
    knowledge_rows = int(embedding.get("knowledge_rows", 0))
    embedded = int(embedding.get("embedded", 0))
    native_rows = int(embedding.get("memory_record_chunks", 0))
    native_embedded = int(embedding.get("memory_record_embedded", 0))
    ann_required = embedded > SEMANTIC_FULL_SCAN_LIMIT
    fts_ok = (
        all(count == doctor["knowledge_rows"] for count in doctor["fts_rows"].values())
        and all(
            item["verified"] and not item["missing"] and not item["extra"]
            for item in doctor["fts_identity"].values()
        )
    )
    core_database_ok = (
        doctor["schema_current"]
        and doctor["integrity"] == "ok"
        and not doctor["foreign_key_issues"]
        and fts_ok
        and embedding["invalid_blobs"] == 0
        and doctor["memory_relations"]["ok"]
    )

    actions: list[dict[str, Any]] = []
    if not core_database_ok:
        actions.append({
            "priority": "P0", "code": "database_health",
            "reason": "SQLite, schema, FTS, embedding-blob, or record-lifecycle health is not current.",
            "command": "python3 ENDMEMEX/endeavor_db.py doctor",
            "guidance": "Resolve the detailed doctor findings before making ENDMEMEX writes.",
        })
    if not hooks_ok(doctor["hooks"]):
        actions.append({
            "priority": "P1", "code": "install_hooks",
            "reason": "The advisory tracked-document sync hook is missing or drifted.",
            "command": "python3 ENDMEMEX/endeavor_db.py install-hooks",
            "guidance": "Install the hook in this clone, then rerun readiness.",
        })

    docs_error = docs.get("error")
    docs_drift = sum(int(docs.get(key, 0)) for key in ("stale", "missing", "metadata_mismatch"))
    if docs_error:
        actions.append({
            "priority": "P1", "code": "document_freshness_unavailable",
            "reason": str(docs_error),
            "command": "python3 ENDMEMEX/sync_tracked.py --check --json",
            "guidance": "Restore the tracked-document check before trusting freshness counts.",
        })
    elif docs_drift:
        actions.append({
            "priority": "P1", "code": "sync_tracked_documents",
            "reason": f"{docs_drift} tracked document(s) are stale, missing, or have metadata drift.",
            "command": "python3 ENDMEMEX/sync_tracked.py --check --json",
            "guidance": "Review the listed paths, then sync each approved tracked document.",
        })
    if int(docs.get("orphaned", 0)):
        actions.append({
            "priority": "P1", "code": "review_orphaned_documents",
            "reason": f"{docs['orphaned']} indexed document(s) are outside the tracked manifest.",
            "command": "python3 ENDMEMEX/sync_tracked.py --propose-prune /private/tmp/endmemex-prune-proposal.json",
            "guidance": "Review the hash-pinned proposal; never prune automatically from readiness.",
        })

    embedding_pending = int(embedding.get("pending", 0)) + int(embedding.get("memory_record_pending", 0))
    stale_embeddings = int(embedding.get("stale_hashes", 0))
    if embedding_pending or stale_embeddings:
        actions.append({
            "priority": "P1", "code": "embedding_coverage",
            "reason": f"{embedding_pending} embedding(s) are pending and {stale_embeddings} hash(es) are stale.",
            "command": "python3 ENDMEMEX/endeavor_db.py embed-diagnose",
            "guidance": "Follow the structured diagnosis before running embed-backfill or changing a companion.",
        })
    if ann_required and not ann_status.get("fresh"):
        available = ann_status.get("available")
        if available is True:
            command = "python3 ENDMEMEX/endeavor_db.py ann-build"
            guidance = "Build the sidecar on this machine, then rerun readiness to confirm it is fresh."
        elif available is False:
            command = None
            guidance = "Install optional numpy and hnswlib in the selected runtime, then build the per-machine sidecar."
        else:
            command = "python3 ENDMEMEX/endeavor_db.py ann-status"
            guidance = "The ANN probe did not establish dependency state; inspect its error before changing a runtime."
        actions.append({
            "priority": "P1", "code": "ann_sidecar",
            "reason": f"{embedded} embedded knowledge rows exceed the exact-scan limit of {SEMANTIC_FULL_SCAN_LIMIT}.",
            "command": command,
            "guidance": guidance,
        })

    if not actions:
        actions.append({
            "priority": "OK", "code": "ready",
            "reason": "The project preflight has no blocking or attention items.",
            "command": shlex.join([
                "python3", "ENDMEMEX/endeavor_db.py", "bootstrap", "--project", project, "--json",
            ]),
            "guidance": "Start or resume the normal session workflow when work is ready to begin.",
        })
    overall = "blocked" if any(item["priority"] == "P0" for item in actions) else (
        "attention" if any(item["priority"] == "P1" for item in actions) else "ready"
    )
    return {
        "project": project,
        "overall": overall,
        "read_only": True,
        "machine": {
            "name": local_machine(),
            "role": "local",
            "write_allowed": True,
        },
        "database": {
            "ok": doctor["ok"], "core_ok": core_database_ok,
            "schema_version": doctor["schema_version"], "schema_current": doctor["schema_current"],
            "integrity": doctor["integrity"], "foreign_key_issues": doctor["foreign_key_issues"],
            "fts_ok": fts_ok, "memory_relations_ok": doctor["memory_relations"]["ok"],
            "hooks": doctor["hooks"],
        },
        "embedding": {
            "knowledge_rows": knowledge_rows, "embedded": embedded,
            "coverage": round(embedded / knowledge_rows, 4) if knowledge_rows else 1.0,
            "pending": int(embedding.get("pending", 0)), "invalid_blobs": int(embedding.get("invalid_blobs", 0)),
            "stale_hashes": stale_embeddings,
            "memory_record_chunks": native_rows, "memory_record_embedded": native_embedded,
            "memory_record_coverage": round(native_embedded / native_rows, 4) if native_rows else 1.0,
            "memory_record_pending": int(embedding.get("memory_record_pending", 0)),
        },
        "ann": {
            **ann_status,
            "required": ann_required,
            "exact_scan_limit": SEMANTIC_FULL_SCAN_LIMIT,
        },
        "documents": docs,
        "next_actions": actions,
    }


def unavailable_readiness_report(project: str, path: Path, error: str) -> dict[str, Any]:
    """Return a useful preflight when a database cannot be opened read-only.

    A health command that simply fails on a new or unavailable database hides
    the one action an operator needs. This path deliberately does not create or
    migrate anything; it only explains the state and points to the explicit
    initialization command.
    """
    database_action = {
        "priority": "P0", "code": "database_unavailable",
        "reason": f"Cannot open the ENDMEMEX database: {error}",
        "command": "python3 ENDMEMEX/endeavor_db.py init",
        "guidance": "Initialize or restore the local database, then rerun readiness.",
    }
    actions = [database_action]
    return {
        "project": project,
        "overall": "blocked",
        "read_only": True,
        "machine": {
            "name": local_machine(),
            "role": "local",
            "write_allowed": True,
        },
        "database": {
            "ok": False, "core_ok": False, "schema_version": None, "schema_current": False,
            "integrity": "unavailable", "foreign_key_issues": [], "fts_ok": False,
            "memory_relations_ok": False, "hooks": {},
        },
        "embedding": {
            "knowledge_rows": 0, "embedded": 0, "coverage": 0.0, "pending": 0,
            "invalid_blobs": 0, "stale_hashes": 0, "memory_record_chunks": 0,
            "memory_record_embedded": 0, "memory_record_coverage": 0.0,
            "memory_record_pending": 0,
        },
        "ann": {
            "status": "unavailable", "available": False, "fresh": False,
            "required": False, "exact_scan_limit": SEMANTIC_FULL_SCAN_LIMIT,
        },
        "documents": {
            "current": 0, "stale": 0, "missing": 0, "metadata_mismatch": 0,
            "orphaned": 0, "ok": None, "error": f"database unavailable: {error}",
        },
        "next_actions": actions,
    }


def render_readiness(report: dict[str, Any]) -> str:
    """Render the one-command preflight compactly for a human terminal."""
    machine = report["machine"]
    database = report["database"]
    embedding = report["embedding"]
    ann = report["ann"]
    docs = report["documents"]
    lines = [
        f"Readiness: {report['overall'].upper()} ({report['project']})",
        f"Machine: {machine['name']} — {machine['role']}",
        f"Database: {'OK' if database['ok'] else 'ATTENTION'} (integrity={database['integrity']}, fts_ok={database['fts_ok']})",
        f"Embeddings: {embedding['embedded']}/{embedding['knowledge_rows']} knowledge; "
        f"{embedding['memory_record_embedded']}/{embedding['memory_record_chunks']} record chunks",
        f"ANN: {ann.get('status', 'unavailable')} (required={ann['required']}, fresh={ann.get('fresh', False)})",
        "Documents: " + ", ".join(f"{key}={docs.get(key, 0)}" for key in (
            "current", "stale", "missing", "metadata_mismatch", "orphaned",
        )),
        "Next action:",
    ]
    for item in report["next_actions"]:
        line = f"  [{item['priority']}] {item['reason']}"
        if item["command"]:
            line += f"\n    {item['command']}"
        lines.append(line)
    return "\n".join(lines)


def bootstrap(
    conn: sqlite3.Connection, project: str, batch_size: int = EMBED_BATCH_SIZE,
    include_pending: bool = False, session_id: str | None = None,
) -> dict[str, Any]:
    """One-call session start replacing the multi-step §7.5 ritual: latest
    handoff (nulls when the project has nothing resumable — the normal state
    of a new task), best-effort embedding backfill, tracked-doc freshness
    counts, and hook installation state. Every part degrades independently;
    a cold companion or a missing git binary never hides the handoff."""
    try:
        data = handoff(conn, session_id, project)
    except SessionNotFoundError:
        if session_id:
            raise
        data = {"session": None, "checkpoint": None}
    result = {
        "project": project,
        **data,
        "embedding": backfill_embeddings(conn, batch_size),
        "docs": _tracked_docs_summary(conn),
        "hooks": hook_status(),
    }
    if include_pending:
        result["pending"] = build_pending_worklist(conn, project)
    return result


def build_pack(
    conn: sqlite3.Connection, project: str, budget_chars: int = PACK_DEFAULT_BUDGET_CHARS,
    session_id: str | None = None,
) -> dict[str, Any]:
    """One-call session briefing: handoff + open native records + recent
    project knowledge + recent activity, kept within a serialized character
    budget across every optional response section.

    Unlike `query`, `knowledge` here is recency-ordered, not ranked against a
    question — this is a briefing for "what's going on", not a search result.
    `open_records` is retained as a raw status filter for compatibility.
    `actionable_records` is the lifecycle-aware pending-work subset.
    """
    try:
        handoff_data = handoff(conn, session_id, project)
    except SessionNotFoundError:
        if session_id:
            raise
        handoff_data = {"session": None, "checkpoint": None}

    open_records_all = [
        {"id": row["id"], "record_type": row["record_type"], "title": row["title"], "status": row["status"]}
        for row in conn.execute(
            "SELECT id, record_type, title, status FROM memory_records "
            "WHERE project = ? AND status = 'open' ORDER BY updated_at DESC, id LIMIT 50",
            (project,),
        )
    ] if table_exists(conn, "memory_records") else []

    recent_activity_all = [
        {"agent": row["agent"], "action": row["action"], "created_at": row["created_at"]}
        for row in conn.execute(
            "SELECT agent, action, created_at FROM activity_log WHERE project = ? ORDER BY id DESC LIMIT 10",
            (project,),
        )
    ] if table_exists(conn, "activity_log") else []

    knowledge_all: list[dict[str, Any]] = []
    if table_exists(conn, "knowledge"):
        rows = conn.execute(
            "SELECT id, title, category, source_path, source_heading, source_line_start, "
            "source_line_end, substr(content, 1, 400) AS excerpt FROM knowledge "
            "WHERE project = ? ORDER BY updated_at DESC LIMIT 30",
            (project,),
        ).fetchall()
        for row in rows:
            location = row["source_path"] or ""
            if row["source_line_start"] is not None:
                location += f":{row['source_line_start']}-{row['source_line_end']}"
            elif row["source_heading"]:
                location += f":{row['source_heading']}"
            entry = {
                "id": row["id"], "title": row["title"], "category": row["category"],
                "location": location, "excerpt": row["excerpt"],
            }
            knowledge_all.append(entry)

    pending = build_pending_worklist(conn, project)
    actionable_all = list(pending.get("actionable_records", []))
    warnings_all = list(pending.get("warnings", []))
    omitted = {
        "session": 1 if handoff_data["session"] is not None else 0,
        "checkpoint": 1 if handoff_data["checkpoint"] is not None else 0,
        "open_records": len(open_records_all),
        "actionable_records": len(actionable_all),
        "pending_warnings": len(warnings_all),
        "knowledge": len(knowledge_all),
        "recent_activity": len(recent_activity_all),
    }
    result: dict[str, Any] = {
        "project": project,
        "session": None,
        "checkpoint": None,
        "open_records": [],
        "open_records_semantics": "raw_stored_status",
        "actionable_records": [],
        "pending_complete": pending.get("complete", False),
        "pending_warnings": [],
        "knowledge": [],
        "recent_activity": [],
        "budget_chars": budget_chars,
        "budget_omitted_counts": omitted,
        "truncated": True,
    }

    def refresh_truncated() -> None:
        result["truncated"] = any(omitted.values())

    refresh_truncated()
    if len(json_text(result)) > budget_chars:
        raise ValueError("pack budget is too small for the fixed response envelope")

    def include_single(key: str, value: Any) -> None:
        result[key] = value
        omitted[key] -= 1
        refresh_truncated()
        if len(json_text(result)) > budget_chars:
            result[key] = None
            omitted[key] += 1
            refresh_truncated()

    def include_items(key: str, values: list[Any]) -> None:
        target = result[key]
        for value in values:
            target.append(value)
            omitted[key] -= 1
            refresh_truncated()
            if len(json_text(result)) > budget_chars:
                target.pop()
                omitted[key] += 1
                refresh_truncated()

    if handoff_data["session"] is not None:
        include_single("session", handoff_data["session"])
    if handoff_data["checkpoint"] is not None:
        include_single("checkpoint", handoff_data["checkpoint"])
    include_items("actionable_records", actionable_all)
    include_items("open_records", open_records_all)
    include_items("pending_warnings", warnings_all)
    include_items("recent_activity", recent_activity_all)
    include_items("knowledge", knowledge_all)
    return result


def auto_detect_changed_files(project: str) -> list[str]:
    """Best-effort git-status file list scoped to a project's own top-level
    directory, for checkpoint --auto-files. A project label that isn't also
    a real direct child directory of ROOT yields an empty list rather than
    falling back to an unscoped repo-wide `git status` — a checkpoint's
    files_changed is supposed to be trustworthy provenance, and guessing
    across whatever another concurrent agent happens to be editing elsewhere
    in the repo would make it worse than an empty list the caller must fill
    in by hand. Labels containing path separators or dot components are
    rejected outright for the same reason: "." resolves to ROOT itself and
    would silently widen the pathspec to the whole repository.
    """
    if not project or project in {".", ".."} or "/" in project or "\\" in project:
        return []
    candidate = ROOT / project
    if not candidate.is_dir():
        return []
    # --untracked-files=all expands a wholly-new directory into its individual
    # files instead of collapsing it to one "dir/" line. Bounded to one
    # project subdirectory via the pathspec, this doesn't carry the full-repo
    # memory-cost concern that rules out -uall for an unscoped git status.
    # core.quotepath=off keeps non-ASCII (e.g. Thai) filenames as real UTF-8
    # instead of C-quoted octal escapes that would corrupt files_changed.
    result = subprocess.run(
        ["git", "-C", str(ROOT), "-c", "core.quotepath=off",
         "status", "--porcelain", "--untracked-files=all", "--", project],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return []
    files: list[str] = []
    for line in result.stdout.splitlines():
        # Porcelain v1: "XY PATH" or "XY ORIG -> PATH" for renames.
        path = line[3:].split(" -> ")[-1].strip()
        if path:
            files.append(path)
    return files


_CHECKPOINT_PAYLOAD_TEXT_FIELDS = (
    "summary", "work_done", "current_state", "next_steps", "blockers",
)
_CHECKPOINT_PAYLOAD_LIST_FIELDS = ("files_changed", "commands_run", "verification")
_CHECKPOINT_PAYLOAD_STATUS_VALUES = ("active", "paused", "completed", "blocked")
_CHECKPOINT_PAYLOAD_FIELDS = frozenset({
    *_CHECKPOINT_PAYLOAD_TEXT_FIELDS,
    *_CHECKPOINT_PAYLOAD_LIST_FIELDS,
    "metadata", "pinned", "status",
})


def _validate_checkpoint_payload(payload: dict[str, Any]) -> None:
    """Reject malformed ``checkpoint --payload`` input before any session write.

    CLI flags are constrained by argparse, but a JSON payload file bypasses
    those ``choices``/``append`` validators. Keeping the validation at this
    boundary prevents an invalid payload from committing an auto-started
    session and protects downstream renderers from non-string evidence.
    """
    unknown = sorted(set(payload) - _CHECKPOINT_PAYLOAD_FIELDS)
    if unknown:
        raise ValueError(f"checkpoint payload has unsupported field(s): {', '.join(unknown)}")
    for key in _CHECKPOINT_PAYLOAD_TEXT_FIELDS:
        if key in payload and not isinstance(payload[key], str):
            raise ValueError(f"checkpoint payload.{key} must be a string")
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary:
        raise ValueError("checkpoint requires --summary or payload.summary")
    if "status" in payload and payload["status"] not in _CHECKPOINT_PAYLOAD_STATUS_VALUES:
        raise ValueError(
            "checkpoint payload.status must be one of: "
            + ", ".join(_CHECKPOINT_PAYLOAD_STATUS_VALUES)
        )
    for key in _CHECKPOINT_PAYLOAD_LIST_FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"checkpoint payload.{key} must be an array of strings")
    if "metadata" in payload and not isinstance(payload["metadata"], dict):
        raise ValueError("checkpoint payload.metadata must be an object")
    if "pinned" in payload and not isinstance(payload["pinned"], bool):
        raise ValueError("checkpoint payload.pinned must be a boolean")


def load_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if args.payload:
        loaded = json.loads(Path(args.payload).read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("checkpoint payload must be a JSON object")
        payload.update(loaded)
    for key in ("summary", "work_done", "current_state", "next_steps", "blockers", "status"):
        value = getattr(args, key, None)
        if value is not None:
            payload[key] = value
    for key in ("files_changed", "commands_run", "verification"):
        values = getattr(args, key, None)
        if values:
            payload[key] = values
    if getattr(args, "pin", False):
        # Explicit CLI flags override the JSON payload. Apply --pin before
        # the pre-auto-files validation below so a malformed payload.pinned
        # cannot reject the valid explicit override.
        payload["pinned"] = True
    if getattr(args, "auto_files", False):
        project = getattr(args, "project", None)
        if not project:
            raise ValueError("--auto-files requires --project (used to scope the git status lookup)")
        # Validate before list expansion: otherwise a string payload is
        # iterable and would silently turn into a list of characters.
        _validate_checkpoint_payload(payload)
        detected = auto_detect_changed_files(project)
        payload["files_changed"] = list(dict.fromkeys([*payload.get("files_changed", []), *detected]))
    _validate_checkpoint_payload(payload)
    return payload


def resolve_record_content(content: str | None, content_file: str | None) -> str:
    """Resolve record-add's body text from --content or --content-file.

    Long/multi-line/Thai record content is exactly what breaks under shell
    quoting when forced through a single --content string; --content-file
    (or '-' for stdin) lets a caller pass a real file instead.
    """
    if content and content_file:
        raise ValueError("--content and --content-file are mutually exclusive")
    if content_file:
        if content_file == "-":
            return sys.stdin.read()
        return Path(content_file).read_text(encoding="utf-8")
    if not content:
        raise ValueError("record-add requires --content or --content-file")
    return content


def fts_index_identity(
    conn: sqlite3.Connection, content_table: str, id_column: str, vocab_table: str,
    *, fts_table: str | None = None, match_expression: str | None = None,
) -> dict[str, Any]:
    """Compare real FTS index doc IDs, not external-content shadow rows."""
    content_ids = {
        row[0] for row in conn.execute(f'SELECT "{id_column}" FROM "{content_table}"')
    }
    if fts_table and match_expression and table_exists(conn, fts_table):
        indexed_ids = {
            row[0] for row in conn.execute(
                f'SELECT rowid FROM "{fts_table}" WHERE "{fts_table}" MATCH ?',
                (match_expression,),
            )
        }
    elif table_exists(conn, vocab_table):
        indexed_ids = {row[0] for row in conn.execute(f'SELECT DISTINCT doc FROM "{vocab_table}"')}
    else:
        return {"missing": 0, "extra": 0, "indexed": None, "verified": False}
    return {
        "missing": len(content_ids - indexed_ids),
        "extra": len(indexed_ids - content_ids),
        "indexed": len(indexed_ids),
        "verified": True,
    }


def print_search(results: list[dict[str, Any]]) -> None:
    if not results:
        print("No knowledge found.")
        return
    for index, item in enumerate(results, start=1):
        excerpt = re.sub(r"\s+", " ", item["excerpt"]).strip()
        labels = [item["project"], item["category"], item.get("status", "")]
        if item.get("bug_id"):
            labels.append(item["bug_id"])
        stale_marker = " [STALE — source file changed since indexing]" if item.get("stale") else ""
        print(f"[{index}] {item['title']} ({' / '.join(label for label in labels if label)}){stale_marker}")
        location = item["source_path"]
        if item.get("line_start") is not None:
            location += f":{item['line_start']}-{item['line_end']}"
        elif item.get("source_heading"):
            location += f":{item['source_heading']}"
        print(f"    {location}")
        print(f"    matched: {', '.join(item.get('match_reasons', []))}")
        print(f"    {excerpt}")


def activity_export_path(db_path: Path) -> Path:
    return _activity_export_path(db_path)


def render_activity(conn: sqlite3.Connection, limit: int = ACTIVITY_EXPORT_LIMIT) -> str:
    return _render_activity_text(conn, limit, export_max=ACTIVITY_EXPORT_MAX)


def activity_line(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Format one activity_log row the same way render_activity does, for
    reuse by `activity --follow`'s poll loop."""
    return _activity_line_text(conn, row)


def poll_activity_since(
    conn: sqlite3.Connection, after_id: int, project: str | None = None, limit: int = 200,
) -> list[sqlite3.Row]:
    """New activity_log rows with id > after_id, oldest first — the single
    query `activity --follow` re-runs on each poll tick."""
    return _poll_activity_since(conn, after_id, project, limit)


def prune_activity_log(conn: sqlite3.Connection, keep: int | None = None) -> int:
    """Keep only the newest `keep` activity_log rows (module default
    MAX_ACTIVITY_LOG_ROWS, re-read at call time so tests can monkeypatch it
    the same way test_checkpoint_retention_* does for MAX_CHECKPOINTS — a
    `keep: int = MAX_ACTIVITY_LOG_ROWS` default would instead freeze the
    value at function-definition time)."""
    if keep is None:
        keep = MAX_ACTIVITY_LOG_ROWS
    return _prune_activity_log_rows(conn, keep)


def refresh_activity_export(
    conn: sqlite3.Connection, db_path: Path, limit: int = ACTIVITY_EXPORT_LIMIT
) -> Path | None:
    """Best-effort: a failed export must never fail the write that triggered it.

    Calls the module-level names above (not activity.py's directly) so a test
    patching endeavor_db.render_activity/prune_activity_log/activity_export_path
    still controls this function's real behavior.
    """
    try:
        prune_activity_log(conn)
        target = activity_export_path(db_path)
        target.write_text(render_activity(conn, limit), encoding="utf-8")
        return target
    except Exception as exc:
        print(f"warning: could not refresh {ACTIVITY_EXPORT_NAME}: {exc}", file=sys.stderr)
        return None


AGENT_HELP_TEXT = """\
ENDMEMEX — agent cheat sheet (README.md = overview; ENDMEMEX_USER_MANUAL.md = full reference)

Start of session (one call — handoff + embed backfill + doc freshness + hooks):
  python3 ENDMEMEX/endeavor_db.py bootstrap --project <PROJECT> --json

Broader session briefing (handoff + open records + recent knowledge/activity,
capped to a char budget — use when bootstrap's handoff alone isn't enough
context):
  python3 ENDMEMEX/endeavor_db.py pack --project <PROJECT> --json

Search before rediscovering know-how (default JSON is verbose; --compact
trims it to what you read when browsing; --check-stale flags results whose
source file has drifted from the index):
  python3 ENDMEMEX/endeavor_db.py query "<question>" --project <PROJECT> --compact --json
  python3 ENDMEMEX/endeavor_db.py query "<question>" --check-stale --json

Checkpoint after every material phase (collapses session-start+checkpoint
into one call via --project/--goal on the first checkpoint of a task;
--auto-files appends git-status-detected files under ROOT/<PROJECT>/ — opt-in,
only fires when PROJECT is also a real top-level directory):
  python3 ENDMEMEX/endeavor_db.py checkpoint \\
    --project <PROJECT> --goal "<goal>" --agent claude|codex \\
    --summary "<what happened>" --status active|paused|blocked|completed \\
    --auto-files --next-steps "<exact continuation>"

Pin a checkpoint so the sliding-window prune never deletes it (--pin at
creation, or retroactively by id; unpin returns it to normal pruning):
  python3 ENDMEMEX/endeavor_db.py checkpoint ... --pin
  python3 ENDMEMEX/endeavor_db.py pin-checkpoint <checkpoint_id> --agent claude|codex
  python3 ENDMEMEX/endeavor_db.py unpin-checkpoint <checkpoint_id> --agent claude|codex

Resume:
  python3 ENDMEMEX/endeavor_db.py handoff --project <PROJECT> --json

List every paused task before choosing a project:
  python3 ENDMEMEX/endeavor_db.py handoff --all-paused --json

Discover all pending work (presence, resumable/blocked sessions, and
lifecycle-aware durable records; does not replace handoff selection):
  python3 ENDMEMEX/endeavor_db.py pending --all-projects --json
  python3 ENDMEMEX/endeavor_db.py pending --project <PROJECT> --json

Durable audit -> fix -> verification lifecycle (stable IDs, typed edges):
  python3 ENDMEMEX/endeavor_db.py record-add --id AUDIT-X-001 \\
    --project <PROJECT> --type audit --title "..." --content "..." --agent claude
  python3 ENDMEMEX/endeavor_db.py record-add --id FIX-X-001 \\
    --project <PROJECT> --type fix --title "..." --content "..." \\
    --link resolves:AUDIT-X-001 --agent claude
  python3 ENDMEMEX/endeavor_db.py record-show AUDIT-X-001 --depth 3

Keep tracked Markdown current after editing a PROJECT_MEMORY.md/plan/audit:
  python3 ENDMEMEX/sync_tracked.py <path>

Health check (never spawns anything, safe anytime):
  python3 ENDMEMEX/endeavor_db.py doctor

One-command read-only project preflight (machine role, DB, embeddings, ANN,
tracked-document freshness, and ordered next actions):
  python3 ENDMEMEX/endeavor_db.py readiness --project <PROJECT>

See who else is working (same-machine real-time, other machines via a
per-machine sidecar file -- never a shared-table write, see
ENDMEMEX_USER_MANUAL.md §Agent Presence for why):
  python3 ENDMEMEX/endeavor_db.py presence-start --agent claude|codex \\
    --project <PROJECT> --task "<short description>"
  python3 ENDMEMEX/endeavor_db.py presence --json
  python3 ENDMEMEX/endeavor_db.py presence-stop

Check when the other Mac last wrote
(informational only, never write authorization or a guarantee sync caught up -- see
ENDMEMEX_USER_MANUAL.md §Sync Freshness Signal):
  python3 ENDMEMEX/endeavor_db.py sync-status --json

Delegate a one-shot task to the other agent (Claude <-> Codex sub-agent;
child starts cold — put context in the prompt; nested delegation refused):
  python3 ENDMEMEX/agent_delegate.py codex  "<task>"   # from Claude
  python3 ENDMEMEX/agent_delegate.py claude "<task>" --model sonnet  # from Codex
  (full guide: agent_delegate.py docstring / ENDMEMEX_USER_MANUAL.md §Cross-Agent Delegation)

Rules that don't show up in --help: never store secrets/tokens in a
checkpoint or record; use `paused` (not `completed`) until verified. Keep a
writable SQLite database local to one host; for a remote-writer deployment,
use the authenticated write gateway rather than a shared filesystem.
"""


def build_parser() -> argparse.ArgumentParser:
    return _build_parser_impl(__doc__)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "agent-help":
        print(AGENT_HELP_TEXT, end="")
        return 0
    if args.command == "embed-diagnose":
        print(json_text(embedding_diagnostics()))
        return 0
    if args.command == "install-hooks":
        try:
            print(json_text(install_hooks()))
            return 0
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    path = database_path(args.db)
    read_only_commands = {
        "query", "stats", "doctor", "evaluate", "embed-status", "embed-warm", "embed-cool",
        "readiness", "ann-status", "ann-build",
        "record-show", "record-search", "handoff", "activity", "pack", "pending", "presence", "sync-status",
        "timeline", "event-poll",
    }
    read_only = args.command in read_only_commands
    if args.command == "readiness" and not path.is_file():
        report = unavailable_readiness_report(args.project, path, "database file does not exist")
        print(json_text(report) if args.json else render_readiness(report))
        return 0
    conn: sqlite3.Connection | None = None
    changes_before = 0
    try:
        conn = connect(path, read_only=read_only)
        changes_before = conn.total_changes
        if not read_only:
            initialize(conn, force=args.command == "init")
        if args.command in {"record-show", "record-search"} and not table_exists(conn, "memory_records"):
            raise ValueError("SQLite-native memory schema is unavailable; run the init command first")
        if args.command == "init":
            print(json_text({"database": str(path), "schema_version": SCHEMA_VERSION}))
        elif args.command == "seed":
            print(json_text(seed(conn)))
        elif args.command == "ingest":
            print(json_text(ingest_markdown(conn, Path(args.source), args.project, args.kind, embed=args.embed)))
        elif args.command == "query":
            results = search_all(
                conn, args.text, args.project, args.category, max(1, min(args.limit, 50)),
                status=args.knowledge_status, module=args.module, bug_id=args.bug_id, session_label=args.session_label,
                semantic=args.semantic,
            )
            if args.check_stale and isinstance(results, list) and all(
                isinstance(item, dict) for item in results
            ):
                annotate_staleness(conn, results)
            if args.json:
                print(json_text([compact_result(r) for r in results] if args.compact else results))
            else:
                print_search(results)
        elif args.command == "stats":
            counts = {
                "database": str(path),
                "schema_version": database_schema_version(conn),
                "schema_current": database_schema_version(conn) == SCHEMA_VERSION,
                "documents": table_count(conn, "documents"),
                "knowledge": table_count(conn, "knowledge"),
                "sessions": table_count(conn, "sessions"),
                "checkpoints": table_count(conn, "checkpoints"),
                "checkpoints_pinned": conn.execute(
                    "SELECT COUNT(*) FROM checkpoints WHERE pinned = 1"
                ).fetchone()[0] if table_exists(conn, "checkpoints") else 0,
                "checkpoints_pinned_warn_threshold": MAX_PINNED_CHECKPOINTS_WARN,
                "checkpoints_pin_warning": (
                    pinned_checkpoint_warning(conn) if table_exists(conn, "checkpoints") else None
                ),
                "checkpoints_cap_total": MAX_TOTAL_CHECKPOINTS,
                "checkpoints_cap_per_session": MAX_CHECKPOINTS,
                "agent_presence_active": conn.execute(
                    "SELECT COUNT(*) FROM agent_presence WHERE status = 'active'"
                ).fetchone()[0] if table_exists(conn, "agent_presence") else 0,
                "activity_log": table_count(conn, "activity_log"),
                "activity_log_cap": MAX_ACTIVITY_LOG_ROWS,
                "memory_records": table_count(conn, "memory_records"),
                "memory_relations": table_count(conn, "memory_relations"),
                "embedded": embedding_stats(conn)["embedded"],
            }
            print(json_text(counts))
        elif args.command == "embed-status":
            status = embedding_stats(conn)
            status["diagnostics"] = embedding_diagnostics()
            print(json_text(status))
        elif args.command == "readiness":
            report = readiness_report(conn, args.project, path)
            print(json_text(report) if args.json else render_readiness(report))
        elif args.command == "ann-status":
            print(json_text(ann_helper(conn, "status")))
        elif args.command == "ann-build":
            result = ann_helper(conn, "build")
            print(json_text(result))
            return 0 if result.get("status") == "ready" else 1
        elif args.command == "embed-warm":
            print(json_text(set_embed_keep_warm(args.keep_alive)))
        elif args.command == "embed-cool":
            print(json_text(set_embed_keep_warm(False)))
        elif args.command == "embed-backfill":
            print(json_text(backfill_embeddings(conn, args.batch_size)))
        elif args.command == "bootstrap":
            data = bootstrap(
                conn, args.project, args.batch_size, args.include_pending, session_id=args.session,
            )
            print(json_text(data) if args.json else json.dumps(data, ensure_ascii=False, indent=2))
        elif args.command == "pack":
            data = build_pack(conn, args.project, args.budget_chars, session_id=args.session)
            print(json_text(data) if args.json else json.dumps(data, ensure_ascii=False, indent=2))
        elif args.command == "pending":
            data = build_pending_worklist(conn, args.project)
            print(json_text(data) if args.json else json.dumps(data, ensure_ascii=False, indent=2))
        elif args.command == "maintenance":
            if not args.yes:
                raise ValueError(
                    "maintenance rewrites the whole database file and briefly blocks all "
                    "writers — pass --yes to confirm no other agent process is writing right now"
                )
            size_before = path.stat().st_size
            conn.execute("PRAGMA optimize")
            conn.execute("VACUUM")
            size_after = path.stat().st_size
            print(json_text({
                "database": str(path), "size_before": size_before, "size_after": size_after,
                "reclaimed": size_before - size_after,
            }))
        elif args.command == "activity":
            if args.follow:
                last_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM activity_log").fetchone()[0]
                print(f"# watching activity_log (project={args.project or 'all'}, "
                      f"interval={args.interval}s) — Ctrl-C to stop", file=sys.stderr)
                try:
                    while True:
                        for row in poll_activity_since(conn, last_id, args.project):
                            print(activity_line(conn, row))
                            sys.stdout.flush()
                            last_id = row["id"]
                        time.sleep(args.interval)
                except KeyboardInterrupt:
                    pass
            elif args.stdout:
                print(render_activity(conn, args.limit), end="")
            else:
                target = activity_export_path(path)
                target.write_text(render_activity(conn, args.limit), encoding="utf-8")
                print(json_text({"path": str(target), "limit": min(args.limit, ACTIVITY_EXPORT_MAX)}))
        elif args.command == "doctor":
            report = doctor_report(conn, path)
            print(json_text(report))
            return 0 if report["ok"] else 1
        elif args.command == "evaluate":
            if args.pipeline == "both":
                report = {
                    "pipelines": {
                        name: evaluate_queries(
                            conn, Path(args.file), max(1, min(args.limit, 20)),
                            semantic=args.semantic, pipeline=name,
                        )
                        for name in ("markdown", "unified")
                    }
                }
            else:
                report = evaluate_queries(
                    conn, Path(args.file), max(1, min(args.limit, 20)),
                    semantic=args.semantic, pipeline=args.pipeline,
                )
            print(json_text(report) if args.json else json.dumps(report, ensure_ascii=False, indent=2))
        elif args.command == "feedback":
            feedback_id = record_feedback(
                conn, args.agent, args.query, args.selected_ids,
                {"yes": True, "no": False}.get(args.useful), args.note,
            )
            print(json_text({"feedback_id": feedback_id}))
        elif args.command == "event-add":
            payload = json.loads(args.payload)
            print(json_text(publish_event(
                conn, args.event_type, args.project, args.subject_id,
                payload, args.dedupe_key, args.agent,
            )))
        elif args.command == "event-poll":
            events = poll_events(
                conn, args.after, args.project, args.limit, args.include_acked,
            )
            print(json_text({"events": events, "count": len(events)}))
        elif args.command == "event-ack":
            print(json_text(acknowledge_event(conn, args.event_id, args.agent)))
        elif args.command == "record-add":
            metadata = json.loads(args.metadata)
            if not isinstance(metadata, dict):
                raise ValueError("--metadata must be a JSON object")
            if args.source:
                metadata["source"] = args.source
            content = resolve_record_content(args.content, args.content_file)
            links = [(*parse_relation_spec(spec), args.note) for spec in args.link]
            record_id = create_memory_record(
                conn, args.record_id, args.project, args.record_type, args.title, content,
                args.status, args.agent, metadata, links, args.action_state,
            )
            print(json_text(memory_record_context(conn, record_id, depth=1)))
        elif args.command == "record-update":
            metadata = None
            if args.metadata is not None:
                metadata = json.loads(args.metadata)
                if not isinstance(metadata, dict):
                    raise ValueError("--metadata must be a JSON object")
            print(json_text(update_memory_record(
                conn, args.record_id, args.agent, project=args.project, record_type=args.record_type,
                title=args.title, content=args.content, status=args.status,
                action_state=args.action_state, metadata=metadata,
            )))
        elif args.command == "record-link":
            add_memory_relation(
                conn, args.source_id, args.relation, args.target_id, args.note, args.agent
            )
            print(json_text(memory_record_context(conn, args.source_id, depth=1)))
        elif args.command == "record-show":
            print(json_text(memory_record_context(
                conn, args.record_id, args.depth,
                max(1, min(args.max_records, MAX_MEMORY_CONTEXT_RECORDS)),
            )))
        elif args.command == "record-search":
            print(json_text(search_memory_records(
                conn, args.text, args.project, args.record_type,
                max(1, min(args.limit, 50)), args.current_only,
            )))
        elif args.command == "session-start":
            print(start_session(conn, args.project, args.goal, args.agent, {}))
        elif args.command == "checkpoint":
            # Validate the payload before resolving: the --project/--goal path
            # commits an auto-started session, and a payload error after that
            # commit (e.g. missing --summary) would strand an empty session.
            payload = load_payload(args)
            session = resolve_or_start_checkpoint_session(conn, args.session, args.project, args.goal, args.agent)
            checkpoint_id = add_checkpoint(conn, session, args.agent, payload)
            result = {"checkpoint_id": checkpoint_id, "session_id": session["id"]}
            if payload.get("pinned"):
                warning = pinned_checkpoint_warning(conn)
                if warning is not None:
                    result["pin_warning"] = warning
            print(json_text(result))
        elif args.command == "pin-checkpoint":
            print(json_text(set_checkpoint_pinned(conn, args.checkpoint_id, True, args.agent)))
        elif args.command == "unpin-checkpoint":
            print(json_text(set_checkpoint_pinned(conn, args.checkpoint_id, False, args.agent)))
        elif args.command == "handoff":
            if args.all_paused:
                if args.session or args.project:
                    raise ValueError("--all-paused cannot be combined with --session or --project")
                data = {"handoffs": paused_handoffs(conn)}
            else:
                try:
                    data = handoff(conn, args.session, args.project)
                except SessionNotFoundError:
                    # "No resumable session" is the normal starting state of almost
                    # every new task, not a failure. Machine callers (--json) get
                    # parseable nulls and exit 0 instead of having to pattern-match
                    # a benign stderr message. An explicit --session that fails to
                    # resolve stays a hard error (a typo, not an empty state), and
                    # the human-readable form keeps the explicit error too.
                    if not args.json or args.session:
                        raise
                    data = {"session": None, "checkpoint": None}
            print(json_text(data) if args.json else json.dumps(data, ensure_ascii=False, indent=2))
        elif args.command == "timeline":
            data = checkpoint_timeline(
                conn, project=args.project, agent=args.agent, session_status=args.status,
                session_id=args.session, limit=args.limit, oldest_first=args.oldest_first,
                per_session_cap=MAX_CHECKPOINTS, total_cap=MAX_TOTAL_CHECKPOINTS,
            )
            if args.json:
                print(json_text(data))
            else:
                print(render_checkpoint_timeline(data, root=ROOT), end="")
        elif args.command == "session-close":
            session = resolve_session(conn, args.session, args.project)
            timestamp = now_utc()
            with conn:
                conn.execute(
                    "UPDATE sessions SET status = ?, last_agent = ?, updated_at = ? WHERE id = ?",
                    (args.status, args.agent, timestamp, session["id"]),
                )
                conn.execute(
                    "INSERT INTO activity_log(agent, action, project, detail, created_at) VALUES(?, 'session_close', ?, ?, ?)",
                    (
                        args.agent, session["project"],
                        json_text({"session_id": session["id"], "status": args.status}), timestamp,
                    ),
                )
            print(json_text({"session_id": session["id"], "status": args.status}))
        elif args.command == "presence-start":
            print(json_text(presence_start(
                conn, local_machine(), args.agent, args.project, args.task,
                pid=args.pid or os.getpid(), instance=args.instance, session_id=args.session,
            )))
        elif args.command == "presence-heartbeat":
            updated = presence_heartbeat(
                conn, local_machine(), args.agent, args.project,
                pid=args.pid or os.getpid(), task=args.task, instance=args.instance,
            )
            print(json_text({"updated": bool(updated)}))
        elif args.command == "presence-stop":
            updated = presence_stop(conn, local_machine(), args.agent, args.project, instance=args.instance)
            print(json_text({"updated": bool(updated)}))
        elif args.command == "presence":
            data = list_presence(conn, args.project)
            print(json_text(data) if args.json else json.dumps(data, ensure_ascii=False, indent=2))
        elif args.command == "sync-status":
            data = sync_freshness_report(local_machine())
            print(json_text(data) if args.json else json.dumps(data, ensure_ascii=False, indent=2))
        if not read_only and conn.total_changes > changes_before:
            # Keep the human-readable digest current after every write —
            # best-effort, never fails the command that triggered it.
            refresh_activity_export(conn, path)
            # Informational freshness signal for the OTHER Mac's next confirm
            # decision (see write_sync_freshness_signal docstring) — never
            # gates or blocks this write itself.
            write_sync_freshness_signal(local_machine(), args.command)
        return 0
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
        if args.command == "readiness" and conn is None:
            report = unavailable_readiness_report(args.project, path, str(exc))
            print(json_text(report) if args.json else render_readiness(report))
            return 0
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
