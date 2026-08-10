"""Phase 0 compatibility contract for ENDMEMEX's two stable entrypoints."""
from __future__ import annotations

import inspect
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ENDMEMEX = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENDMEMEX))

import agent_delegate  # noqa: E402
import agent_mcp_server  # noqa: E402
import config  # noqa: E402
import endeavor_db  # noqa: E402
import mcp_server  # noqa: E402

ROOT = config.ROOT
# Runtime constants use config.ROOT. Parser snapshots instead normalize to the
# package parent so the nested Main checkout and standalone Public checkout
# serialize the same default paths.
CONTRACT_ROOT = ENDMEMEX.parent


DB_FUNCTIONS = (
    "AmbiguousSessionError", "SessionNotFoundError", "_embed_health",
    "_embed_health_probe", "_embed_knowledge_rows_result", "_embed_python_candidates",
    "_reap_stale_sidecar_temps", "_spawn_embed_server", "_terminal_current_ids",
    "_tracked_docs_summary", "_typo_corrected_terms", "_write_presence_sidecar",
    "activity_export_path", "activity_line", "add_checkpoint", "add_memory_relation",
    "acknowledge_event", "ann_candidate_ids", "ann_helper",
    "annotate_staleness", "auto_detect_changed_files", "backfill_embeddings", "bootstrap",
    "build_pack", "build_parser", "build_pending_worklist", "checkpoint_timeline",
    "compact_result", "connect",
    "create_memory_record", "database_path", "database_schema_version",
    "delete_documents_by_source_paths", "embed_companion_ready", "embed_failure_reason",
    "embed_knowledge_rows", "embed_texts", "embedding_diagnostics", "embedding_hash",
    "embedding_stats", "ensure_embed_server", "evaluate_queries", "extract_metadata",
    "handoff", "hook_status", "hooks_ok", "ingest_markdown", "initialize", "install_hooks",
    "json_text", "list_presence", "load_payload", "local_machine", "main", "markdown_chunks",
    "memory_record_context", "memory_relation_health", "now_utc", "pack_embedding",
    "paused_handoffs", "pinned_checkpoint_warning", "poll_activity_since", "presence_heartbeat",
    "poll_events", "presence_start", "presence_stop", "prune_activity_log", "prune_checkpoints_globally",
    "prune_documents", "record_feedback", "refresh_activity_export", "render_activity",
    "render_checkpoint_timeline",
    "readiness_report", "render_readiness", "resolve_embed_python", "resolve_or_start_checkpoint_session", "resolve_record_content",
    "resolve_session", "search", "search_all", "search_memory_records", "semantic_memory_records",
    "set_checkpoint_pinned", "set_embed_keep_warm", "start_session", "sync_freshness_report", "doctor_report",
    "publish_event", "table_count", "table_exists", "unpack_embedding", "update_memory_record",
    "write_sync_freshness_signal", "seed", "fts_index_identity",
)

DELEGATE_FUNCTIONS = (
    "_config_from_args", "_group_has_other_members", "_group_member_pids",
    "_process_exists", "_process_matches_run", "_process_start_token", "_read_json",
    "_read_output", "_sha256", "advisor_main", "append_log", "build_command",
    "build_delegate_parser", "cancel_main", "diagnose_main", "effective_model",
    "effective_reasoning_effort", "execute_managed", "infer_caller", "main", "new_run",
    "parse_depth", "positive_int", "progress_snapshot", "prune_old_runs", "read_run_state",
    "run_child_to_files", "start_background", "status_main", "update_run_state",
    "update_run_state_if", "wait_main", "wait_retry_or_cancel", "worker_main", "guard_main",
    "find_binary", "_binary_version",
)

# These are the signatures most likely to be consumed by extracted modules and
# callers. The complete presence manifest above catches accidental disappearance
# of the remaining façade functions.
SIGNATURES = {
    ("endeavor_db", "database_path"): "(value: 'str | None' = None) -> 'Path'",
    ("endeavor_db", "connect"): "(path: 'Path', *, read_only: 'bool' = False) -> 'sqlite3.Connection'",
    ("endeavor_db", "table_count"): "(conn: 'sqlite3.Connection', name: 'str') -> 'int'",
    ("endeavor_db", "execute_sql_script"): "(conn: 'sqlite3.Connection', script: 'str') -> 'None'",
    ("endeavor_db", "initialize"): "(conn: 'sqlite3.Connection', *, force: 'bool' = False) -> 'None'",
    ("endeavor_db", "search"): "(conn: 'sqlite3.Connection', query: 'str', project: 'str | None', category: 'str | None', limit: 'int', *, status: 'str | None' = None, module: 'str | None' = None, bug_id: 'str | None' = None, session_label: 'str | None' = None, semantic: 'str' = 'auto') -> 'list[dict[str, Any]]'",
    ("endeavor_db", "resolve_or_start_checkpoint_session"): "(conn: 'sqlite3.Connection', session_id: 'str | None', project: 'str | None', goal: 'str | None', agent: 'str') -> 'sqlite3.Row'",
    ("endeavor_db", "embedding_stats"): "(conn: 'sqlite3.Connection') -> 'dict[str, Any]'",
    ("endeavor_db", "delete_documents_by_source_paths"): "(conn: 'sqlite3.Connection', source_paths: 'Iterable[str]') -> 'int'",
    ("endeavor_db", "auto_detect_changed_files"): "(project: 'str') -> 'list[str]'",
    ("endeavor_db", "main"): "(argv: 'list[str] | None' = None) -> 'int'",
    ("agent_delegate", "build_command"): "(args: 'argparse.Namespace', binary: 'str') -> 'list[str]'",
    ("agent_delegate", "new_run"): "(config: 'dict', run_id: 'str | None' = None) -> 'tuple[str, Path]'",
    ("agent_delegate", "update_run_state"): "(directory: 'Path', **changes: 'object') -> 'dict'",
    ("agent_delegate", "build_delegate_parser"): "() -> 'argparse.ArgumentParser'",
    ("agent_delegate", "main"): "() -> 'int'",
    ("agent_delegate", "_process_start_token"): "(pid: 'object') -> 'str | None'",
    ("agent_delegate", "run_child_to_files"): "(cmd: 'list[str]', cwd: 'str', env: 'dict', timeout: 'int', stdout_path: 'Path', stderr_path: 'Path', on_started=None, should_cancel=None) -> 'tuple[int, int | None]'",
    ("agent_delegate", "find_binary"): "(name: 'str') -> 'str | None'",
    ("agent_delegate", "_binary_version"): "(binary: 'str') -> 'str | None'",
    ("agent_delegate", "append_log"): "(entry: 'dict') -> 'bool'",
    ("agent_delegate", "execute_managed"): "(args: 'argparse.Namespace', run_id: 'str', directory: 'Path') -> 'dict'",
}

