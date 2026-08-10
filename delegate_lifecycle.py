"""Pure request-shaping, progress-parsing, and OS-identity helpers for
agent_delegate.py's managed sub-agent runs.

Scoped by a transitive-closure AST call-graph check over agent_delegate.py's
top-level functions against its monkeypatch set: these helpers are
transitively clean. write_checkpoint
(the 20th candidate the scan also cleared) stays in agent_delegate.py --
it is the only one of the clean set that reads ROOT/DB_SCRIPT, both of which
stay defined in agent_delegate.py (ROOT feeds build_delegate_parser's
`default=str(ROOT)`, a golden-contract-sensitive spot, and HERE -- which ROOT
derives from -- is computed via Path(__file__), and Path itself is a
monkeypatch target in test_agent_delegate.py). Threading ROOT/DB_SCRIPT
through a parameter for one function was judged not worth it, the same
trade already made for the checkpoint-retention cluster in sessions.py.

DEPTH_ENV/MAX_DEPTH/CALLER_ENV/LOG_PROMPT_CHARS/PROGRESS_TAIL_CHARS/
EXIT_ARTIFACT_LIMIT/EXIT_REAP_FAILED/TRANSIENT_MARKERS/READ_ONLY_CLAUDE_TOOLS
moved here because they are pure literals (no dependency on __file__) with
no monkeypatch reference of their own -- confirmed by grep against
test_agent_delegate.py -- and agent_delegate.py imports them back for its
own remaining (dirty) readers, the same relationship endeavor_db.py has
with config.py.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import time
from datetime import datetime

DEPTH_ENV = "ENDEAVOR_DELEGATE_DEPTH"
CALLER_ENV = "ENDEAVOR_DELEGATE_AS"
MAX_DEPTH = 1
LOG_PROMPT_CHARS = 500
PROGRESS_TAIL_CHARS = 6000
EXIT_ARTIFACT_LIMIT = 122
EXIT_REAP_FAILED = 123
READ_ONLY_CLAUDE_TOOLS = {"Read", "Grep", "Glob"}
TRANSIENT_MARKERS = (
    "rate limit", "overloaded", "temporarily unavailable", "service unavailable",
    "connection reset", "connection refused", "network error", "timed out",
)


class _DarwinProcBsdInfo(ctypes.Structure):
    """Stable prefix of macOS proc_bsdinfo through process birth time."""

    _fields_ = [
        (name, ctypes.c_uint32) for name in (
            "pbi_flags", "pbi_status", "pbi_xstatus", "pbi_pid", "pbi_ppid",
            "pbi_uid", "pbi_gid", "pbi_ruid", "pbi_rgid", "pbi_svuid",
            "pbi_svgid", "rfu_1",
        )
    ] + [
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
    ] + [
        (name, ctypes.c_uint32) for name in (
            "pbi_nfiles", "pbi_pgid", "pbi_pjobc", "e_tdev", "e_tpgid",
        )
    ] + [
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_run_id(run_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}", run_id):
        raise ValueError("invalid run id")
    return run_id


def parse_depth() -> int:
    """Read the recursion-depth env var defensively: an unparseable or
    tampered value fails closed (treated as already at the limit) rather
    than accidentally weakening the guard."""
    raw = os.environ.get(DEPTH_ENV, "0")
    try:
        depth = int(raw)
    except ValueError:
        return MAX_DEPTH
    return max(depth, 0)


def infer_caller() -> str:
    # CALLER_ENV is set explicitly by execute_managed() below when spawning a
    # sub-agent, so it reflects who that sub-agent actually is. Ambient
    # vars like CLAUDECODE are unreliable for this: they're inherited by
    # every descendant process, so a Codex child that (redundantly) still
    # sees its Claude grandparent's CLAUDECODE=1 would otherwise misreport
    # itself as "claude" — confirmed live, CLAUDECODE propagates downward.
    forced = os.environ.get(CALLER_ENV)
    if forced in ("claude", "codex"):
        return forced
    if os.environ.get("CLAUDECODE"):
        return "claude"
    if any(key.startswith("CODEX_") for key in os.environ):
        return "codex"
    return "unknown"


def effective_model(args: argparse.Namespace) -> str | None:
    """Resolve only the stable wrapper default; otherwise pass model IDs/aliases through."""
    if args.model:
        return args.model
    return "haiku" if args.target == "claude" else None


def effective_reasoning_effort(args: argparse.Namespace) -> str:
    """Apply the stable shared default while allowing future CLI values."""
    if args.reasoning_effort:
        return args.reasoning_effort
    return "medium"


def build_command(args: argparse.Namespace, binary: str) -> list[str]:
    model = effective_model(args)
    reasoning_effort = effective_reasoning_effort(args)
    if args.target == "codex":
        cmd = [binary, "exec", "-C", args.cwd, "-s", args.sandbox,
               "--skip-git-repo-check"]
        if getattr(args, "isolated", False):
            cmd += ["--ignore-user-config", "--ignore-rules", "--ephemeral"]
        if model:
            cmd += ["--model", model]
        cmd += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
        if args.json or getattr(args, "stream_progress", False):
            cmd.append("--json")
        cmd += ["--", args.prompt]
    else:
        cmd = [binary, "-p", "--model", model]
        cmd += ["--effort", reasoning_effort]
        if getattr(args, "isolated", False):
            cmd += [
                "--safe-mode", "--no-session-persistence",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            ]
        if getattr(args, "stream_progress", False):
            cmd += ["--output-format", "stream-json", "--verbose", "--include-partial-messages"]
        elif args.json:
            cmd += ["--output-format", "json"]
        # --allowedTools adds permission rules but does not restrict which
        # built-in tools are available. --tools is the actual availability
        # boundary, so always pass an exact set (including the empty set) to
        # make the documented no-tools default and read-only roles enforceable.
        available_tools = getattr(args, "available_tools", None)
        if available_tools is None:
            available_tools = args.allowed_tools
        cmd += ["--tools", ",".join(available_tools or [])]
        if args.allowed_tools:
            cmd += ["--allowedTools", *args.allowed_tools]
        if args.permission_mode:
            cmd += ["--permission-mode", args.permission_mode]
        cmd += ["--", args.prompt]
    return cmd


def base_entry(args: argparse.Namespace, caller: str) -> dict:
    return {
        "caller": caller,
        "target": args.target,
        "cwd": args.cwd,
        "prompt": args.prompt[:LOG_PROMPT_CHARS],
    }


def _event_texts(event: object) -> list[str]:
    """Extract human-readable incremental text from Codex/Claude JSONL events."""
    if not isinstance(event, dict):
        return []
    texts: list[str] = []
    event_type = event.get("type")
    if event_type == "result" and isinstance(event.get("result"), str) \
            and not event.get("is_error"):
        texts.append(event["result"])
    nested = event.get("event")
    if isinstance(nested, dict):
        delta = nested.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("text"), str):
            texts.append(delta["text"])
    item = event.get("item")
    if isinstance(item, dict) and item.get("type") in {"agent_message", "reasoning"}:
        for key in ("text", "content"):
            if isinstance(item.get(key), str):
                texts.append(item[key])
    message = event.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    texts.append(block["text"])
    return texts


def _event_error_texts(event: object) -> list[str]:
    """Extract failure/error text from Codex/Claude JSONL events.

    _event_texts only recognizes success shapes (result, message deltas,
    agent_message/reasoning items) -- a run that fails before producing any
    of those (e.g. Codex's item.type=="error" / top-level type=="error" /
    turn.failed.error, or Claude's result with is_error) renders an empty
    progress_tail/stdout_tail even though the artifact holds the real
    diagnostic, forcing a caller to open the raw log by hand."""
    if not isinstance(event, dict):
        return []
    texts: list[str] = []
    if event.get("type") == "error" and isinstance(event.get("message"), str):
        texts.append(event["message"])
    item = event.get("item")
    if isinstance(item, dict) and item.get("type") == "error" \
            and isinstance(item.get("message"), str):
        texts.append(item["message"])
    error = event.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        texts.append(error["message"])
    if event.get("type") == "result" and event.get("is_error"):
        result = event.get("result")
        if isinstance(result, str) and result:
            texts.append(result)
    return texts


def _filter_plain_progress(raw: str) -> str:
    """Keep human-useful agent updates while hiding terminal/source dumps.

    Codex emits its own narration and every executed command to stderr.  The
    latter can be thousands of lines of source/grep output and makes a healthy
    run appear noisy rather than informative.  Final stdout artifacts remain
    untouched; this applies only to the *live raw stderr view*.
    """
    kept: list[str] = []
    for paragraph in raw.split("\n\n"):
        lines = [line.rstrip() for line in paragraph.splitlines()]
        meaningful = [line for line in lines if line.strip()]
        if not meaningful:
            continue
        joined = "\n".join(meaningful)
        is_command_dump = (
            meaningful[0] in {"exec", "shell", "analysis"}
            or any(line.lstrip().startswith(("/bin/", "$ ")) for line in meaningful)
            or any(" succeeded in " in line or " failed in " in line for line in meaningful)
        )
        is_source_dump = (
            sum(bool(re.match(r"^\s*\d+\t", line)) for line in meaningful) >= 2
            or sum(line.startswith(("import ", "from ", "def ", "class ", "    def ", "    return ")) for line in meaningful) >= 2
        )
        if is_command_dump or is_source_dump:
            summary = "🔧 กำลังตรวจสอบ source และคำสั่ง…"
            if not kept or kept[-1] != summary:
                kept.append(summary)
            continue
        # Keep an individual plain status (`Thinking…`, `Running tool: …`),
        # agent narration, and errors; cap a malformed verbose paragraph.
        kept.append(joined[-1200:])
    return "\n\n".join(kept)[-PROGRESS_TAIL_CHARS:]


def classify_error(args: argparse.Namespace, caller: str, exit_code: int,
                   stdout: str, stderr: str) -> tuple[str | None, str | None]:
    combined = f"{stdout}\n{stderr}".lower()
    if exit_code == 0:
        return None, None
    if exit_code == 124:
        return "timeout", "Increase --timeout or reduce the task scope."
    if exit_code == EXIT_ARTIFACT_LIMIT:
        return "artifact_limit", "Reduce task output; the bounded artifact limit was reached."
    if exit_code == EXIT_REAP_FAILED:
        return "reap_failed", "Inspect the recorded PID; process exit could not be confirmed."
    if exit_code == 130:
        return "cancelled", None
    if exit_code == 126:
        return "launch_failed", "Check the selected binary and working directory."
    if args.target == "claude" and caller == "codex" and "not logged in" in combined:
        return (
            "sandbox_credential_unavailable",
            "Retry this delegation outside the Codex sandbox before asking the user to log in again.",
        )
    if any(marker in combined for marker in TRANSIENT_MARKERS):
        return "transient_service_error", "Retry with bounded backoff."
    return "child_failed", "Inspect stderr and the run artifacts."


def validate_result(args: argparse.Namespace, stdout: str, truncated: bool) -> list[str]:
    errors: list[str] = []
    if len(stdout.strip()) < args.min_output_chars:
        errors.append(f"output shorter than {args.min_output_chars} characters")
    if args.expect_regex:
        if truncated:
            errors.append("output exceeded validation limit and cannot be fully regex-validated")
        else:
            try:
                matched = re.search(args.expect_regex, stdout, re.MULTILINE)
            except re.error as exc:
                errors.append(f"invalid expectation regex: {exc}")
            else:
                if not matched:
                    errors.append(f"output did not match regex: {args.expect_regex}")
    if args.expect_json:
        if truncated:
            errors.append("output exceeded validation limit and cannot be validated as JSON")
        else:
            try:
                json.loads(stdout)
            except json.JSONDecodeError as exc:
                errors.append(f"output is not valid JSON: {exc}")
    return errors


def is_transient(exit_code: int, error_kind: str | None) -> bool:
    return exit_code == 124 or error_kind == "transient_service_error"


def role_policy_error(args: argparse.Namespace) -> str | None:
    if args.role == "worker":
        return None
    if args.target == "codex" and args.sandbox != "read-only":
        return f"{args.role} role requires --sandbox read-only"
    if args.target == "claude":
        granted = set(args.allowed_tools or []) | set(
            getattr(args, "available_tools", None) or [],
        )
        unsafe = sorted(granted - READ_ONLY_CLAUDE_TOOLS)
        if unsafe:
            return f"{args.role} role does not allow tool(s): {', '.join(unsafe)}"
    return None


def _config_from_args(args: argparse.Namespace) -> dict:
    config = vars(args).copy()
    config["caller"] = args.caller or infer_caller()
    config["model"] = effective_model(args)
    config["reasoning_effort"] = effective_reasoning_effort(args)
    return config


def _darwin_bsd_info(pid: int) -> _DarwinProcBsdInfo | None:
    try:
        info = _DarwinProcBsdInfo()
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidinfo = libproc.proc_pidinfo
        proc_pidinfo.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
            ctypes.c_void_p, ctypes.c_int,
        ]
        proc_pidinfo.restype = ctypes.c_int
        size = proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if size == ctypes.sizeof(info) and info.pbi_pid == pid:
            return info
    except (AttributeError, OSError):
        pass
    return None


def _process_group_exists(pgid: object) -> bool:
    if not isinstance(pgid, int) or pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _iso_age_seconds(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return max(0.0, time.time() - datetime.fromisoformat(value).timestamp())
    except ValueError:
        return None
