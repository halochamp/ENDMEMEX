#!/usr/bin/env python3
# Developer: Poomwat Jarussri
# Email: champoomwat@gmail.com
# GitHub: https://github.com/halochamp
"""Stdio MCP bridge for policy-bounded managed cross-agent delegation.

This server deliberately stays separate from mcp_server.py: delegation can
start external CLI/model processes, while the existing server remains a
project-memory-only trust and failure boundary. The implementation delegates
all lifecycle work to agent_delegate.py instead of duplicating it here.
"""
from __future__ import annotations

import json
import fcntl
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import agent_delegate
from config import ROOT

HERE = Path(__file__).resolve().parent
DELEGATE = HERE / "agent_delegate.py"

CONTROL_TIMEOUT_S = 15
OUTPUT_DETAIL_CHARS = 4000
MAX_CONCURRENT_RUNS = 4
MAX_STARTS_PER_WINDOW = 6
START_WINDOW_S = 60
RUNS_DIR = Path(os.environ.get(
    "ENDMEMEX_AGENT_RUNS_DIR", HERE / ".agent_delegate_runs",
))
ADMISSION_LOCK = RUNS_DIR.parent / ".agent_mcp_admission.lock"
START_HISTORY = RUNS_DIR.parent / ".agent_mcp_start_history.json"
RUN_ID_PATTERN = "^[A-Za-z0-9][A-Za-z0-9_-]{7,127}$"
LATEST_PROTOCOL = "2025-03-26"
SUPPORTED_PROTOCOLS = {"2024-11-05", LATEST_PROTOCOL}

SERVER_INSTRUCTIONS = (
    "Endeavor Agents runs bounded Codex, Claude, or Antigravity (agy) sub-agents. Runs default to "
    "read-only; workspace_write must be requested explicitly and is restricted to the worker role. Use "
    "start for one cold, self-contained task, then poll status with the returned run_id; status includes "
    "incremental progress_tail chunks while the model is running. Report those chunks to the "
    "user instead of waiting silently for terminal output. Use cancel only when the run should "
    "stop. Reviewer and advisor roles always remain read-only. Claude and Antigravity workspace-write "
    "workers receive edit tools but no shell: Claude gets Read/Grep/Glob/Edit/Write with acceptEdits "
    "and no Bash preapproval; Antigravity gets --mode accept-edits, which auto-applies file edits but "
    "still denies run_command since --dangerously-skip-permissions is never passed. Use Codex or the "
    "parent when command execution is needed. The parent agent owns final conclusions and "
    "verification. Nested delegation is refused. Never put secrets in prompts because run requests "
    "and outputs are stored in local ENDMEMEX audit artifacts."
)

START_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}
STATUS_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
CANCEL_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": True,
    "openWorldHint": False,
}

RUN_ID_PROPERTY = {
    "type": "string",
    "pattern": RUN_ID_PATTERN,
    "description": "Managed run identifier returned by endeavor_agent_start.",
}