CONSTANTS = {
    ("endeavor_db", "SCHEMA_VERSION"): "12",
    # v7: fence-aware chunking + status-context gating changed chunk
    # boundaries and stored status, so tracked docs must be re-ingested.
    ("endeavor_db", "INDEX_VERSION"): "7",
    ("endeavor_db", "MAX_CHUNK_CHARS"): 500,
    ("endeavor_db", "SEMANTIC_FULL_SCAN_LIMIT"): 20_000,
    ("endeavor_db", "SEMANTIC_CHUNKS_PER_RECORD_LIMIT"): 20,
    ("endeavor_db", "ROOT"): ROOT,
    ("endeavor_db", "DEFAULT_DB"): ENDMEMEX / "endeavor_memory.sqlite3",
    ("endeavor_db", "PRESENCE_DIR"): ENDMEMEX / ".presence",
    ("endeavor_db", "SYNC_FRESHNESS_DIR"): ENDMEMEX / ".sync_freshness",
    ("endeavor_db", "GIT_HOOK_SOURCES"): {"pre-commit": ENDMEMEX / "hooks" / "pre-commit"},
    ("endeavor_db", "EMBED_DIM"): 384,
    ("endeavor_db", "EMBED_LOG_PATH"): ENDMEMEX / "embed_server.log",
    ("endeavor_db", "EMBED_START_LOCK_PATH"): ENDMEMEX / ".embed_start.lock",
    ("endeavor_db", "EMBED_MODEL_NAME"): "paraphrase-multilingual-MiniLM-L12-v2",
    ("endeavor_db", "EMBED_PYTHON_ENV"): "ENDEAVOR_EMBED_PYTHON",
    ("endeavor_db", "MAX_CHECKPOINTS"): 500,
    ("endeavor_db", "MAX_TOTAL_CHECKPOINTS"): 10_000,
    ("endeavor_db", "MAX_ACTIVITY_LOG_ROWS"): 2_000,
    ("endeavor_db", "MAX_PINNED_CHECKPOINTS_WARN"): 1_000,
    ("endeavor_db", "PRESENCE_STALE_SEC"): 1800,
    ("endeavor_db", "PRESENCE_ROW_MAX_AGE_DAYS"): 3,
    ("endeavor_db", "SIDECAR_TEMP_MAX_AGE_SEC"): 600,
    ("endeavor_db", "EMBED_HEALTH_TIMEOUT_SEC"): 0.3,
    ("endeavor_db", "EMBED_STARTUP_TIMEOUT_SEC"): 30,
    ("endeavor_db", "EMBED_REQUEST_TIMEOUT_SEC"): 60,
    ("endeavor_db", "EMBED_BATCH_SIZE"): 128,
    ("agent_delegate", "ROOT"): ROOT,
    ("agent_delegate", "DB_SCRIPT"): ENDMEMEX / "endeavor_db.py",
    ("agent_delegate", "MAX_DEPTH"): 1,
    ("agent_delegate", "EXIT_ARTIFACT_LIMIT"): 122,
    ("agent_delegate", "EXIT_REAP_FAILED"): 123,
    ("agent_delegate", "CHECKPOINT_TIMEOUT_S"): 60,
    ("agent_delegate", "POST_KILL_DRAIN_S"): 5,
    ("agent_delegate", "GUARD_DRAIN_GRACE_S"): 5.0,
    ("agent_delegate", "CANCEL_GRACE_S"): 2.0,
    ("agent_delegate", "MANAGER_HEARTBEAT_S"): 5.0,
    ("agent_delegate", "MANAGER_STALE_S"): 20.0,
    ("agent_delegate", "START_PUBLICATION_GRACE_S"): 20.0,
    ("agent_delegate", "MAX_VALIDATE_BYTES"): 2 * 1024 * 1024,
    ("agent_delegate", "MAX_ARTIFACT_BYTES_PER_STREAM"): 4 * 1024 * 1024,
    ("agent_delegate", "DEPTH_ENV"): "ENDEAVOR_DELEGATE_DEPTH",
    ("agent_delegate", "CALLER_ENV"): "ENDEAVOR_DELEGATE_AS",
    ("agent_delegate", "LOG_PROMPT_CHARS"): 500,
    ("agent_delegate", "LOG_OUTPUT_CHARS"): 2000,
    ("agent_delegate", "LOG_STDERR_CHARS"): 500,
    ("agent_delegate", "RUN_DIR_MAX_AGE_DAYS"): 14,
    ("agent_delegate", "TERMINAL_STATUSES"): {"completed", "failed", "timed_out", "cancelled"},
}

AGENT_MCP_CONSTANTS = {
    "CONTROL_TIMEOUT_S": 15, "MAX_CONCURRENT_RUNS": 4,
    "MAX_STARTS_PER_WINDOW": 6, "START_WINDOW_S": 60,
}

GOLDEN_FIXTURE = Path(__file__).with_name("phase0_golden_contract.json")
_GOLDEN = json.loads(GOLDEN_FIXTURE.read_text(encoding="utf-8"))
PARSER_GOLDEN_SHA256 = _GOLDEN["endeavor_db_parser_sha256"]
PARSER_ERROR_CHOICE_BY_MINOR = _GOLDEN["parser_error_choice_by_python_minor"]
MCP_GOLDEN_SHA256 = _GOLDEN["mcp_contract_sha256"]
MCP_INSTRUCTIONS_SHA256 = _GOLDEN["mcp_instructions_sha256"]
AGENT_MCP_GOLDEN_SHA256 = _GOLDEN["agent_mcp_golden_sha256"]
AGENT_DELEGATE_GOLDEN_BY_MINOR = _GOLDEN["agent_delegate_contract_sha256_by_python_minor"]


def _stable(value):
    """Return parser/schema data without machine-specific values."""
    if isinstance(value, Path):
        try:
            return "<ROOT>/" + value.relative_to(CONTRACT_ROOT).as_posix()
        except ValueError:
            resolved = value.resolve()
            # A truncated digest plus basename silently collides for equal
            # basenames in different directories.  Keep the canonical path
            # token explicit so snapshots remain deterministic and injective.
            return f"<EXTERNAL:{resolved.as_posix()}>"
    if isinstance(value, str):
        root = str(CONTRACT_ROOT)
        if value == root:
            return "<ROOT>"
        prefix = root + os.sep
        if value.startswith(prefix):
            return "<ROOT>/" + value[len(prefix):].replace(os.sep, "/")
    if isinstance(value, dict):
        return {str(key): _stable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stable(item) for item in value]
    if isinstance(value, set):
        return sorted(_stable(item) for item in value)
    if value is argparse.SUPPRESS:
        return "<SUPPRESS>"
    if callable(value):
        return f"{value.__module__}.{value.__qualname__}"
    return value