TOOLS = [
    {
        "name": "endeavor_agent_start",
        "description": (
            "WRITE, NON-IDEMPOTENT, OPEN WORLD. Starts one bounded managed sub-agent in the "
            "background and returns JSON containing run_id and status. Runs default to read-only. "
            "Explicit workspace_write is allowed only for role=worker: Codex uses its workspace "
            "sandbox; Claude receives Read/Grep/Glob/Edit/Write without Bash or bare edit "
            "preapproval; Antigravity (agy) gets --mode accept-edits, which auto-applies file edits "
            "but still denies shell/run_command. Reviewer and advisor "
            "roles are always read-only. The child starts cold, so include all needed context or tell it which "
            "ENDMEMEX handoff to read. Never include secrets; local run artifacts retain the "
            "request and output."
        ),
        "annotations": START_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["codex", "claude", "antigravity"],
                    "description": "CLI agent to start.",
                },
                "prompt": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 50000,
                    "description": "One self-contained bounded task with a clear deliverable; never include secrets.",
                },
                "role": {
                    "type": "string",
                    "enum": ["worker", "reviewer", "advisor"],
                    "description": "Role contract applied by agent_delegate.py (default worker).",
                },
                "access": {
                    "type": "string",
                    "enum": ["read_only", "workspace_write"],
                    "description": (
                        "Filesystem access policy (default read_only). workspace_write requires "
                        "role=worker; Claude and Antigravity write workers are edit-only and receive "
                        "no shell."
                    ),
                },
                "model": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 200,
                    "description": "Optional target CLI model alias or full ID; passed through without an allowlist.",
                },
                "reasoning_effort": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 100,
                    "description": "Optional target CLI reasoning effort; defaults to medium in agent_delegate.py.",
                },
                "timeout": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3600,
                    "description": "Child runtime limit in seconds (default 900; max 3600).",
                },
                "parent_record": {
                    "type": "string",
                    "pattern": "^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$",
                    "description": "Optional ENDMEMEX record ID for provenance attribution.",
                },
            },
            "required": ["target", "prompt"],
        },
    },
    {
        "name": "endeavor_agent_status",
        "description": (
            "READ-MOSTLY. Returns managed run state as JSON, including terminal result/output "
            "tails and incremental progress_tail/progress_bytes while running. Poll and surface "
            "new progress to the user instead of waiting silently. A status check may repair "
            "stale state to worker_died when its "
            "manager no longer exists, so it is not annotated as strictly read-only. Poll this "
            "tool instead of blocking the MCP server with a long wait."
        ),
        "annotations": STATUS_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"run_id": RUN_ID_PROPERTY},
            "required": ["run_id"],
        },
    },
    {
        "name": "endeavor_agent_cancel",
        "description": (
            "DESTRUCTIVE PROCESS CONTROL. Requests cancellation of one managed run and returns "
            "JSON with the resulting status. The live run manager signals and reaps its own "
            "process group; the MCP control process never signals a PGID loaded from disk. "
            "Repeating cancellation for a terminal run is safe."
        ),
        "annotations": CANCEL_ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"run_id": RUN_ID_PROPERTY},
            "required": ["run_id"],
        },
    },
]

TOOL_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def _validate_value(name: str, value: object, schema: dict) -> str | None:
    expected = schema.get("type")
    if expected == "string":
        if not isinstance(value, str):
            return f"{name} must be a string"
        if len(value) < schema.get("minLength", 0):
            return f"{name} must not be empty"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"{name} must be <= {schema['maxLength']} characters"
        if schema.get("pattern") and not re.fullmatch(schema["pattern"], value):
            return f"{name} has an invalid format"
    elif expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{name} must be an integer"
        if "minimum" in schema and value < schema["minimum"]:
            return f"{name} must be >= {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{name} must be <= {schema['maximum']}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{name} must be one of: {', '.join(schema['enum'])}"
    return None


def validate_arguments(name: str, args: object) -> str | None:
    tool = TOOL_BY_NAME.get(name)
    if tool is None:
        return f"unknown Endeavor agent tool: {name}"
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
    if name == "endeavor_agent_start":
        role = args.get("role", "worker")
        access = args.get("access", "read_only")
        if access == "workspace_write" and role != "worker":
            return "workspace_write requires role=worker; reviewer and advisor are read-only"
    return None


def run_backend(
    args: list[str], timeout: int = CONTROL_TIMEOUT_S, recovery_run_id: str | None = None,
) -> str:
    command = [sys.executable, str(DELEGATE), *args]
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        recovery = f"; recover with run_id {recovery_run_id}" if recovery_run_id else ""
        return f"[error] agent_delegate control command timed out after {timeout}s{recovery}"
    except OSError as exc:
        return f"[error] failed to launch agent_delegate.py: {exc}"

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    detail = stdout or stderr or "no diagnostic output"
    if result.returncode != 0:
        return f"[error] agent_delegate exited {result.returncode}: {detail[-OUTPUT_DETAIL_CHARS:]}"
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return f"[error] agent_delegate returned invalid JSON: {detail[-OUTPUT_DETAIL_CHARS:]}"
    if not isinstance(payload, dict):
        return "[error] agent_delegate returned a non-object JSON result"
    return json.dumps(payload, ensure_ascii=False)


def _process_exists(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _recently_created(state: dict, seconds: float = CONTROL_TIMEOUT_S + 5) -> bool:
    try:
        created = datetime.fromisoformat(str(state.get("created_at"))).timestamp()
    except ValueError:
        return False
    return time.time() - created <= seconds


def _child_identity_alive(state: dict) -> bool:
    """Verify the persisted child pid is still the exact process that was
    launched, not an unrelated process that reused the pid after the owner
    died. Falls back to alive when a birth token cannot be computed on
    either side: undercounting active runs would let admission exceed the
    concurrency limit, which is less safe than a rare overcount."""
    child = state.get("pid")
    if not _process_exists(child):
        return False
    stored_token = state.get("process_start_token")
    if stored_token is None:
        return True
    current_token = agent_delegate._process_start_token(child)
    if current_token is None:
        return True
    return current_token == stored_token


def _active_run_count() -> int:
    count = 0
    if not RUNS_DIR.exists():
        return 0
    for state_path in RUNS_DIR.glob("*/state.json"):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if state.get("status") in {"completed", "failed", "timed_out", "cancelled"}:
            continue
        # Keep ownership separate from the child: falling back to pid here
        # would let an ownerless, PID-reused child bypass the birth-token check.
        owner = state.get("manager_pid") or state.get("launcher_pid")
        if (_process_exists(owner) or _child_identity_alive(state)
                or (not owner and _recently_created(state))):
            count += 1
    return count


def _read_start_history(now: float) -> list[float]:
    try:
        values = json.loads(START_HISTORY.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        values = []
    return [float(value) for value in values if isinstance(value, (int, float))
            and now - float(value) < START_WINDOW_S]


def _write_start_history(values: list[float]) -> None:
    temporary = START_HISTORY.with_suffix(f".json.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(values) + "\n", encoding="utf-8")
    os.replace(temporary, START_HISTORY)


def admitted_start(command: list[str], run_id: str) -> str:
    ADMISSION_LOCK.parent.mkdir(parents=True, exist_ok=True)
    try:
        with ADMISSION_LOCK.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            active = _active_run_count()
            if active >= MAX_CONCURRENT_RUNS:
                return (f"[error] delegation concurrency limit reached "
                        f"({active}/{MAX_CONCURRENT_RUNS}); poll or cancel an active run")
            now = time.time()
            history = _read_start_history(now)
            if len(history) >= MAX_STARTS_PER_WINDOW:
                return (f"[error] delegation start rate limit reached "
                        f"({MAX_STARTS_PER_WINDOW} per {START_WINDOW_S}s); retry later")
            _write_start_history([*history, now])
            return run_backend(command, recovery_run_id=run_id)
    except OSError as exc:
        return f"[error] delegation admission control failed: {exc}"


def call(name: str, args: object) -> str:
    validation_error = validate_arguments(name, args)
    if validation_error:
        return f"[error] {validation_error}"
    assert isinstance(args, dict)

    if name == "endeavor_agent_start":
        run_id = uuid.uuid4().hex
        access = args.get("access", "read_only")
        sandbox = "workspace-write" if access == "workspace_write" else "read-only"
        command = [
            args["target"],
            "--background",
            "--isolated",
            "--stream-progress",
            "--run-id", run_id,
            "--role", args.get("role", "worker"),
            "--sandbox", sandbox,
            "--timeout", str(args.get("timeout", 900)),
        ]
        if args.get("model"):
            command += ["--model", args["model"]]
        if args.get("reasoning_effort"):
            command += ["--reasoning-effort", args["reasoning_effort"]]
        if args.get("parent_record"):
            command += ["--parent-record", args["parent_record"]]
        allowed_tools = ["Read", "Grep", "Glob"]
        if access == "workspace_write" and args["target"] == "claude":
            command += [
                "--available-tools", "Read", "Grep", "Glob", "Edit", "Write",
            ]
            command += ["--permission-mode", "acceptEdits"]
        # Keep the prompt after argparse's option terminator so a legitimate
        # task beginning with "--" cannot be mistaken for a wrapper flag.
        command += ["--allowed-tools", *allowed_tools, "--", args["prompt"]]
        return admitted_start(command, run_id)
    if name == "endeavor_agent_status":
        return run_backend(["status", args["run_id"], "--json"])
    if name == "endeavor_agent_cancel":
        return run_backend(["cancel", args["run_id"]])
    return "[error] unknown Endeavor agent tool"


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _tools_for_protocol(protocol: str) -> list[dict]:
    if protocol >= "2025-03-26":
        return TOOLS
    return [{key: value for key, value in tool.items() if key != "annotations"} for tool in TOOLS]


def _dispatch(
    request: object, negotiated_protocol: str, *, in_batch: bool = False,
) -> tuple[dict | None, str]:
    request_id = request.get("id") if isinstance(request, dict) else None
    is_notification = isinstance(request, dict) and "id" not in request
    try:
        if not isinstance(request, dict):
            raise JsonRpcError(-32600, "request must be a JSON object")
        if "id" in request and (
            isinstance(request["id"], bool)
            or not isinstance(request["id"], (str, int))
        ):
            request_id = None
            raise JsonRpcError(-32600, "request id must be a string or number")
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            raise JsonRpcError(-32600, "invalid JSON-RPC 2.0 request")
        method = request["method"]
        if method == "initialize":
            if in_batch:
                raise JsonRpcError(-32600, "initialize must not be batched")
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise JsonRpcError(-32602, "initialize params must be an object")
            requested = params.get("protocolVersion")
            negotiated_protocol = (
                requested if requested in SUPPORTED_PROTOCOLS else LATEST_PROTOCOL
            )
            result = {
                "protocolVersion": negotiated_protocol,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "endeavor-agents", "version": "1.0"},
                "instructions": SERVER_INSTRUCTIONS,
            }
        elif method == "ping":
            result = {}
        elif method in {"notifications/initialized", "notifications/cancelled"}:
            return None, negotiated_protocol
        elif method == "tools/list":
            result = {"tools": _tools_for_protocol(negotiated_protocol)}
        elif method == "tools/call":
            # A side-effecting notification would launch an untrackable run
            # because there is no response channel for its run ID.
            if is_notification:
                return None, negotiated_protocol
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise JsonRpcError(-32602, "tools/call params must be an object")
            name, arguments = params.get("name"), params.get("arguments", {})
            if not isinstance(name, str) or name not in TOOL_BY_NAME:
                raise JsonRpcError(-32602, f"unknown Endeavor agent tool: {name}")
            if not isinstance(arguments, dict):
                raise JsonRpcError(-32602, "tool arguments must be an object")
            output = call(name, arguments)
            result = {"content": [{"type": "text", "text": output}]}
            if output.startswith("[error]"):
                result["isError"] = True
        else:
            raise JsonRpcError(-32601, f"method not found: {method}")
        if is_notification:
            return None, negotiated_protocol
        return {"jsonrpc": "2.0", "id": request_id, "result": result}, negotiated_protocol
    except JsonRpcError as exc:
        if is_notification:
            return None, negotiated_protocol
        return {
            "jsonrpc": "2.0", "id": request_id,
            "error": {"code": exc.code, "message": str(exc)},
        }, negotiated_protocol
    except Exception as exc:
        if is_notification:
            return None, negotiated_protocol
        return {
            "jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32603, "message": str(exc)},
        }, negotiated_protocol


def serve() -> None:
    negotiated_protocol = LATEST_PROTOCOL
    for line in sys.stdin:
        try:
            incoming = json.loads(line)
        except json.JSONDecodeError as exc:
            print(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": f"parse error: {exc}"},
            }), flush=True)
            continue
        if isinstance(incoming, list):
            if not incoming:
                print(json.dumps({
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32600, "message": "empty JSON-RPC batch"},
                }), flush=True)
                continue
            responses: list[dict] = []
            for request in incoming:
                response, negotiated_protocol = _dispatch(
                    request, negotiated_protocol, in_batch=True,
                )
                if response is not None:
                    responses.append(response)
            if responses:
                print(json.dumps(responses), flush=True)
            continue
        response, negotiated_protocol = _dispatch(incoming, negotiated_protocol)
        if response is not None:
            print(json.dumps(response), flush=True)


if __name__ == "__main__":
    serve()