def _parser_snapshot(parser, stable=_stable):
    actions = []
    subparsers = None
    for action in parser._actions:
        item = {
            "option_strings": list(action.option_strings), "dest": action.dest,
            "nargs": stable(action.nargs), "const": stable(action.const),
            "default": stable(action.default), "type": stable(action.type),
            "required": action.required, "choices": stable(action.choices),
            "metavar": action.metavar, "help": action.help,
        }
        if isinstance(action, argparse._SubParsersAction):
            item["choices"] = sorted(action.choices)
            subparsers = {
                name: _parser_snapshot(child, stable=stable)
                for name, child in sorted(action.choices.items())
            }
        actions.append(item)
    result = {
        "description": parser.description, "usage": parser.usage,
        "formatter": f"{parser.formatter_class.__module__}.{parser.formatter_class.__qualname__}",
        "actions": actions,
    }
    if subparsers is not None:
        result["subparsers"] = subparsers
    return result


def _delegate_stable(value):
    """Normalize agent_delegate's runtime root independently of layout."""
    root = agent_delegate.ROOT
    if isinstance(value, Path):
        try:
            return "<ROOT>/" + value.relative_to(root).as_posix()
        except ValueError:
            return _stable(value)
    if isinstance(value, str):
        if value == str(root):
            return "<ROOT>"
        prefix = str(root) + os.sep
        if value.startswith(prefix):
            return "<ROOT>/" + value[len(prefix):].replace(os.sep, "/")
    return _stable(value)


def _delegate_help(text: str) -> str:
    return text.replace(str(agent_delegate.ROOT), "<ROOT>")


def _parser_golden_payload():
    parser = endeavor_db.build_parser()
    parser.prog = "endeavor_db.py"
    help_text = {"root": _delegate_help(parser.format_help())}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in sorted(action.choices.items()):
                child.prog = f"endeavor_db.py {name}"
                help_text[name] = _delegate_help(child.format_help())
    errors = {}
    probes = {"root-empty": (parser, [])}
    for name, child in sorted(parser._actions[-1].choices.items()):
        probes[name] = (child, [])
    for name, (target, argv) in probes.items():
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            try:
                target.parse_args(argv)
            except SystemExit as exc:
                errors[name] = {"exit": exc.code, "stderr": stderr.getvalue()}
    return {"parser": _parser_snapshot(parser), "help": help_text, "errors": errors}


def _parser_error_probe_payload():
    parser = endeavor_db.build_parser()
    parser.prog = "endeavor_db.py"
    subparsers = next(
        action for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    probes = {
        "positive_int_zero": ("embed-backfill", ["--batch-size", "0"]),
        "positive_int_text": ("embed-backfill", ["--batch-size", "not-a-number"]),
        "pack_budget_low": ("pack", ["--project", "DEMO", "--budget", "499"]),
        "pack_budget_high": ("pack", ["--project", "DEMO", "--budget", "50001"]),
        "pack_budget_text": ("pack", ["--project", "DEMO", "--budget", "not-a-number"]),
        "nonempty_text": ("pending", ["--project", "   "]),
        "invalid_choice": ("query", ["needle", "--semantic", "maybe"]),
        "mutually_exclusive": ("pending", ["--project", "DEMO", "--all-projects"]),
        "feedback_result_bad": (
            "feedback", ["--agent", "claude", "--query", "x", "--result", "notanid"],
        ),
    }
    result = {}
    for label, (command, argv) in probes.items():
        child = subparsers.choices[command]
        child.prog = f"endeavor_db.py {command}"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            try:
                child.parse_args(argv)
            except SystemExit as exc:
                result[label] = {"argv": argv, "exit": exc.code, "stderr": stderr.getvalue()}
    return result


def _agent_delegate_contract_payload():
    parser = agent_delegate.build_delegate_parser()
    parser.prog = "agent_delegate.py"
    help_text = {"root": parser.format_help()}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in sorted(action.choices.items()):
                child.prog = f"agent_delegate.py {name}"
                help_text[name] = child.format_help()
    errors = {}
    for label, argv in {
        "root_empty": [],
        "unknown_target": ["terra", "prompt"],
        "missing_prompt": ["codex"],
        "invalid_timeout": ["codex", "prompt", "--timeout", "0"],
    }.items():
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            try:
                parser.parse_args(argv)
            except SystemExit as exc:
                errors[label] = {"argv": argv, "exit": exc.code, "stderr": stderr.getvalue()}
    management = {}
    for command, argv in {
        "status_help": ["status", "--help"],
        "status_missing_run_id": ["status"],
        "wait_invalid_timeout": ["wait", "run-1", "--timeout", "0"],
        "cancel_help": ["cancel", "--help"],
        "diagnose_missing_target": ["diagnose"],
        "advise_missing_prompt": ["advise"],
    }.items():
        stderr = io.StringIO()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                agent_delegate.management_main(argv)
            except SystemExit as exc:
                management[command] = {
                    "argv": argv, "exit": exc.code, "stdout": stdout.getvalue(),
                    "stderr": stderr.getvalue(),
                }
    return {
        "parser": _parser_snapshot(parser, stable=_delegate_stable),
        "help": help_text,
        "errors": errors,
        "management": management,
    }


def _sha256_json(payload) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _assert_cli_stdout_contract(test, actual, contract):
    if contract["format"] == "text":
        expected = endeavor_db.AGENT_HELP_TEXT if contract["exact"] == "AGENT_HELP_TEXT" else contract["exact"]
        test.assertEqual(actual, expected)
        return
    test.assertEqual(contract["format"], "json")
    payload = json.loads(actual)
    test.assertIsInstance(payload, dict)
    key_sets = contract["key_sets"]
    types = contract["types"]
    values = contract.get("values", {})

    def resolve(path):
        value = payload
        for component in path.split(".") if path else ():
            value = value[component]
        return value

    def type_name(value):
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, int):
            return "integer"
        if isinstance(value, float):
            return "number"
        if isinstance(value, str):
            return "string"
        if isinstance(value, list):
            return "array"
        if isinstance(value, dict):
            return "object"
        return type(value).__name__

    for path, expected_keys in key_sets.items():
        value = resolve(path)
        test.assertIsInstance(value, dict, path)
        test.assertEqual(set(value), set(expected_keys), path)
    for path, expected_type in types.items():
        test.assertEqual(type_name(resolve(path)), expected_type, path)
    for path, expected_value in values.items():
        if expected_value == "<DB_PATH>":
            test.assertEqual(resolve(path), str(GOLDEN_FIXTURE), path)
        else:
            test.assertEqual(resolve(path), expected_value, path)


CLI_COMMAND_ROUTES = {
    "seed": "seed", "ingest": "ingest_markdown", "query": "search_all",
    "stats": "table_count", "embed-status": "embedding_stats",
    "embed-warm": "set_embed_keep_warm", "embed-cool": "set_embed_keep_warm",
    "embed-backfill": "backfill_embeddings", "bootstrap": "bootstrap",
    "pack": "build_pack", "pending": "build_pending_worklist",
    "activity": "render_activity", "evaluate": "evaluate_queries",
    "feedback": "record_feedback", "record-add": "create_memory_record",
    "record-update": "update_memory_record", "record-link": "add_memory_relation",
    "record-show": "memory_record_context", "record-search": "search_memory_records",
    "session-start": "start_session", "checkpoint": "add_checkpoint",
    "pin-checkpoint": "set_checkpoint_pinned", "unpin-checkpoint": "set_checkpoint_pinned",
    "handoff": "handoff", "session-close": "resolve_session",
    "presence-start": "presence_start", "presence-heartbeat": "presence_heartbeat",
    "presence-stop": "presence_stop", "presence": "list_presence",
    "sync-status": "sync_freshness_report", "timeline": "checkpoint_timeline",
    "readiness": "readiness_report",
    "ann-status": "ann_helper", "ann-build": "ann_helper",
    "event-add": "publish_event", "event-poll": "poll_events", "event-ack": "acknowledge_event",
}


class _StubCursor:
    def __init__(self, statement):
        self.statement = statement

    def fetchone(self):
        if "integrity_check" in self.statement:
            return ("ok",)
        if "foreign_keys" in self.statement:
            return (1,)
        return (0,)

    def fetchall(self):
        return []


class _StubConnection:
    """Deterministic fake sqlite3.Connection; `execute` never mutates
    `total_changes` itself so tests control write-detection explicitly."""

    def __init__(self):
        self.total_changes = 0

    def execute(self, *args, **kwargs):
        return _StubCursor(str(args[0]) if args else "")

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _cli_runtime_replacements(command, conn):
    """Deterministic, side-effect-free mocks that let every registered CLI
    command reach and exercise its real dispatch branch in `endeavor_db.main`."""
    replacements = {
        "connect": mock.Mock(return_value=conn),
        "initialize": mock.Mock(),
        "database_path": mock.Mock(return_value=GOLDEN_FIXTURE),
        "refresh_activity_export": mock.Mock(),
        "write_sync_freshness_signal": mock.Mock(),
        "table_exists": mock.Mock(return_value=True),
        "table_count": mock.Mock(return_value=0),
        "database_schema_version": mock.Mock(return_value=endeavor_db.SCHEMA_VERSION),
        "embedding_stats": mock.Mock(return_value={"embedded": 0, "invalid_blobs": 0}),
        "embedding_diagnostics": mock.Mock(return_value={}),
        "hook_status": mock.Mock(return_value={}), "hooks_ok": mock.Mock(return_value=True),
        "fts_index_identity": mock.Mock(return_value={"indexed": 0, "verified": True, "missing": [], "extra": []}),
        "memory_relation_health": mock.Mock(return_value={"ok": True}),
        "install_hooks": mock.Mock(return_value={}),
        "load_payload": mock.Mock(return_value={}),
        "resolve_session": mock.Mock(return_value={"id": "s", "project": "P"}),
        "resolve_or_start_checkpoint_session": mock.Mock(return_value={"id": "s"}),
        "local_machine": mock.Mock(return_value="test-machine"),
        "now_utc": mock.Mock(return_value="2026-01-01T00:00:00+00:00"),
    }
    route = CLI_COMMAND_ROUTES.get(command)
    if route:
        replacements[route] = mock.Mock(return_value={"route": command})
    if command == "ann-build":
        replacements["ann_helper"] = mock.Mock(return_value={"route": command, "status": "ready"})
    if command in ("record-add", "record-link"):
        replacements["memory_record_context"] = mock.Mock(return_value={"route": command})
    if command == "session-close":
        replacements["resolve_session"] = mock.Mock(return_value={"id": "s", "project": "P"})
    if command == "embed-diagnose":
        replacements["embedding_diagnostics"] = mock.Mock(return_value={"route": command})
    return replacements


def _normalize_cli_stderr(value):
    """Freeze argparse diagnostics while allowing unittest/script invocation names."""
    if value.startswith("usage: ") and " query [" in value:
        value = "usage: endeavor_db.py query" + value.split(" query", 1)[1]
        value = re.sub(r"\n.*? query: error:", "\nendeavor_db.py query: error:", value, count=1)
        return " ".join(value.split())
    return value


class CompatibilityContractTest(unittest.TestCase):
    def test_facade_functions_exist(self):
        for module, names in ((endeavor_db, DB_FUNCTIONS), (agent_delegate, DELEGATE_FUNCTIONS)):
            with self.subTest(module=module.__name__):
                for name in names:
                    self.assertTrue(callable(getattr(module, name, None)), name)

    def test_selected_signatures_are_frozen(self):
        modules = {"endeavor_db": endeavor_db, "agent_delegate": agent_delegate}
        for (module_name, name), expected in SIGNATURES.items():
            with self.subTest(module=module_name, name=name):
                self.assertEqual(str(inspect.signature(getattr(modules[module_name], name))), expected)

    def test_constants_and_exception_identity_are_frozen(self):
        modules = {"endeavor_db": endeavor_db, "agent_delegate": agent_delegate}
        for (module_name, name), expected in CONSTANTS.items():
            with self.subTest(module=module_name, name=name):
                self.assertEqual(getattr(modules[module_name], name), expected)
        self.assertIs(endeavor_db.SessionNotFoundError.__base__, ValueError)
        self.assertIs(endeavor_db.AmbiguousSessionError.__base__, ValueError)
        self.assertNotEqual(endeavor_db.SessionNotFoundError, endeavor_db.AmbiguousSessionError)

    def test_parser_contract_is_complete_and_deterministic(self):
        self.assertEqual(
            _GOLDEN["contract_scope"],
            "Phase 0 current workspace; canonical snapshots use stable parser/schema values only",
        )
        payload = _parser_golden_payload()
        self.assertEqual(
            sorted(set(payload["help"]) - {"root"}),
            _GOLDEN["endeavor_db_commands"],
        )
        self.assertEqual(_sha256_json(payload), PARSER_GOLDEN_SHA256)
        # The root parser must retain argparse's required-command failure shape;
        # this catches an accidental optional subcommand even if help is unchanged.
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            endeavor_db.build_parser().parse_args([])
        self.assertEqual(raised.exception.code, 2)

    def test_agent_delegate_parser_and_management_contract_is_deep_and_deterministic(self):
        minor = f"{sys.version_info.major}.{sys.version_info.minor}"
        self.assertIn(
            minor,
            AGENT_DELEGATE_GOLDEN_BY_MINOR,
            f"unsupported Python minor for agent_delegate contract: {minor}",
        )
        self.assertEqual(
            _sha256_json(_agent_delegate_contract_payload()),
            AGENT_DELEGATE_GOLDEN_BY_MINOR[minor],
        )

    def test_agent_delegate_management_runtime_cases_are_side_effect_free(self):
        cases = _GOLDEN["agent_delegate_runtime_cases"]
        for case in cases:
            with self.subTest(case=case["name"]):
                stdout = io.StringIO()
                stderr = io.StringIO()
                patches = [mock.patch.object(agent_delegate, "read_run_state", side_effect=FileNotFoundError("missing"))]
                if case["name"] == "diagnose_unavailable":
                    patches = [mock.patch.object(agent_delegate, "find_binary", return_value=None)]
                with contextlib.ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        code = agent_delegate.management_main(case["argv"])
                self.assertEqual(code, case["exit"])
                self.assertEqual(stderr.getvalue(), case["stderr"])
                payload = json.loads(stdout.getvalue())
                self.assertEqual(set(payload), set(case["json_keys"]))
                for key, value in case.get("json_values", {}).items():
                    self.assertEqual(payload[key], value)

    def test_parser_error_probes_are_exact_and_deterministic(self):
        minor = f"{sys.version_info.major}.{sys.version_info.minor}"
        expected_choice = PARSER_ERROR_CHOICE_BY_MINOR.get(minor)
        self.assertIsNotNone(
            expected_choice,
            f"unsupported Python minor {minor}; add an exact parser-error fixture before running this contract",
        )
        actual = _parser_error_probe_payload()
        # Replace the version-sensitive probe on a fresh mapping so test order
        # cannot mutate the module-level golden fixture.
        expected = dict(_GOLDEN["parser_error_probes"])
        expected["invalid_choice"] = expected_choice
        self.assertEqual(actual, expected)

    def test_mcp_schema_and_instructions_are_complete_golden_contracts(self):
        payload = {"tools": mcp_server.TOOLS, "server_instructions": mcp_server.SERVER_INSTRUCTIONS}
        self.assertEqual(_sha256_json(payload), MCP_GOLDEN_SHA256)
        self.assertEqual(
            hashlib.sha256(mcp_server.SERVER_INSTRUCTIONS.encode()).hexdigest(),
            MCP_INSTRUCTIONS_SHA256,
        )
        self.assertEqual(
            set(mcp_server.TOOL_BY_NAME), {tool["name"] for tool in mcp_server.TOOLS},
        )
        self.assertEqual(
            [tool["name"] for tool in mcp_server.TOOLS],
            _GOLDEN["mcp_tool_names"],
        )

    def test_agent_mcp_schema_and_instructions_are_deep_golden_contracts(self):
        payload = {
            "tools": agent_mcp_server.TOOLS,
            "server_instructions": agent_mcp_server.SERVER_INSTRUCTIONS,
            "protocols": sorted(agent_mcp_server.SUPPORTED_PROTOCOLS),
            "annotations": {
                "start": agent_mcp_server.START_ANNOTATIONS,
                "status": agent_mcp_server.STATUS_ANNOTATIONS,
                "cancel": agent_mcp_server.CANCEL_ANNOTATIONS,
            },
        }
        self.assertEqual(_sha256_json(payload), AGENT_MCP_GOLDEN_SHA256)

    def test_external_paths_have_distinct_stable_tokens(self):
        external_root = CONTRACT_ROOT.parent / "_endmemex_contract_external"
        first = external_root / "one" / "same.md"
        second = external_root / "two" / "same.md"
        self.assertNotEqual(_stable(first), _stable(second))
        self.assertEqual(_stable(first), _stable(first))
        self.assertIn(first.as_posix(), _stable(first))

    def test_workspace_root_prefers_standalone_repo_over_git_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            monorepo = Path(directory)
            (monorepo / ".git").mkdir()
            nested = monorepo / "ENDMEMEX"
            nested.mkdir()
            self.assertEqual(config._workspace_root(nested), monorepo)

            standalone = monorepo / "public-clone"
            standalone.mkdir()
            (standalone / ".git").mkdir()
            self.assertEqual(config._workspace_root(standalone), standalone)

    def test_agent_mcp_timing_limits_are_manifested(self):
        for name, expected in AGENT_MCP_CONSTANTS.items():
            self.assertEqual(getattr(agent_mcp_server, name), expected)

    def test_known_monkeypatch_surface_is_present(self):
        for module, names in {
            endeavor_db: (
                "ROOT", "INDEX_VERSION", "SEMANTIC_FULL_SCAN_LIMIT", "embed_texts",
                "subprocess", "time", "_embed_health", "_spawn_embed_server",
                "_write_presence_sidecar",
                "_embed_health_probe", "_embed_python_candidates", "_embed_python_has_dependencies",
                "connect", "initialize", "database_path", "build_pending_worklist",
                "ensure_embed_server", "embed_failure_reason", "embedding_diagnostics",
                "set_embed_keep_warm", "backfill_embeddings", "_tracked_docs_summary",
                "hook_status", "hooks_ok", "install_hooks",
                "auto_detect_changed_files", "resolve_embed_python", "embedding_stats",
                "resolve_or_start_checkpoint_session", "activity_export_path",
                "activity_line", "render_activity", "prune_activity_log",
                "refresh_activity_export", "write_sync_freshness_signal", "PRESENCE_DIR",
                "SYNC_FRESHNESS_DIR", "_git_hooks_dir", "GIT_HOOK_SOURCES", "urlopen",
                "local_machine",
            ),
            agent_delegate: (
                "RUNS_DIR", "DB_SCRIPT", "_read_json", "_read_output", "_sha256",
                "_process_exists", "_process_start_token", "_group_member_pids",
                "Path", "wait_retry_or_cancel", "update_run_state", "update_run_state_if",
                "run_child_to_files", "_config_from_args",
                "subprocess", "os", "time", "sys", "fcntl", "ctypes", "append_log",
                "find_binary", "_binary_version", "execute_managed", "new_run", "read_run_state",
                "progress_snapshot", "infer_caller", "management_main",
                "CHECKPOINT_TIMEOUT_S", "POST_KILL_DRAIN_S", "GUARD_DRAIN_GRACE_S",
                "CANCEL_GRACE_S", "MANAGER_HEARTBEAT_S", "MANAGER_STALE_S",
                "START_PUBLICATION_GRACE_S",
                "MAX_ARTIFACT_BYTES_PER_STREAM", "DEPTH_ENV", "CALLER_ENV",
                "LOG_PROMPT_CHARS", "LOG_OUTPUT_CHARS", "LOG_STDERR_CHARS",
                "RUN_DIR_MAX_AGE_DAYS",
            ),
            mcp_server: ("run", "subprocess"),
            agent_mcp_server: (
                "admitted_start", "run_backend", "RUNS_DIR", "ADMISSION_LOCK", "START_HISTORY",
                "subprocess", "_process_exists",
            ),
        }.items():
            for name in names:
                self.assertTrue(hasattr(module, name), f"{module.__name__}.{name}")

    def test_requested_facade_seams_have_behavioral_patch_points(self):
        with tempfile.TemporaryDirectory() as directory:
            sidecars = Path(directory) / "presence"
            freshness = Path(directory) / "freshness"
            hooks = Path(directory) / "hooks"
            source = Path(directory) / "pre-commit-source"
            source.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            with mock.patch.object(endeavor_db, "PRESENCE_DIR", sidecars), mock.patch.object(
                endeavor_db, "SYNC_FRESHNESS_DIR", freshness
            ), mock.patch.object(endeavor_db, "local_machine", return_value="test-machine"):
                conn = sqlite3.connect(":memory:")
                conn.row_factory = sqlite3.Row
                endeavor_db.initialize(conn)
                endeavor_db.presence_start(conn, "test-machine", "codex", "DEMO", "phase 0")
                self.assertEqual(json.loads((sidecars / "test-machine.json").read_text())["machine"], "test-machine")
                endeavor_db.write_sync_freshness_signal("test-machine", "unit-test")
                report = endeavor_db.sync_freshness_report("test-machine")
                self.assertEqual(report["machines"]["test-machine"]["last_command"], "unit-test")
                conn.close()
            hooks.mkdir()
            with mock.patch.object(endeavor_db, "_git_hooks_dir", return_value=hooks), mock.patch.object(
                endeavor_db, "GIT_HOOK_SOURCES", {"pre-commit": source}
            ):
                self.assertEqual(endeavor_db.hook_status(), {"pre-commit": "missing"})
                self.assertEqual(endeavor_db.install_hooks(), {"pre-commit": "installed"})
                self.assertEqual(endeavor_db.hook_status(), {"pre-commit": "installed"})

        with mock.patch.object(endeavor_db, "_embed_health", return_value={
            "status": "ready", "model": endeavor_db.EMBED_MODEL_NAME, "dim": endeavor_db.EMBED_DIM,
        }) as health:
            self.assertTrue(endeavor_db.ensure_embed_server(wait=False))
        health.assert_called_once()

    def test_facade_seams_are_behavioral_not_only_names(self):
        with tempfile.TemporaryDirectory() as directory:
            project_root = Path(directory)
            (project_root / "demo").mkdir()
            with mock.patch.object(endeavor_db, "ROOT", project_root), mock.patch.object(
                endeavor_db, "subprocess"
            ) as subprocess_module:
                subprocess_module.run.return_value = subprocess.CompletedProcess(
                    [], 0, stdout=" M demo/file.md\n?? demo/new.md\n", stderr=""
                )
                self.assertEqual(
                    endeavor_db.auto_detect_changed_files("demo"),
                    ["demo/file.md", "demo/new.md"],
                )
                subprocess_module.run.assert_called_once()
                self.assertEqual(subprocess_module.run.call_args.args[0][1], "-C")
                self.assertEqual(subprocess_module.run.call_args.args[0][2], str(project_root))

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        endeavor_db.initialize(conn)
        with mock.patch.object(endeavor_db, "embed_texts", return_value=[[0.0] * endeavor_db.EMBED_DIM]) as embed:
            self.assertEqual(endeavor_db.semantic_memory_records(conn, "needle", None, None, 5), [])
        embed.assert_called_once_with(["needle"])
        conn.close()

        with mock.patch.object(agent_mcp_server, "_process_exists", return_value=True), mock.patch.object(
            agent_delegate, "_process_start_token", return_value="patched-token"
        ) as token:
            self.assertTrue(agent_mcp_server._child_identity_alive({"pid": 123, "process_start_token": "patched-token"}))
        token.assert_called_once_with(123)

    def test_cli_help_and_required_argument_errors_cover_every_command(self):
        parser = endeavor_db.build_parser()
        subparsers = next(
            action for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        for name, child in sorted(subparsers.choices.items()):
            with self.subTest(command=name):
                with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as help_exit:
                    child.parse_args(["--help"])
                self.assertEqual(help_exit.exception.code, 0)
                required = [
                    action for action in child._actions
                    if action.required and not isinstance(action, argparse._HelpAction)
                ]
                if required:
                    with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error_exit:
                        child.parse_args([])
                    self.assertEqual(error_exit.exception.code, 2)

    def test_mcp_call_translates_maximal_valid_arguments_for_every_registered_tool(self):
        cases = [
            ("endeavor_memory_query", {"query": "needle", "project": "DEMO", "category": "debugging", "status": "resolved", "module": "mod.py", "bug_id": "BUG-1", "session_label": "S1", "semantic": "on", "limit": 20, "compact": False, "check_stale": True}, ["query", "needle", "--json", "--project", "DEMO", "--category", "debugging", "--status", "resolved", "--module", "mod.py", "--bug-id", "BUG-1", "--session-label", "S1", "--semantic", "on", "--limit", "20", "--check-stale"]),
            ("endeavor_memory_readiness", {"project": "DEMO"}, ["readiness", "--project", "DEMO", "--json"]),
            ("endeavor_memory_pack", {"project": "DEMO", "session": "S1", "budget": 50000}, ["pack", "--project", "DEMO", "--json", "--session", "S1", "--budget", "50000"]),
            ("endeavor_memory_pending", {"project": "DEMO"}, ["pending", "--json", "--project", "DEMO"]),
            ("endeavor_memory_handoff", {"project": "DEMO"}, ["handoff", "--project", "DEMO", "--json"]),
            ("endeavor_memory_timeline", {"project": "DEMO", "agent": "codex", "status": "paused", "session": "S1", "limit": 500, "oldest_first": True}, ["timeline", "--json", "--project", "DEMO", "--agent", "codex", "--status", "paused", "--session", "S1", "--limit", "500", "--oldest-first"]),
            ("endeavor_memory_record_show", {"id": "AUDIT-MEM-001"}, ["record-show", "AUDIT-MEM-001"]),
            ("endeavor_memory_record_search", {"query": "needle", "project": "DEMO", "type": "audit", "limit": 50, "current_only": True}, ["record-search", "needle", "--project", "DEMO", "--type", "audit", "--limit", "50", "--current-only"]),
            ("endeavor_memory_bootstrap", {"project": "DEMO", "session": "S1", "include_pending": True, "confirm": True}, ["bootstrap", "--project", "DEMO", "--json", "--session", "S1", "--include-pending"]),
            ("endeavor_memory_checkpoint", {"project": "DEMO", "session": "S1", "agent": "codex", "summary": "summary", "goal": "goal", "work_done": "done", "current_state": "state", "next_steps": "next", "blockers": "none", "status": "paused", "files": ["a.py", "b.py"], "auto_files": True, "commands": ["cmd"], "verify": ["test"], "pinned": True, "confirm": True}, ["checkpoint", "--project", "DEMO", "--agent", "codex", "--summary", "summary", "--session", "S1", "--goal", "goal", "--work-done", "done", "--current-state", "state", "--next-steps", "next", "--blockers", "none", "--status", "paused", "--file", "a.py", "--file", "b.py", "--auto-files", "--command", "cmd", "--verify", "test", "--pin"]),
            ("endeavor_memory_pin_checkpoint", {"checkpoint_id": 7, "agent": "codex", "pinned": True, "confirm": True}, ["pin-checkpoint", "7", "--agent", "codex"]),
            ("endeavor_memory_pin_checkpoint", {"checkpoint_id": 7, "agent": "codex", "pinned": False, "confirm": True}, ["unpin-checkpoint", "7", "--agent", "codex"]),
            ("endeavor_memory_record_add", {"id": "FIX-MEM-001", "project": "DEMO", "type": "fix", "title": "title", "content": "content", "status": "accepted", "action_state": "done", "source": "report.md", "link": "resolves:AUDIT-MEM-001", "links": ["verifies:FIX-MEM-000"], "agent": "codex", "confirm": True}, ["record-add", "--project", "DEMO", "--type", "fix", "--title", "title", "--content", "content", "--agent", "codex", "--id", "FIX-MEM-001", "--status", "accepted", "--action-state", "done", "--source", "report.md", "--link", "resolves:AUDIT-MEM-001", "--link", "verifies:FIX-MEM-000"]),
            ("endeavor_memory_record_update", {"id": "AUDIT-MEM-001", "project": "DEMO", "type": "audit", "title": "title", "content": "content", "status": "resolved", "action_state": "done", "metadata": {"source": "report.md"}, "agent": "codex", "confirm": True}, ["record-update", "AUDIT-MEM-001", "--agent", "codex", "--project", "DEMO", "--type", "audit", "--title", "title", "--content", "content", "--status", "resolved", "--action-state", "done", "--metadata", "{\"source\": \"report.md\"}"]),
            ("endeavor_memory_record_link", {"source_id": "FIX-MEM-001", "relation": "resolves", "target_id": "AUDIT-MEM-001", "note": "closed", "agent": "codex", "confirm": True}, ["record-link", "FIX-MEM-001", "resolves", "AUDIT-MEM-001", "--agent", "codex", "--note", "closed"]),
            ("endeavor_memory_session_close", {"session": "S1", "status": "completed", "agent": "codex", "confirm": True}, ["session-close", "--agent", "codex", "--session", "S1", "--status", "completed"]),
            ("endeavor_memory_feedback", {"agent": "codex", "query": "q", "results": [1, "AUDIT-MEM-001"], "useful": True, "note": "used", "confirm": True}, ["feedback", "--agent", "codex", "--query", "q", "--result", "1", "--result", "AUDIT-MEM-001", "--useful", "yes", "--note", "used"]),
            ("endeavor_memory_event_poll", {"after": 4, "project": "DEMO", "limit": 500, "include_acked": True}, ["event-poll", "--json", "--after", "4", "--project", "DEMO", "--limit", "500", "--include-acked"]),
            ("endeavor_memory_event_ack", {"event_id": 4, "agent": "codex", "confirm": True}, ["event-ack", "4", "--agent", "codex"]),
            ("endeavor_presence_start", {"agent": "codex", "project": "DEMO", "task": "task", "instance": "i", "session": "S1", "confirm": True}, ["presence-start", "--agent", "codex", "--project", "DEMO", "--task", "task", "--instance", "i", "--session", "S1"]),
            ("endeavor_presence_heartbeat", {"agent": "codex", "project": "DEMO", "task": "task", "instance": "i", "confirm": True}, ["presence-heartbeat", "--agent", "codex", "--project", "DEMO", "--task", "task", "--instance", "i"]),
            ("endeavor_presence_stop", {"agent": "codex", "project": "DEMO", "instance": "i", "confirm": True}, ["presence-stop", "--agent", "codex", "--project", "DEMO", "--instance", "i"]),
            ("endeavor_presence_list", {"project": "DEMO"}, ["presence", "--json", "--project", "DEMO"]),
            ("endeavor_sync_status", {}, ["sync-status", "--json"]),
        ]
        self.assertEqual(len(cases), len(mcp_server.TOOLS) + 1)
        with mock.patch.object(mcp_server, "run", return_value="ok") as run:
            for name, args, expected in cases:
                with self.subTest(name=name, args=args):
                    self.assertEqual(mcp_server.call(name, args), "ok")
                    self.assertEqual(run.call_args.args[0], expected)

    def test_import_identity_and_stdlib_boundary(self):
        self.assertEqual(Path(endeavor_db.__file__).resolve(), ENDMEMEX / "endeavor_db.py")
        self.assertEqual(Path(agent_delegate.__file__).resolve(), ENDMEMEX / "agent_delegate.py")
        self.assertIs(sys.modules["endeavor_db"], endeavor_db)
        self.assertIs(sys.modules["agent_delegate"], agent_delegate)
        self.assertFalse(hasattr(endeavor_db, "fastapi"))
        self.assertFalse(hasattr(endeavor_db, "sentence_transformers"))

    def test_direct_script_help_invariants(self):
        env = os.environ.copy()
        env.pop("ENDEAVOR_DB_PATH", None)
        commands = (
            ([sys.executable, str(ENDMEMEX / "endeavor_db.py"), "agent-help"], "Checkpoint after every material phase"),
            ([sys.executable, str(ENDMEMEX / "agent_delegate.py"), "--help"], "which agent to spawn"),
        )
        for command, expected in commands:
            with self.subTest(command=command[-1]):
                result = subprocess.run(
                    command, cwd=ROOT, env=env, capture_output=True, text=True,
                    check=False, timeout=10,
                )
                self.assertEqual(result.returncode, 0)
                self.assertIn(expected, result.stdout)
                self.assertEqual(result.stderr, "")

    def test_command_connection_and_finalization_contract(self):
        contract = _GOLDEN["command_connection_contract"]
        commands_in_contract = [c["command"] for c in contract]
        self.assertEqual(len(contract), 43, "must have a connection-contract row for all 43 commands")
        self.assertEqual(
            len(set(commands_in_contract)), len(commands_in_contract), "command rows must be unique",
        )
        self.assertEqual(set(commands_in_contract), set(_GOLDEN["endeavor_db_commands"]))

        argv_by_command = {case["command"]: case["argv"] for case in _GOLDEN["cli_runtime_cases"]}
        exit_by_command = {case["command"]: case["exit"] for case in _GOLDEN["cli_runtime_cases"]}
        self.assertEqual(set(argv_by_command), set(commands_in_contract))

        observed_read_only = set()
        for entry in contract:
            command = entry["command"]
            expected_connection = entry["connection"]
            expected_finalizes = entry["finalizes_when_changed"]
            expected_early_return = entry["early_return"]
            argv = argv_by_command[command]

            # `initialize(conn, force=...)` (endeavor_db.py:4638) runs right after
            # `changes_before = conn.total_changes` for every non-read-only command
            # and before command dispatch — the single injection point that lets one
            # mock drive the generic total_changes>changes_before finalization check
            # (endeavor_db.py:4914) for all write commands without modelling what
            # each routed function does to a real connection.
            changed_scenarios = (False, True) if expected_connection == "write" else (False,)
            for changed in changed_scenarios:
                with self.subTest(command=command, changed=changed):
                    conn = _StubConnection()

                    def _bump_on_initialize(*args, _conn=conn, _changed=changed, **kwargs):
                        if _changed:
                            _conn.total_changes += 1

                    replacements = _cli_runtime_replacements(command, conn)
                    replacements["initialize"] = mock.Mock(side_effect=_bump_on_initialize)

                    with contextlib.ExitStack() as stack:
                        for name, replacement in replacements.items():
                            stack.enter_context(mock.patch.object(endeavor_db, name, replacement))
                        stdout = io.StringIO()
                        stderr = io.StringIO()
                        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                            result = endeavor_db.main([command, *argv])
                    self.assertEqual(result, exit_by_command[command], command)

                    if expected_connection == "none":
                        replacements["connect"].assert_not_called()
                        replacements["initialize"].assert_not_called()
                        replacements["refresh_activity_export"].assert_not_called()
                        replacements["write_sync_freshness_signal"].assert_not_called()
                        continue

                    replacements["connect"].assert_called_once()
                    call_kwargs = replacements["connect"].call_args.kwargs
                    if expected_connection == "read_only":
                        observed_read_only.add(command)
                        self.assertTrue(call_kwargs.get("read_only"), f"{command} should connect read_only=True")
                        replacements["initialize"].assert_not_called()
                        replacements["refresh_activity_export"].assert_not_called()
                        replacements["write_sync_freshness_signal"].assert_not_called()
                        if expected_early_return:
                            # doctor returns inside its branch (endeavor_db.py:4792),
                            # before the generic finalization block — pin the one
                            # command this classification covers so a future refactor
                            # that moves the return can't drift silently.
                            self.assertIn(command, {"doctor", "ann-build"})
                    else:
                        self.assertEqual(expected_connection, "write")
                        self.assertFalse(
                            call_kwargs.get("read_only", False), f"{command} should not connect read_only=True",
                        )
                        replacements["initialize"].assert_called_once()
                        self.assertFalse(expected_early_return, f"{command} must not early-return before finalization")
                        if changed:
                            self.assertTrue(expected_finalizes, f"{command} fixture must expect finalization")
                            replacements["refresh_activity_export"].assert_called_once()
                            replacements["write_sync_freshness_signal"].assert_called_once_with(
                                "test-machine", command,
                            )
                        else:
                            replacements["refresh_activity_export"].assert_not_called()
                            replacements["write_sync_freshness_signal"].assert_not_called()

        self.assertEqual(observed_read_only, set(_GOLDEN["read_only_commands"]))
        self.assertEqual(len(observed_read_only), 20)

    def test_every_cli_command_has_a_side_effect_free_runtime_route(self):
        cases = _GOLDEN["cli_runtime_cases"]
        self.assertEqual({case["command"] for case in cases}, set(_GOLDEN["endeavor_db_commands"]))
        for case in cases:
            command = case["command"]
            with self.subTest(command=command):
                conn = _StubConnection()
                replacements = _cli_runtime_replacements(command, conn)
                route = CLI_COMMAND_ROUTES.get(command)
                with contextlib.ExitStack() as stack:
                    for name, replacement in replacements.items():
                        stack.enter_context(mock.patch.object(endeavor_db, name, replacement))
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        self.assertEqual(endeavor_db.main([command, *case["argv"]]), case["exit"])
                    self.assertEqual(_normalize_cli_stderr(stderr.getvalue()), case["stderr"])
                    _assert_cli_stdout_contract(self, stdout.getvalue(), case["stdout"])
                    if route:
                        replacements[route].assert_called()

    def test_endeavor_db_cli_errors_have_deterministic_nonzero_contracts(self):
        cases = _GOLDEN["cli_error_cases"]
        self.assertEqual(
            {c["name"] for c in cases}, {"query_missing_text", "stats_connect_error"},
        )
        self.assertEqual(len({c["name"] for c in cases}), len(cases), "case names must be unique")
        known_patch_modes = {"connect_oserror"}
        for case in cases:
            with self.subTest(name=case["name"]):
                patch_mode = case.get("patch")
                if patch_mode is not None:
                    self.assertIn(
                        patch_mode, known_patch_modes, f"unknown fixture patch mode: {patch_mode!r}",
                    )
                if case.get("raises_systemexit"):
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                            self.assertRaises(SystemExit) as raised:
                        endeavor_db.main(case["argv"])
                    self.assertEqual(raised.exception.code, case["exit"])
                    actual_stderr = stderr.getvalue()
                    if case.get("normalize_argparse"):
                        actual_stderr = _normalize_cli_stderr(actual_stderr)
                    self.assertEqual(actual_stderr, case["stderr"])
                    self.assertEqual(stdout.getvalue(), case["stdout"])
                else:
                    patches = {}
                    if patch_mode == "connect_oserror":
                        patches["connect"] = mock.patch.object(
                            endeavor_db, "connect", side_effect=OSError("fixture unavailable")
                        )
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    with contextlib.ExitStack() as stack:
                        for name, patcher in patches.items():
                            stack.enter_context(patcher)
                        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                            result = endeavor_db.main(case["argv"])
                    self.assertEqual(result, case["exit"])
                    self.assertEqual(stdout.getvalue(), case["stdout"])
                    self.assertEqual(stderr.getvalue(), case["stderr"])

    def test_memory_mcp_translations_errors_and_timeouts_are_exhaustive(self):
        for case in _GOLDEN["mcp_translation_cases"]:
            with self.subTest(tool=case["tool"]):
                with mock.patch.object(mcp_server, "run", return_value="{}") as run:
                    self.assertEqual(mcp_server.call(case["tool"], case["args"]), "{}")
                self.assertEqual(run.call_args.args[0], case["command"])
                with mock.patch.object(mcp_server, "run") as blocked:
                    self.assertTrue(mcp_server.call(case["tool"], {"unexpected": True}).startswith("[error]"))
                    blocked.assert_not_called()
                with mock.patch.object(mcp_server, "run", return_value=case["timeout_result"]):
                    self.assertEqual(mcp_server.call(case["tool"], case["args"]), case["timeout_result"])
        self.assertEqual({case["tool"] for case in _GOLDEN["mcp_translation_cases"]}, set(mcp_server.TOOL_BY_NAME))
        for tool in mcp_server.TOOLS:
            required = tool["inputSchema"].get("required", [])
            with self.subTest(missing_required=tool["name"]):
                if required:
                    self.assertTrue(mcp_server.call(tool["name"], {}).startswith("[error]"))


if __name__ == "__main__":
    unittest.main()
