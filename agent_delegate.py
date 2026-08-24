#!/usr/bin/env python3
# Developer: Poomwat Jarussri
# Email: champoomwat@gmail.com
# GitHub: https://github.com/halochamp
"""Delegate a one-shot task to the other CLI agent (Claude <-> Codex).

WHEN TO USE (agent-facing): you are Claude Code and want Codex to do a
bounded piece of work (or you are Codex and want Claude) without the user
switching tools — a second opinion on a diff, a parallel analysis, a task
the other agent is better trained for. The sub-agent runs headless, does
the task, prints its answer, and exits. For long multi-phase handoffs use
the ENDMEMEX checkpoint/handoff workflow instead (CLAUDE.md §7.5); this
wrapper is for one-shot delegation only.

USAGE:

    # Claude -> Codex (codex exec). Sandbox defaults to read-only;
    # pass workspace-write ONLY if the sub-agent must edit files.
    python3 ENDMEMEX/agent_delegate.py codex "summarize ENDMEMEX/schema.sql"
    python3 ENDMEMEX/agent_delegate.py codex "fix the TODO in sync_tracked.py" \
        --sandbox workspace-write
    python3 ENDMEMEX/agent_delegate.py codex "review this diff" \
        --model <codex-model-alias-or-id> --reasoning-effort high

    # Claude/Codex -> Antigravity (agy -p). Binary is "agy", not "antigravity";
    # resolved via TARGET_BINARIES. --add-dir <cwd> and --print-timeout are
    # always injected automatically -- without --add-dir, agy silently edits
    # its own internal scratch directory instead of --cwd and reports
    # success. --sandbox translates to --mode/--dangerously-skip-permissions
    # (no direct read-only flag exists; omitting --mode is the safe default).
    python3 ENDMEMEX/agent_delegate.py antigravity "summarize ENDMEMEX/schema.sql"
    python3 ENDMEMEX/agent_delegate.py antigravity "fix the TODO in sync_tracked.py" \
        --sandbox workspace-write

    # Codex -> Claude (claude -p). Model defaults to haiku (cheap).
    # Any current/future Claude alias or full model ID can be passed without
    # a wrapper update. Claude gets NO tools unless you grant them.
    python3 ENDMEMEX/agent_delegate.py claude "review this function: ..." \
        --model sonnet --allowed-tools Read Grep

    # --model is intentionally a free-form passthrough for both CLIs. When
    # omitted, Claude uses the wrapper's haiku default and Codex uses the
    # model configured by Codex CLI.
    # --reasoning-effort is likewise optional: it maps to Codex's
    # model_reasoning_effort config and Claude's --effort flag.

    # Managed/background execution. Every run gets durable local artifacts.
    # IMPORTANT for the delegating agent: completion is not pushed back into
    # your LLM context. Before using --background, arrange a status/wait poll
    # or a host-level completion trigger for this run_id; otherwise its result
    # can remain unread.
    python3 ENDMEMEX/agent_delegate.py claude "audit ENDMEMEX" --model sonnet \
        --role reviewer --background
    python3 ENDMEMEX/agent_delegate.py status <RUN_ID> --json
    python3 ENDMEMEX/agent_delegate.py wait <RUN_ID> --timeout 600 --json
    python3 ENDMEMEX/agent_delegate.py cancel <RUN_ID>

    # Sonnet performs the task; Opus reviews its findings as a read-only advisor.
    python3 ENDMEMEX/agent_delegate.py advise "audit ENDMEMEX for bugs" \
        --worker-model sonnet --advisor-model opus --result-format json

    # Preflight and result validation.
    python3 ENDMEMEX/agent_delegate.py diagnose claude
    python3 ENDMEMEX/agent_delegate.py claude "return JSON" --expect-json \
        --min-output-chars 2 --retries 1 --result-format json

    # Codex note: Claude CLI credentials may be unavailable to a sandboxed
    # Codex subprocess. When Claude reports "Not logged in" despite a working
    # terminal login, rerun this wrapper outside the sandbox so it can access
    # the user's Keychain/credential context. This is machine-dependent:
    # request the required execution approval; do not ask the user to log in
    # again until the out-of-sandbox check also fails.

RULES THE SUB-AGENT'S PROMPT MUST FOLLOW:
- The child starts COLD — it knows nothing about your session. Inline the
  needed context into the prompt, or tell it explicitly to run
  `python3 ENDMEMEX/endeavor_db.py handoff --project <P> --json` first.
- Give one bounded task with a clear deliverable ("reply with X"), not an
  open-ended mission; you read its stdout as the result.
- Never ask the child to delegate onward — depth is capped at 1 and the
  nested call will fail with exit 2.

EXIT CODES: child's own code on success path; 2 = nested delegation
refused; 3 = target CLI/run not found; 4 = result validation failed;
124 = timeout (default 900s, override with --timeout); 126 = failed to
launch the CLI; 130 = cancelled.

LOGGING: every call appends one JSON line to
ENDMEMEX/.agent_delegate_log.jsonl (plain file write, safe on both Macs,
never a DB write). Managed run state, request/result envelopes, checksums,
and disk-streamed stdout/stderr live under ENDMEMEX/.agent_delegate_runs/.
Add --checkpoint --project <P> to also record a real ENDMEMEX checkpoint.
This is opt-in and Main-Mac-only; the Backup Mac rejects every ENDMEMEX
database write (CLAUDE.md §7.5).

Full reference: ENDMEMEX/ENDMEMEX_USER_MANUAL.md §Cross-Agent Delegation.
"""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from config import ROOT
from delegate_cli_validators import nonnegative_float, nonnegative_int, positive_int
from delegate_lifecycle import (
    CALLER_ENV,
    DEPTH_ENV,
    EXIT_ARTIFACT_LIMIT,
    EXIT_REAP_FAILED,
    # LOG_PROMPT_CHARS has no reader here, but is tested directly as
    # agent_delegate.LOG_PROMPT_CHARS -- kept as a facade re-export, same
    # class as endeavor_db.py's KNOWLEDGE_CATEGORIES.
    LOG_PROMPT_CHARS,
    MAX_DEPTH,
    PROGRESS_TAIL_CHARS,
    READ_ONLY_CLAUDE_TOOLS,
    # TRANSIENT_MARKERS/_DarwinProcBsdInfo/_process_group_exists have no
    # reader here and no test reference -- not re-exported at all, same
    # treatment as endeavor_db.py's _insert_session.
    _config_from_args,
    _darwin_bsd_info,
    _event_error_texts,
    _event_texts,
    _filter_plain_progress,
    _iso_age_seconds,
    _safe_run_id,
    base_entry,
    build_command,
    classify_error,
    effective_model,
    effective_reasoning_effort,
    infer_caller,
    is_transient,
    now_iso,
    parse_depth,
    role_policy_error,
    validate_result,
)

HERE = Path(__file__).resolve().parent
LOG_PATH = HERE / ".agent_delegate_log.jsonl"
RUNS_DIR = Path(os.environ.get("ENDMEMEX_AGENT_RUNS_DIR", HERE / ".agent_delegate_runs"))
DB_SCRIPT = HERE / "endeavor_db.py"
RUN_ID_ENV = "ENDEAVOR_DELEGATE_RUN_ID"
LOG_OUTPUT_CHARS = 2000
LOG_STDERR_CHARS = 500
CHECKPOINT_TIMEOUT_S = 60
POST_KILL_DRAIN_S = 5
GUARD_DRAIN_GRACE_S = 5.0
CANCEL_GRACE_S = 2.0
MAX_VALIDATE_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_BYTES_PER_STREAM = 4 * 1024 * 1024
MANAGER_HEARTBEAT_S = 5.0
MANAGER_STALE_S = 20.0
START_PUBLICATION_GRACE_S = 20.0
TERMINAL_STATUSES = {"completed", "failed", "timed_out", "cancelled"}
BACKGROUND_NEXT_ACTION = (
    "If --project was supplied, a durable completion event will be published for a host "
    "to consume; otherwise arrange status/wait polling for this run_id."
)
RUN_DIR_MAX_AGE_DAYS = 14  # artifacts of a finished run are debugging material, not a durable record
ROLES = ("worker", "reviewer", "advisor")


ROLE_PREFIX = {
    "worker": "",
    "reviewer": (
        "Act as a read-only reviewer. Check the supplied work for concrete defects, "
        "cite evidence, and do not edit files.\n\n"
    ),
    "advisor": (
        "Act as a read-only senior advisor. Evaluate the supplied findings, return "
        "a verdict of accept, reject, or needs-evidence for each, explain risk and "
        "confidence, and do not edit files.\n\n"
    ),
}

CODEX_SANDBOXES = ("read-only", "workspace-write", "danger-full-access")
TARGET_BINARIES = {"codex": "codex", "claude": "claude", "antigravity": "agy"}


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def run_dir(run_id: str) -> Path:
    return RUNS_DIR / _safe_run_id(run_id)


def prune_old_runs(now: float | None = None) -> int:
    """Age out artifacts of long-finished runs. Nothing else bounded this
    directory, so it grew without limit -- every run keeps its request,
    state, stdout/stderr artifacts and logs forever, and the artifacts are
    the large part.

    Deliberately conservative about what counts as prunable:
      * a run whose state.json says it reached a TERMINAL status, and whose
        state has not been touched for RUN_DIR_MAX_AGE_DAYS;
      * a directory with no readable state.json at all (a run that died
        during creation) that is equally old -- judged by mtime.
    Anything still queued/running, anything recent, and anything whose state
    cannot be parsed is left strictly alone: reclaiming disk is never worth
    deleting a live run's working directory.

    Wrapped so it can never propagate: this is called on the start path, and
    a permission error or a directory being deleted concurrently must not be
    able to stop a new run from starting."""
    cutoff = (now if now is not None else time.time()) - RUN_DIR_MAX_AGE_DAYS * 86_400
    removed = 0
    try:
        if not RUNS_DIR.is_dir():
            return 0
        for directory in RUNS_DIR.iterdir():
            try:
                if not directory.is_dir():
                    continue
                state_path = directory / "state.json"
                try:
                    state = _read_json(state_path)
                    prunable = state.get("status") in TERMINAL_STATUSES
                    age_source = state_path.stat().st_mtime
                except (OSError, json.JSONDecodeError, AttributeError):
                    # No parseable state: only mtime can date it, and only a
                    # long-cold directory is safe to assume abandoned.
                    prunable = not state_path.exists()
                    age_source = directory.stat().st_mtime
                if prunable and age_source < cutoff:
                    shutil.rmtree(directory, ignore_errors=True)
                    removed += 1
            except OSError:
                continue
    except OSError:
        return removed
    return removed


def new_run(config: dict, run_id: str | None = None) -> tuple[str, Path]:
    run_id = run_id or uuid.uuid4().hex
    directory = run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=False)
    _atomic_json(directory / "request.json", config)
    _atomic_json(directory / "state.json", {
        "run_id": run_id,
        "status": "queued",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "caller": config.get("caller"),
        "target": config.get("target"),
        "model": config.get("model"),
        "reasoning_effort": config.get("reasoning_effort"),
        "role": config.get("role"),
        "kind": config.get("kind"),
    })
    # Only after this run's own state.json exists: the sweep treats a
    # stateless directory as prunable, and relying on the age gate alone to
    # spare a just-created one would be correct only by coincidence of
    # ordering.
    prune_old_runs()
    return run_id, directory


def update_run_state(directory: Path, **changes: object) -> dict:
    state_path = directory / "state.json"
    lock_path = directory / "state.lock"
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = _read_json(state_path)
        state.update(changes)
        state["updated_at"] = now_iso()
        _atomic_json(state_path, state)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return state


def update_run_state_if(directory: Path, predicate, **changes: object) -> tuple[dict, bool]:
    """Atomically update state only when the locked current value still matches."""
    state_path = directory / "state.json"
    lock_path = directory / "state.lock"
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = _read_json(state_path)
        applied = bool(predicate(state))
        if applied:
            state.update(changes)
            state["updated_at"] = now_iso()
            _atomic_json(state_path, state)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return state, applied


def read_run_state(run_id: str) -> dict:
    directory = run_dir(run_id)
    state = _read_json(directory / "state.json")
    activity = [directory.joinpath("state.json").stat().st_mtime]
    activity.extend(path.stat().st_mtime for path in directory.glob("*.log"))
    state["last_activity_epoch"] = max(activity)
    return state


def find_binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for candidate in (
        Path.home() / ".local" / "bin" / name,
        Path.home() / ".claude" / "local" / name,
    ):
        if candidate.exists():
            return str(candidate)
    return None


def append_log(entry: dict) -> bool:
    entry.setdefault("ts", datetime.now().astimezone().isoformat(timespec="seconds"))
    try:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[agent_delegate] warning: could not append delegation log ({exc})",
              file=sys.stderr)
        return False
    return True


def run_child_to_files(
    cmd: list[str], cwd: str, env: dict, timeout: int,
    stdout_path: Path, stderr_path: Path, on_started=None, should_cancel=None,
) -> tuple[int, int | None]:
    """Run a child while pipe readers enforce a hard per-artifact byte cap."""
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.touch()
    stderr_path.touch()
    proc = None
    readers: list[threading.Thread] = []
    artifact_exceeded = threading.Event()
    note: str | None = None
    code = 126
    try:
        guarded_cmd = [
            sys.executable, str(Path(__file__).resolve()), "_guard", "--", *cmd,
        ]
        proc = subprocess.Popen(
            guarded_cmd, cwd=cwd, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        readers = _start_pipe_readers(
            proc, stdout_path, stderr_path, artifact_exceeded,
        )
        if on_started:
            try:
                on_started(proc.pid)
            except Exception as exc:
                # The child already exists. A state-publication failure
                # must not escape and orphan an untracked CLI process.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except OSError:
                    pass
                try:
                    proc.wait(timeout=POST_KILL_DRAIN_S)
                except subprocess.TimeoutExpired:
                    note = f"failed to publish child state and reap child: {exc}"
                    return EXIT_REAP_FAILED, proc.pid
                note = f"failed to publish child state: {exc}"
                return 126, proc.pid
        deadline = time.monotonic() + timeout
        while True:
            if artifact_exceeded.is_set() or _artifact_limit_exceeded(stdout_path, stderr_path):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError as exc:
                    note = f"artifact-limit signal permission denied: {exc}"
                    return EXIT_REAP_FAILED, proc.pid
                try:
                    proc.wait(timeout=POST_KILL_DRAIN_S)
                except subprocess.TimeoutExpired:
                    note = "child remained after artifact-limit SIGKILL"
                    return EXIT_REAP_FAILED, proc.pid
                note = "child output exceeded the per-stream artifact limit"
                code = EXIT_ARTIFACT_LIMIT
                return code, proc.pid
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError as exc:
                    note = f"timeout signal permission denied: {exc}"
                    return EXIT_REAP_FAILED, proc.pid
                try:
                    proc.wait(timeout=POST_KILL_DRAIN_S)
                except subprocess.TimeoutExpired:
                    note = "child process group remained after timeout SIGKILL"
                    return EXIT_REAP_FAILED, proc.pid
                return 124, proc.pid
            try:
                exit_code = proc.wait(timeout=min(0.05, remaining))
                if artifact_exceeded.is_set() or _artifact_limit_exceeded(
                    stdout_path, stderr_path,
                ):
                    code = EXIT_ARTIFACT_LIMIT
                    return code, proc.pid
                return exit_code, proc.pid
            except subprocess.TimeoutExpired:
                if not should_cancel or not should_cancel():
                    continue
                # Cancellation is manager-owned: this process holds the
                # exact Popen object it created, so it never signals a
                # historical PGID read from disk after possible PID reuse.
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    try:
                        proc.wait(timeout=POST_KILL_DRAIN_S)
                        return 130, proc.pid
                    except subprocess.TimeoutExpired:
                        return EXIT_REAP_FAILED, proc.pid
                except PermissionError as exc:
                    note = f"cancel signal permission denied: {exc}"
                    return EXIT_REAP_FAILED, proc.pid
                try:
                    proc.wait(timeout=CANCEL_GRACE_S)
                    return 130, proc.pid
                except subprocess.TimeoutExpired:
                    pass
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    try:
                        proc.wait(timeout=POST_KILL_DRAIN_S)
                        return 130, proc.pid
                    except subprocess.TimeoutExpired:
                        return EXIT_REAP_FAILED, proc.pid
                except PermissionError as exc:
                    note = f"force-cancel permission denied: {exc}"
                    return EXIT_REAP_FAILED, proc.pid
                try:
                    proc.wait(timeout=POST_KILL_DRAIN_S)
                    return 130, proc.pid
                except subprocess.TimeoutExpired:
                    note = "child process group remained after force-cancel"
                    return EXIT_REAP_FAILED, proc.pid
            except KeyboardInterrupt:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    proc.wait(timeout=POST_KILL_DRAIN_S)
                except subprocess.TimeoutExpired:
                    pass
                note = "cancelled by interrupt"
                return 130, proc.pid
    except OSError as exc:
        note = f"failed to launch {cmd[0]!r}: {exc}"
        return 126, None
    finally:
        _finish_pipe_readers(proc, readers)
        if note:
            _append_artifact_note(stderr_path, note)
        if code == EXIT_ARTIFACT_LIMIT or artifact_exceeded.is_set():
            _cap_artifacts(stdout_path, stderr_path)


def _start_pipe_readers(
    proc: object, stdout_path: Path, stderr_path: Path,
    exceeded: threading.Event,
) -> list[threading.Thread]:
    readers: list[threading.Thread] = []
    for stream, path in (
        (getattr(proc, "stdout", None), stdout_path),
        (getattr(proc, "stderr", None), stderr_path),
    ):
        if stream is None:
            continue
        thread = threading.Thread(
            target=_drain_bounded_pipe, args=(stream, path, exceeded), daemon=True,
        )
        thread.start()
        readers.append(thread)
    return readers


def _drain_bounded_pipe(stream: object, path: Path, exceeded: threading.Event) -> None:
    marker = b"\n[agent_delegate: artifact truncated at configured byte limit]\n"
    written = 0
    capped = False
    try:
        with path.open("wb", buffering=0) as fh:
            read_chunk = getattr(stream, "read1", stream.read)
            while True:
                chunk = read_chunk(65536)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                if capped:
                    continue
                if written + len(chunk) <= MAX_ARTIFACT_BYTES_PER_STREAM:
                    fh.write(chunk)
                    written += len(chunk)
                    continue
                fit = max(0, MAX_ARTIFACT_BYTES_PER_STREAM - written)
                if fit:
                    fh.write(chunk[:fit])
                retained = max(0, MAX_ARTIFACT_BYTES_PER_STREAM - len(marker))
                fh.truncate(retained)
                fh.seek(retained)
                fh.write(marker[:MAX_ARTIFACT_BYTES_PER_STREAM])
                capped = True
                exceeded.set()
    except (OSError, ValueError):
        exceeded.set()


def _finish_pipe_readers(proc: object, readers: list[threading.Thread]) -> None:
    if not readers:
        return
    deadline = time.monotonic() + POST_KILL_DRAIN_S
    for reader in readers:
        reader.join(timeout=max(0.0, deadline - time.monotonic()))
    streams = (
        getattr(proc, "stdout", None), getattr(proc, "stderr", None),
    ) if proc is not None else ()
    if any(reader.is_alive() for reader in readers):
        for stream in streams:
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        for reader in readers:
            reader.join(timeout=0.1)
    for stream in streams:
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass


def _append_artifact_note(path: Path, note: str) -> None:
    payload = ("\n" + note).encode("utf-8", errors="replace")
    try:
        with path.open("ab") as fh:
            remaining = max(0, MAX_ARTIFACT_BYTES_PER_STREAM - fh.tell())
            fh.write(payload[:remaining])
    except OSError:
        pass


def _read_output(path: Path, limit: int = MAX_VALIDATE_BYTES) -> tuple[str, bool]:
    if not path.exists():
        return "", False
    size = path.stat().st_size
    truncated = size > limit
    with path.open("rb") as fh:
        if truncated:
            fh.seek(-limit, os.SEEK_END)
        data = fh.read(limit)
    return data.decode("utf-8", errors="replace"), truncated


def _artifact_limit_exceeded(stdout_path: Path, stderr_path: Path) -> bool:
    for path in (stdout_path, stderr_path):
        try:
            if path.stat().st_size > MAX_ARTIFACT_BYTES_PER_STREAM:
                return True
        except OSError:
            continue
    return False


def _cap_artifacts(stdout_path: Path, stderr_path: Path) -> None:
    marker = b"\n[agent_delegate: artifact truncated at configured byte limit]\n"
    if len(marker) > MAX_ARTIFACT_BYTES_PER_STREAM:
        marker = marker[:MAX_ARTIFACT_BYTES_PER_STREAM]
    retained = MAX_ARTIFACT_BYTES_PER_STREAM - len(marker)
    for path in (stdout_path, stderr_path):
        try:
            if path.stat().st_size <= MAX_ARTIFACT_BYTES_PER_STREAM:
                continue
            with path.open("r+b") as fh:
                fh.truncate(retained)
                fh.seek(0, os.SEEK_END)
                fh.write(marker)
        except OSError:
            continue


def _progress_stream(path: Path, target: str, stream_progress: bool) -> tuple[int, str]:
    """Read one bounded live artifact without changing its final-result role.

    stdout remains the only artifact used for result validation.  stderr is
    deliberately handled here only as an observability stream: Codex emits
    useful live notices there, and dropping them made a healthy long-running
    run look idle to a caller polling status/wait.
    """
    if not path.exists():
        return 0, ""
    try:
        size = path.stat().st_size
        # Artifacts are already hard-capped, so reading the bounded stream from
        # the beginning preserves earlier partial text across tool-heavy events.
        read_limit = min(size, MAX_ARTIFACT_BYTES_PER_STREAM)
        with path.open("rb") as fh:
            if size > read_limit:
                fh.seek(-read_limit, os.SEEK_END)
            raw = fh.read(read_limit).decode("utf-8", errors="replace")
    except OSError:
        return 0, ""
    if not stream_progress:
        return size, raw[-PROGRESS_TAIL_CHARS:]
    lines = raw.splitlines()
    if size > read_limit and lines:
        lines = lines[1:]
    delta_chunks: list[str] = []
    chunks: list[str] = []
    error_chunks: list[str] = []
    last_antigravity_step_index: object = None
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        for err in _event_error_texts(event):
            if err and (not error_chunks or err != error_chunks[-1]):
                error_chunks.append(err)
        nested = event.get("event") if isinstance(event, dict) else None
        if isinstance(nested, dict) and nested.get("type") == "message_start" \
                and delta_chunks and not delta_chunks[-1].endswith("\n"):
            delta_chunks.append("\n")
        delta = nested.get("delta") if isinstance(nested, dict) else None
        if isinstance(delta, dict) and isinstance(delta.get("text"), str):
            delta_chunks.append(delta["text"])
            continue
        # Antigravity (agy): unlike Claude, event["event"] is a string here,
        # not a dict, so it never matches the branch above. Its incremental
        # answer text lives at step_update.text_delta on agent_response steps.
        if nested == "step_update":
            step_update = event.get("step_update") if isinstance(event, dict) else None
            if isinstance(step_update, dict) and step_update.get("step_type") == "agent_response":
                text_delta = step_update.get("text_delta")
                if isinstance(text_delta, str) and text_delta:
                    step_index = step_update.get("step_index")
                    if (delta_chunks and step_index != last_antigravity_step_index
                            and not delta_chunks[-1].endswith("\n")):
                        delta_chunks.append("\n")
                    delta_chunks.append(text_delta)
                    last_antigravity_step_index = step_index
            continue
        for chunk in _event_texts(event):
            if chunk and (not chunks or chunk != chunks[-1]):
                chunks.append(chunk)
    rendered = "".join(delta_chunks) if delta_chunks else "\n".join(chunks)
    # Error text is appended regardless of which path produced the main
    # rendered content (or if neither did) so a failure is never silently
    # dropped just because it arrived outside the success-shaped events.
    if error_chunks:
        error_text = "\n".join(error_chunks)
        rendered = f"{rendered}\n{error_text}" if rendered else error_text
    # stderr is usually plain progress text.  If a target does put JSONL on
    # stderr, parse its known event shapes; otherwise preserve the raw text so
    # diagnostics never disappear merely because they were not JSON.
    if not rendered and raw.strip():
        rendered = _filter_plain_progress(raw)
    return size, rendered[-PROGRESS_TAIL_CHARS:]


def progress_snapshot(stdout_path: Path, target: str, stream_progress: bool,
                      stderr_path: Path | None = None) -> dict:
    """Return a bounded, combined live view of stdout and stderr artifacts.

    ``progress_bytes`` is the total observed bytes so polling can reliably
    detect activity whichever descriptor the target CLI uses.  Per-stream
    counts retain enough detail for tooling without conflating stdout with the
    validated final result.
    """
    stdout_bytes, stdout_tail = _progress_stream(stdout_path, target, stream_progress)
    stderr_bytes, stderr_tail = (
        _progress_stream(stderr_path, target, stream_progress)
        if stderr_path is not None else (0, "")
    )
    parts = []
    if stdout_tail:
        parts.append(stdout_tail)
    if stderr_tail:
        parts.append(stderr_tail)
    return {
        "progress_bytes": stdout_bytes + stderr_bytes,
        "stdout_progress_bytes": stdout_bytes,
        "stderr_progress_bytes": stderr_bytes,
        "progress_tail": "\n".join(parts)[-PROGRESS_TAIL_CHARS:],
        "progress_format": f"{target}-jsonl+stderr" if stream_progress else "raw+stderr",
    }


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _binary_version(binary: str) -> str | None:
    try:
        result = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (result.stdout or result.stderr).strip() or None


def _stream_to_console(path: Path, stream) -> None:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for chunk in iter(lambda: fh.read(65536), ""):
            stream.write(chunk)
    stream.flush()


def wait_retry_or_cancel(directory: Path, seconds: float) -> bool:
    """Return False when cancellation is requested during retry backoff.
    Refreshes manager_heartbeat_epoch periodically so a manager sleeping
    through retry backoff isn't misclassified as worker_died by status/cancel
    once MANAGER_STALE_S elapses. update_run_state merges under a lock, so
    this never clobbers a concurrent cancel_requested or other state field."""
    deadline = time.monotonic() + seconds
    last_heartbeat = time.monotonic()
    while time.monotonic() < deadline:
        if _read_json(directory / "state.json").get("cancel_requested"):
            return False
        now = time.monotonic()
        if now - last_heartbeat >= MANAGER_HEARTBEAT_S:
            update_run_state(directory, manager_heartbeat_epoch=time.time())
            last_heartbeat = now
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    if _read_json(directory / "state.json").get("cancel_requested"):
        return False
    update_run_state(directory, manager_heartbeat_epoch=time.time())
    return True


def execute_managed(args: argparse.Namespace, run_id: str, directory: Path) -> dict:
    caller = args.caller or infer_caller()
    model = effective_model(args)
    reasoning_effort = effective_reasoning_effort(args)
    policy_error = role_policy_error(args)
    if policy_error:
        result = {
            "run_id": run_id, "status": "failed", "exit_code": 4,
            "error_kind": "role_policy_violation", "message": policy_error,
            "caller": caller, "target": args.target, "model": model,
            "reasoning_effort": reasoning_effort,
            "role": args.role, "duration_s": 0.0,
        }
        _atomic_json(directory / "result.json", result)
        update_run_state(directory, **result)
        append_log({**base_entry(args, caller), **result, "output": policy_error})
        return result
    depth = parse_depth()
    if depth >= MAX_DEPTH:
        result = {
            "run_id": run_id, "status": "failed", "exit_code": 2,
            "error_kind": "delegation_depth_limit", "caller": caller,
            "target": args.target, "model": model,
            "reasoning_effort": reasoning_effort, "role": args.role,
            "duration_s": 0.0,
        }
        _atomic_json(directory / "result.json", result)
        update_run_state(directory, **result)
        append_log({**base_entry(args, caller), **result,
                    "output": "refused: nested delegation depth limit reached"})
        return result

    binary = args.binary or find_binary(TARGET_BINARIES.get(args.target, args.target))
    if not binary:
        result = {
            "run_id": run_id, "status": "failed", "exit_code": 3,
            "error_kind": "cli_not_found", "caller": caller,
            "target": args.target, "model": model,
            "reasoning_effort": reasoning_effort, "role": args.role,
            "duration_s": 0.0,
        }
        _atomic_json(directory / "result.json", result)
        update_run_state(directory, **result)
        append_log({**base_entry(args, caller), **result,
                    "output": f"refused: '{args.target}' CLI not found"})
        return result

    update_run_state(directory, manager_pid=os.getpid())

    original_prompt = args.prompt
    args.prompt = ROLE_PREFIX[args.role] + original_prompt
    cmd = build_command(args, binary)
    args.prompt = original_prompt
    env = {
        **os.environ,
        DEPTH_ENV: str(depth + 1),
        CALLER_ENV: args.target,
        RUN_ID_ENV: run_id,
    }
    started = time.monotonic()
    final_stdout = final_stderr = ""
    stdout_path = stderr_path = directory / "unused.log"
    exit_code = 1
    validation_errors: list[str] = []
    error_kind = next_action = None
    attempts = args.retries + 1
    launch_identity: dict[str, object] = {}

    for attempt in range(1, attempts + 1):
        if _read_json(directory / "state.json").get("cancel_requested"):
            exit_code, error_kind, next_action = 130, "cancelled", None
            break
        stdout_path = directory / f"stdout.attempt-{attempt}.log"
        stderr_path = directory / f"stderr.attempt-{attempt}.log"

        def on_started(pid: int) -> None:
            launch_identity.update(
                pid=pid, pgid=pid, process_start_token=_process_start_token(pid),
            )
            update_run_state(directory, status="running", attempt=attempt,
                             attempts=attempts, pid=pid, pgid=pid,
                             process_start_token=launch_identity["process_start_token"],
                             manager_heartbeat_epoch=time.time(),
                             stdout_path=str(stdout_path), stderr_path=str(stderr_path))

        heartbeat = [time.monotonic()]

        def manager_control() -> bool:
            now = time.monotonic()
            if now - heartbeat[0] >= MANAGER_HEARTBEAT_S:
                update_run_state(directory, manager_heartbeat_epoch=time.time())
                heartbeat[0] = now
            return bool(_read_json(directory / "state.json").get("cancel_requested"))

        exit_code, _ = run_child_to_files(
            cmd, args.cwd, env, args.timeout, stdout_path, stderr_path, on_started,
            should_cancel=manager_control,
        )
        if exit_code == EXIT_REAP_FAILED and launch_identity:
            # on_started may have failed while publishing state. Preserve the
            # identity captured before that write so later status/wait can
            # retry verified cleanup instead of losing the live process.
            update_run_state(directory, **launch_identity)
        final_stdout, stdout_truncated = _read_output(stdout_path)
        final_stderr, _ = _read_output(stderr_path)
        error_kind, next_action = classify_error(
            args, caller, exit_code, final_stdout, final_stderr,
        )
        validation_errors = validate_result(args, final_stdout, stdout_truncated) if exit_code == 0 else []
        if validation_errors:
            exit_code = 4
            error_kind = "result_validation_failed"
            next_action = "Tighten the prompt or inspect the result artifact."
        if exit_code == 0 or attempt == attempts or not is_transient(exit_code, error_kind):
            break
        if _read_json(directory / "state.json").get("cancel_requested"):
            exit_code, error_kind, next_action = 130, "cancelled", None
            break
        retry_delay = args.retry_delay * attempt
        update_run_state(directory, status="retrying", error_kind=error_kind,
                         next_retry_in_s=retry_delay)
        if not wait_retry_or_cancel(directory, retry_delay):
            exit_code, error_kind, next_action = 130, "cancelled", None
            break

    if exit_code == 0:
        status = "completed"
    elif exit_code == 124:
        status = "timed_out"
    elif exit_code == 130:
        status = "cancelled"
    elif exit_code == EXIT_REAP_FAILED:
        status = "orphaned"
    else:
        status = "failed"
    duration = round(time.monotonic() - started, 3)
    progress = progress_snapshot(
        stdout_path, args.target, bool(getattr(args, "stream_progress", False)), stderr_path,
    )
    result = {
        "run_id": run_id,
        "status": status,
        "exit_code": exit_code,
        "error_kind": error_kind,
        "next_action": next_action,
        "caller": caller,
        "target": args.target,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "role": args.role,
        "binary": str(Path(binary).resolve()),
        "binary_version": _binary_version(binary),
        "duration_s": duration,
        "attempts": attempt,
        "validation_errors": validation_errors,
        "stdout_path": str(stdout_path) if stdout_path.exists() else None,
        "stderr_path": str(stderr_path) if stderr_path.exists() else None,
        "stdout_sha256": _sha256(stdout_path),
        "stderr_sha256": _sha256(stderr_path),
        "stdout_tail": (
            progress["progress_tail"][-LOG_OUTPUT_CHARS:]
            if getattr(args, "stream_progress", False)
            else final_stdout[-LOG_OUTPUT_CHARS:]
        ),
        "stderr_tail": final_stderr[-LOG_STDERR_CHARS:],
        **progress,
        "parent_record": args.parent_record,
    }
    _atomic_json(directory / "result.json", result)
    update_run_state(directory, **result)
    entry = {**base_entry(args, caller), **result, "output": result["stdout_tail"]}
    if exit_code != 0 and final_stderr:
        entry["stderr"] = result["stderr_tail"]
    append_log(entry)
    if args.checkpoint:
        write_checkpoint(args, caller, entry)
    if args.project:
        publish_completion_event(args, caller, entry)
    return result


def write_checkpoint(args: argparse.Namespace, caller: str, entry: dict) -> None:
    """Best-effort: a checkpoint failure must never mask the child's
    result, which has already been printed/returned by the time this
    runs. Bounded timeout so a hung DB write can't hang the wrapper."""
    summary = (
        f"Delegation {entry.get('run_id', 'legacy')} to {args.target} "
        f"model={entry.get('model')} role={entry.get('role')} "
        f"status={entry.get('status')} exit={entry['exit_code']} "
        f"duration={entry['duration_s']}s: {entry['prompt']}"
    )
    cmd = [sys.executable, str(DB_SCRIPT), "checkpoint",
           "--project", args.project,
           "--goal", f"Delegate task to {args.target}",
           "--agent", caller if caller in ("claude", "codex") else "claude",
           "--summary", summary[:1000],
           "--status", "active",
           "--next-steps", (
               f"Review run {entry.get('run_id')} result and artifacts at "
               f"{entry.get('stdout_path')}; parent agent owns verification and edits"
           )]
    try:
        result = subprocess.run(cmd, cwd=ROOT, check=False, timeout=CHECKPOINT_TIMEOUT_S,
                                capture_output=True, text=True)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            print(f"[agent_delegate] warning: --checkpoint exited {result.returncode}"
                  f"{f': {detail}' if detail else ''}; the sub-agent result is unaffected",
                  file=sys.stderr)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[agent_delegate] warning: --checkpoint failed ({exc}); "
              "the sub-agent's result above is unaffected", file=sys.stderr)


def publish_completion_event(args: argparse.Namespace, caller: str, entry: dict) -> None:
    """Best-effort durable notification; never masks the worker result."""
    payload = {
        "run_id": entry.get("run_id"), "status": entry.get("status"),
        "exit_code": entry.get("exit_code"), "target": entry.get("target"),
        "role": entry.get("role"), "stdout_path": entry.get("stdout_path"),
        "stderr_path": entry.get("stderr_path"), "error_kind": entry.get("error_kind"),
    }
    command = [
        sys.executable, str(DB_SCRIPT), "event-add",
        "--type", "delegation.completed", "--project", args.project,
        "--subject", str(entry.get("run_id")),
        "--dedupe-key", f"agent-delegate:{entry.get('run_id')}:terminal",
        "--payload", json.dumps(payload, ensure_ascii=False),
        "--agent", caller if caller in ("claude", "codex") else "system",
    ]
    try:
        result = subprocess.run(
            command, cwd=ROOT, check=False, timeout=CHECKPOINT_TIMEOUT_S,
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            print(
                f"[agent_delegate] warning: completion event exited {result.returncode}"
                f"{f': {detail}' if detail else ''}", file=sys.stderr,
            )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"[agent_delegate] warning: completion event failed ({exc})", file=sys.stderr)


def build_delegate_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("target", choices=("codex", "claude", "antigravity"),
                        help="which agent to spawn as the sub-agent")
    parser.add_argument("prompt", help="task for the sub-agent")
    parser.add_argument("--cwd", default=str(ROOT),
                        help="working directory for the sub-agent (default: repo root)")
    parser.add_argument("--timeout", type=positive_int, default=900,
                        help="seconds before the sub-agent is killed (default 900)")
    parser.add_argument("--json", action="store_true",
                        help="request machine-readable output from the sub-agent")
    parser.add_argument("--caller", choices=("claude", "codex"),
                        help="who is delegating (default: inferred from environment)")
    parser.add_argument("--role", choices=ROLES, default="worker",
                        help="task role; reviewer/advisor enforce read-only critique prompts")
    parser.add_argument("--binary", default=None,
                        help="explicit target CLI path (avoids ambiguous PATH installations)")
    parser.add_argument("--background", action="store_true",
                        help=("start asynchronously and print a run id; caller must arrange "
                              "status/wait polling or a host completion trigger (no LLM push)"))
    parser.add_argument("--run-id", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--isolated", action="store_true",
                        help="ignore ambient CLI customizations/MCP and disable session persistence")
    parser.add_argument("--stream-progress", action="store_true",
                        help="emit target CLI events incrementally for managed status polling")
    parser.add_argument("--result-format", choices=("raw", "json"), default="raw",
                        help="print raw child stdout or a structured result envelope")
    parser.add_argument("--expect-regex", default=None,
                        help="fail with exit 4 unless stdout matches this regex")
    parser.add_argument("--expect-json", action="store_true",
                        help="fail with exit 4 unless stdout is valid JSON")
    parser.add_argument("--min-output-chars", type=nonnegative_int, default=0,
                        help="minimum non-whitespace output length")
    parser.add_argument("--retries", type=nonnegative_int, default=0,
                        help="retries for timeout/transient service errors")
    parser.add_argument("--retry-delay", type=nonnegative_float, default=2.0,
                        help="base retry delay in seconds")
    parser.add_argument("--parent-record", default=None,
                        help="optional ENDMEMEX record id for provenance attribution")
    # codex options
    parser.add_argument("--sandbox", default="read-only", choices=CODEX_SANDBOXES,
                        help="codex sandbox mode (default read-only)")
    # Target CLI options. Model values are intentionally not allow-listed:
    # aliases and full IDs evolve independently of this wrapper.
    parser.add_argument("--model", default=None,
                        help="target CLI model alias/full ID (Claude defaults to haiku; Codex uses its configured default)")
    parser.add_argument("--reasoning-effort", default=None,
                        help="target reasoning effort (default: medium)")
    parser.add_argument("--allowed-tools", nargs="*", default=None,
                        help="claude tools preapproved through --allowedTools")
    parser.add_argument("--available-tools", nargs="*", default=None,
                        help="exact Claude --tools availability set (defaults to allowed-tools)")
    parser.add_argument("--permission-mode", default=None,
                        help="claude --permission-mode passthrough")
    # ENDMEMEX checkpoint (opt-in DB write; Main Mac only)
    parser.add_argument("--checkpoint", action="store_true",
                        help="also record an ENDMEMEX checkpoint of this delegation")
    parser.add_argument("--project", default=None,
                        help="ENDMEMEX project label; publishes a durable terminal event and is required with --checkpoint")
    return parser


def _print_result(args: argparse.Namespace, result: dict) -> None:
    if args.result_format == "json":
        print(json.dumps(result, ensure_ascii=False))
        return
    if result.get("stdout_path"):
        _stream_to_console(Path(result["stdout_path"]), sys.stdout)
    if result["exit_code"] != 0:
        if result.get("stderr_path"):
            _stream_to_console(Path(result["stderr_path"]), sys.stderr)
        elif result.get("message") or result.get("error_kind"):
            print(f"[agent_delegate] {result.get('message') or result['error_kind']}",
                  file=sys.stderr)
        if result.get("next_action"):
            print(f"\n[agent_delegate] {result['next_action']}", file=sys.stderr)


def start_background(args: argparse.Namespace) -> int:
    # This launcher deliberately persists only an artifact/run id.  It has no
    # connection to the delegating LLM, so it cannot resume or notify that LLM
    # on completion.  The caller must have installed a poll or host trigger.
    config = _config_from_args(args)
    config["background"] = False
    run_id, directory = new_run(config, run_id=getattr(args, "run_id", None))
    manager_log = directory / "manager.log"
    try:
        with manager_log.open("a", encoding="utf-8") as manager:
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "_worker", run_id],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=manager, stderr=manager,
                start_new_session=True,
            )
    except OSError as exc:
        update_run_state(directory, status="failed", error_kind="worker_launch_failed",
                         exit_code=126, message=str(exc))
        print(json.dumps({"run_id": run_id, "status": "failed", "error": str(exc)}))
        return 126
    # Do not force status back to queued: a very fast worker may already have
    # completed between Popen() and this metadata merge.
    state = update_run_state(directory, launcher_pid=proc.pid, launcher_pgid=proc.pid,
                             manager_log=str(manager_log))
    print(json.dumps({"run_id": run_id, "status": state.get("status", "queued"),
                      "status_command": f"python3 ENDMEMEX/agent_delegate.py status {run_id}",
                      "next_action": BACKGROUND_NEXT_ACTION}))
    return 0


def worker_main(run_id: str) -> int:
    directory = run_dir(run_id)
    args = argparse.Namespace(**_read_json(directory / "request.json"))
    result = execute_managed(args, run_id, directory)
    return int(result["exit_code"])


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


def _process_start_token(pid: object) -> str | None:
    """Return an OS-derived process birth token suitable for PID-reuse checks."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    proc_stat = Path(f"/proc/{pid}/stat")
    if proc_stat.exists():
        try:
            # Linux proc(5): field 22 is the start time in clock ticks. The
            # command field may contain spaces or parentheses, so split only
            # after its final closing parenthesis; field 3 then starts at 0.
            tail = proc_stat.read_text().rsplit(")", 1)[1].split()
            return f"linux:{tail[19]}"
        except (OSError, IndexError):
            return None
    if sys.platform.startswith("linux"):
        # Linux birth identity is accepted only from procfs field 22. Do not
        # weaken PID-reuse protection with second-resolution ps output.
        return None
    if sys.platform == "darwin":
        info = _darwin_bsd_info(pid)
        if info is not None:
            return f"darwin:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}"
        return None
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            text=True, capture_output=True, timeout=2, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    started = result.stdout.strip()
    return f"ps:{started}" if result.returncode == 0 and started else None


def _group_member_pids(pgid: int, own_pid: int) -> list[int] | None:
    """Return other pids sharing `pgid`, or None when the OS cannot prove
    occupancy safely (fail-closed for callers checking for zero members)."""
    proc_root = Path("/proc")
    if sys.platform.startswith("linux"):
        if not proc_root.is_dir():
            return None
        try:
            entries = list(proc_root.iterdir())
        except OSError:
            return None
        pids: list[int] = []
        for entry in entries:
            if not entry.name.isdigit() or int(entry.name) == own_pid:
                continue
            try:
                stat_text = (entry / "stat").read_text()
            except FileNotFoundError:
                continue
            except PermissionError:
                # hidepid=invisible, or an unrelated foreign-uid process this
                # caller can't read /proc/<pid>/stat for. Our own descendants
                # are always same-uid and readable, so a pid we can't read
                # cannot be one of them -- skip it rather than failing the
                # entire enumeration closed over an unrelated process.
                continue
            except OSError:
                return None
            try:
                tail = stat_text.rsplit(")", 1)[1].split()
                if int(tail[2]) == pgid:  # proc(5) field 5: process group ID
                    pids.append(int(entry.name))
            except (ValueError, IndexError):
                return None
        return pids
    if sys.platform == "darwin":
        # PROC_PGRP_ONLY (kind 2) asks the kernel to filter by process group
        # directly, unlike PROC_ALL_PIDS (kind 1): listing every system pid
        # and then probing each candidate's pgid via proc_pidinfo fails
        # closed (returns None) as soon as ANY unrelated, ambient process on
        # the machine can be signalled but not introspected -- which is the
        # common case on a real desktop with many running apps/daemons, and
        # was observed live to make this helper return None permanently
        # regardless of the guarded run's own group state.
        PROC_PGRP_ONLY = 2
        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            proc_listpids = libproc.proc_listpids
            proc_listpids.argtypes = [
                ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int,
            ]
            proc_listpids.restype = ctypes.c_int
            needed = proc_listpids(PROC_PGRP_ONLY, pgid, None, 0)
            if needed <= 0:
                return None
            capacity = needed // ctypes.sizeof(ctypes.c_int) + 64
            buffer = (ctypes.c_int * capacity)()
            received = proc_listpids(PROC_PGRP_ONLY, pgid, buffer, ctypes.sizeof(buffer))
            if received <= 0:
                return None
            if received >= ctypes.sizeof(buffer):
                # A full buffer may be a truncated snapshot. Missing even one
                # group member would let the guard exit fail-open.
                return None
            return [
                pid for pid in buffer[:received // ctypes.sizeof(ctypes.c_int)]
                if pid > 0 and pid != own_pid
            ]
        except (AttributeError, OSError):
            return None
    return None


def _group_has_other_members(pgid: int, own_pid: int) -> bool | None:
    """Return group occupancy, or None when the OS cannot prove it safely."""
    pids = _group_member_pids(pgid, own_pid)
    if pids is None:
        return None
    return bool(pids)


def guard_main(argv: list[str]) -> int:
    """Keep a verifiable process-group leader alive until all children exit,
    bounded by GUARD_DRAIN_GRACE_S so a target's own real result is never
    withheld indefinitely."""
    command = argv[1:] if argv[:1] == ["--"] else argv
    if not command:
        return 126
    # SIGTERM is delivered to the entire process group. The guarded target
    # retains the default action after exec, while this stable leader stays
    # alive so the manager can verify group shutdown or escalate to SIGKILL.
    signal.signal(signal.SIGTERM, lambda _signum, _frame: None)
    try:
        target = subprocess.Popen(command, stdin=subprocess.DEVNULL)
    except OSError as exc:
        print(f"failed to launch guarded child {command[0]!r}: {exc}", file=sys.stderr)
        return 126
    exit_code = target.wait()
    pgid = os.getpgrp()
    own_pid = os.getpid()
    # Every real caller launches this guard with start_new_session=True,
    # which makes it both session and process-group leader (pgid == own
    # pid). That invariant is what makes it safe to SIGKILL "other members
    # of my process group" below -- they can only be descendants of the
    # guarded command tree, never ambient siblings. If that invariant
    # doesn't hold (a caller bug), the group may be shared with unrelated
    # processes; refuse to signal anything in that case rather than risk
    # killing something outside the guarded tree.
    is_group_leader = pgid == own_pid
    deadline = time.monotonic() + GUARD_DRAIN_GRACE_S
    drained = False
    while time.monotonic() < deadline:
        occupied = _group_has_other_members(pgid, own_pid)
        if occupied is False:
            drained = True
            break
        # Unknown occupancy fails closed within the grace window: the owning
        # manager's timeout or cancellation remains able to signal this
        # stable group leader.
        time.sleep(0.05)
    if not drained and not is_group_leader:
        print(
            "warning: guard is not its own process-group leader (expected "
            "start_new_session=True); refusing to signal other members of "
            "a group it does not own, so a lingering descendant may still "
            "be running",
            file=sys.stderr,
        )
    elif not drained:
        # The target already produced its real exit_code. A same-process-group
        # descendant that outlives it (e.g. a detached helper the target's own
        # CLI spawned) must not hold that result hostage for the full outer
        # --timeout, which would discard a completed answer and misreport it
        # as timed_out (as observed live: a target finished in ~30s but its
        # run was reported timed_out after burning a 120s timeout because one
        # descendant never exited). Best-effort reap the leftover members
        # directly -- never killpg(pgid, ...), which would also SIGKILL this
        # guard before it can return the target's status.
        #
        # A single kill pass is not enough: _group_member_pids can return
        # None (occupancy unverifiable, e.g. transient enumeration failure)
        # right when we go to act on it, silently skipping the kill and
        # leaving the run reported "completed" with the descendant still
        # alive. Re-enumerate and re-kill for up to POST_KILL_DRAIN_S so a
        # transient None or a fresh grandchild spawned mid-reap still gets
        # caught, and only give up -- with a note so it's visible in the
        # captured stderr artifact -- if the group truly won't confirm empty.
        reap_deadline = time.monotonic() + POST_KILL_DRAIN_S
        while time.monotonic() < reap_deadline:
            pids = _group_member_pids(pgid, own_pid)
            if pids is None:
                time.sleep(0.05)
                continue
            if not pids:
                drained = True
                break
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            time.sleep(0.05)
        if not drained:
            print(
                "warning: guard could not confirm the process group was "
                f"empty after {GUARD_DRAIN_GRACE_S + POST_KILL_DRAIN_S:.0f}s "
                "of reaping; a descendant may still be running",
                file=sys.stderr,
            )
    if exit_code < 0:
        # A negative code means the target died from a signal (Python
        # subprocess convention). SystemExit can't represent that: POSIX
        # process exit statuses are an unsigned byte, so `raise
        # SystemExit(-9)` is silently truncated to 247 instead of preserving
        # signal identity. Re-deliver the same signal to this guard itself
        # so its own OS-level death reports the identical negative
        # returncode to the parent's proc.wait(), exactly like an unguarded
        # signal-killed child would.
        signum = -exit_code
        if signum == signal.SIGTERM:
            # This guard ignores SIGTERM on itself (see above); restore the
            # default disposition or the self-signal below would do nothing.
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
        try:
            os.kill(own_pid, signum)
        except OSError:
            pass
        # Fallback only: the self-signal above terminates the process for
        # every signal in the common set. Reached only if the default action
        # for `signum` somehow wasn't fatal.
    return exit_code


def _process_matches_run(pid: object, expected_start_token: object) -> bool:
    return (
        isinstance(expected_start_token, str)
        and bool(expected_start_token)
        and _process_start_token(pid) == expected_start_token
    )


def _recover_stale_run(directory: Path, state: dict, error_kind: str) -> dict:
    """Kill an orphan only after verifying its persisted process birth token."""
    child_pid = state.get("pid")
    child_pgid = state.get("pgid")
    if _process_exists(child_pid):
        if not _process_matches_run(child_pid, state.get("process_start_token")):
            current, _ = update_run_state_if(
                directory, lambda item: item.get("status") not in TERMINAL_STATUSES,
                status="orphaned", error_kind="orphan_identity_unverified",
                next_action="Retry status/cancel where process identity can be verified.",
            )
            return current
        try:
            os.killpg(int(child_pgid), signal.SIGKILL)
        except (ProcessLookupError, ValueError, TypeError):
            pass
        except PermissionError:
            current, _ = update_run_state_if(
                directory, lambda item: item.get("status") not in TERMINAL_STATUSES,
                status="orphaned", error_kind="orphan_reap_permission_denied",
            )
            return current
        deadline = time.monotonic() + POST_KILL_DRAIN_S
        while _process_exists(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _process_exists(child_pid):
            current, _ = update_run_state_if(
                directory, lambda item: item.get("status") not in TERMINAL_STATUSES,
                status="orphaned", error_kind="orphan_reap_unconfirmed",
            )
            return current
    current, _ = update_run_state_if(
        directory, lambda item: item.get("status") not in TERMINAL_STATUSES,
        status="failed", exit_code=1, error_kind=error_kind,
        next_action="Inspect manager.log and retry the run.",
    )
    return current


def status_main(run_id: str, as_json: bool = False) -> int:
    try:
        state = read_run_state(run_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"run_id": run_id, "status": "unknown", "error": str(exc)}))
        return 3
    if state.get("status") not in TERMINAL_STATUSES:
        owner = state.get("manager_pid") or state.get("launcher_pid") or state.get("pid")
        heartbeat = state.get("manager_heartbeat_epoch")
        heartbeat_stale = (
            isinstance(heartbeat, (int, float))
            and time.time() - float(heartbeat) > MANAGER_STALE_S
        )
        ownerless_stale = (
            not owner
            and (_iso_age_seconds(state.get("created_at")) or 0) > START_PUBLICATION_GRACE_S
        )
        if (owner and not _process_exists(owner)) or heartbeat_stale:
            state = _recover_stale_run(run_dir(run_id), state, "worker_died")
        elif ownerless_stale:
            state, _ = update_run_state_if(
                run_dir(run_id), lambda item: item.get("status") not in TERMINAL_STATUSES,
                status="failed", exit_code=1, error_kind="worker_died",
                next_action="Launch ownership was never published; retry the run.",
            )
    stdout_path = state.get("stdout_path")
    stderr_path = state.get("stderr_path")
    if stdout_path or stderr_path:
        try:
            request = _read_json(run_dir(run_id) / "request.json")
        except (OSError, ValueError, json.JSONDecodeError):
            request = {}
        state.update(progress_snapshot(
            Path(str(stdout_path)) if stdout_path else Path("/dev/null"),
            str(state.get("target") or "unknown"),
            bool(request.get("stream_progress")),
            Path(str(stderr_path)) if stderr_path else None,
        ))
    if as_json:
        print(json.dumps(state, ensure_ascii=False))
    else:
        print(f"{run_id}: {state.get('status')} model={state.get('model')} "
              f"role={state.get('role')} attempt={state.get('attempt', 0)}")
    return 0


def wait_main(run_id: str, timeout: int, as_json: bool = False) -> int:
    deadline = time.monotonic() + timeout
    while True:
        try:
            state = read_run_state(run_id)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(json.dumps({"run_id": run_id, "status": "unknown", "error": str(exc)}))
            return 3
        if state.get("status") in TERMINAL_STATUSES:
            result_path = run_dir(run_id) / "result.json"
            result = _read_json(result_path) if result_path.exists() else state
            if result.get("status") not in TERMINAL_STATUSES:
                result = state
            if as_json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(f"{run_id}: {result.get('status')} exit={result.get('exit_code')}")
            return int(result.get("exit_code") or 0)
        owner = state.get("manager_pid") or state.get("launcher_pid") or state.get("pid")
        if owner and not _process_exists(owner):
            state = _recover_stale_run(run_dir(run_id), state, "worker_died")
            continue
        if time.monotonic() >= deadline:
            # A timeout is a polling deadline, not a run failure.  Return the
            # same structured live state as `status` (including stderr-driven
            # progress) so callers can immediately decide whether to keep
            # waiting or cancel instead of receiving an empty-looking result.
            try:
                request = _read_json(run_dir(run_id) / "request.json")
            except (OSError, ValueError, json.JSONDecodeError):
                request = {}
            stdout_path = state.get("stdout_path")
            if stdout_path or state.get("stderr_path"):
                state.update(progress_snapshot(
                    Path(str(stdout_path)) if stdout_path else Path("/dev/null"),
                    str(state.get("target") or "unknown"),
                    bool(request.get("stream_progress")),
                    Path(str(state["stderr_path"])) if state.get("stderr_path") else None,
                ))
            state.update(
                wait_timed_out=True,
                wait_timeout_s=timeout,
                error=f"wait timed out after {timeout}s; run is still {state.get('status')}",
            )
            print(json.dumps(state, ensure_ascii=False))
            return 124
        time.sleep(0.2)


def cancel_main(run_id: str, _emit: bool = True) -> int:
    def respond(payload: dict, code: int) -> int:
        if _emit:
            print(json.dumps(payload))
        return code

    try:
        state = read_run_state(run_id)
        directory = run_dir(run_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return respond({"run_id": run_id, "status": "unknown", "error": str(exc)}, 3)
    if state.get("status") in TERMINAL_STATUSES:
        return respond({"run_id": run_id, "status": state.get("status"),
                        "message": "run already finished"}, 0)
    state, applied = update_run_state_if(
        directory, lambda item: item.get("status") not in TERMINAL_STATUSES,
        cancel_requested=True, status="cancel_pending", error_kind=None,
    )
    if not applied:
        return respond({"run_id": run_id, "status": state.get("status"),
                        "message": "run finished while cancellation was requested"}, 0)

    if state.get("kind") == "advisor_group":
        # The group manager is not in either model child's process group.
        # Forward cancellation to the currently active child instead of
        # falsely marking the group terminal without delivering a signal.
        for key in ("advisor_run_id", "worker_run_id"):
            child_id = state.get(key)
            if not child_id:
                continue
            try:
                child_state = read_run_state(str(child_id))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if child_state.get("status") in TERMINAL_STATUSES:
                continue
            code = cancel_main(str(child_id), _emit=False)
            child_state = read_run_state(str(child_id))
            if code != 0:
                failed, _ = update_run_state_if(
                    directory, lambda item: item.get("status") not in TERMINAL_STATUSES,
                    status="failed", exit_code=code,
                    error_kind=child_state.get("error_kind") or "child_cancel_failed",
                )
                return respond({"run_id": run_id, "status": failed["status"],
                                "error": failed["error_kind"]}, code)
            if child_state.get("status") == "cancelled":
                update_run_state_if(
                    directory, lambda item: item.get("status") not in TERMINAL_STATUSES,
                    status="cancelled", exit_code=130, error_kind="cancelled",
                )
                return respond({"run_id": run_id, "status": "cancelled",
                                "child_run_id": child_id}, 0)
        return respond({"run_id": run_id, "status": "cancel_pending",
                        "message": "cancellation recorded; manager will stop before the next phase"}, 0)

    # The manager that owns the live Popen object performs signal delivery.
    # A control process must never signal a PGID persisted on disk: after a
    # crash and PID reuse that number may belong to an unrelated process.
    deadline = time.monotonic() + CANCEL_GRACE_S + POST_KILL_DRAIN_S
    while time.monotonic() < deadline:
        state = _read_json(directory / "state.json")
        if state.get("status") in TERMINAL_STATUSES:
            code = 0 if state.get("status") == "cancelled" else int(state.get("exit_code") or 0)
            return respond({"run_id": run_id, "status": state.get("status")}, code)
        owner = state.get("manager_pid") or state.get("launcher_pid")
        heartbeat = state.get("manager_heartbeat_epoch")
        heartbeat_stale = (
            isinstance(heartbeat, (int, float))
            and time.time() - float(heartbeat) > MANAGER_STALE_S
        )
        ownerless_stale = (
            not owner
            and (_iso_age_seconds(state.get("created_at")) or 0) > START_PUBLICATION_GRACE_S
        )
        if (owner and not _process_exists(owner)) or heartbeat_stale or ownerless_stale:
            failed = _recover_stale_run(directory, state, "cancel_manager_unavailable")
            terminal = failed.get("status") in TERMINAL_STATUSES
            return respond({"run_id": run_id, "status": failed.get("status"),
                            "error": failed.get("error_kind")}, 1 if terminal else 0)
        time.sleep(0.05)
    return respond({
        "run_id": run_id,
        "status": "cancel_pending",
        "message": "cancellation recorded; the run manager has not confirmed exit yet",
    }, 0)


def diagnose_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent_delegate.py diagnose")
    parser.add_argument("target", choices=("codex", "claude", "antigravity"))
    parser.add_argument("--binary")
    args = parser.parse_args(argv)
    binary = args.binary or find_binary(TARGET_BINARIES.get(args.target, args.target))
    binary_ok = bool(binary and Path(binary).is_file() and os.access(binary, os.X_OK))
    payload = {
        "target": args.target,
        "caller": infer_caller(),
        "binary": str(Path(binary).resolve()) if binary else None,
        "binary_version": _binary_version(binary) if binary_ok else None,
        "binary_executable": binary_ok,
        "delegate_depth": parse_depth(),
        "credential_note": (
            "If Claude reports Not logged in only under Codex, retry outside the sandbox."
            if args.target == "claude" else None
        ),
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if binary_ok else 3


def advisor_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="agent_delegate.py advise")
    parser.add_argument("prompt")
    parser.add_argument("--worker-model", default="sonnet")
    parser.add_argument("--advisor-model", default="opus")
    parser.add_argument("--allowed-tools", nargs="*", default=["Read", "Grep", "Glob"])
    parser.add_argument("--timeout", type=positive_int, default=900)
    parser.add_argument("--binary")
    parser.add_argument("--caller", choices=("claude", "codex"))
    parser.add_argument("--result-format", choices=("raw", "json"), default="raw")
    parser.add_argument("--project")
    parser.add_argument("--parent-record")
    args = parser.parse_args(argv)
    unsafe = sorted(set(args.allowed_tools or []) - READ_ONLY_CLAUDE_TOOLS)
    if unsafe:
        parser.error(f"advisor workflow does not allow tool(s): {', '.join(unsafe)}")
    caller = args.caller or infer_caller()
    group_id, group_dir = new_run({
        "kind": "advisor_group", "caller": caller, "target": "claude",
        "model": f"{args.worker_model}->{args.advisor_model}", "role": "orchestrator",
        "prompt": args.prompt,
    })
    print(f"[agent_delegate] advisor group run_id={group_id}", file=sys.stderr)

    def child_namespace(prompt: str, model: str, role: str) -> argparse.Namespace:
        return argparse.Namespace(
            target="claude", prompt=prompt, cwd=str(ROOT), timeout=args.timeout,
            json=False, caller=caller, role=role, binary=args.binary,
            background=False, result_format="raw", expect_regex=None,
            expect_json=False, min_output_chars=1, retries=1, retry_delay=2.0,
            parent_record=args.parent_record, sandbox="read-only", model=model,
            reasoning_effort=None,
            allowed_tools=args.allowed_tools, available_tools=args.allowed_tools,
            permission_mode=None,
            checkpoint=False, project=args.project,
        )

    update_run_state(group_dir, status="running", phase="worker", manager_pid=os.getpid())
    worker_args = child_namespace(args.prompt, args.worker_model, "worker")
    worker_id, worker_dir = new_run(_config_from_args(worker_args))
    update_run_state(group_dir, worker_run_id=worker_id)
    if _read_json(group_dir / "state.json").get("cancel_requested"):
        update_run_state(worker_dir, cancel_requested=True, status="cancel_pending")
    worker_result = execute_managed(worker_args, worker_id, worker_dir)
    if worker_result["status"] != "completed":
        group = {"run_id": group_id, "status": worker_result["status"],
                 "exit_code": worker_result["exit_code"],
                 "worker": worker_result, "advisor": None}
        _atomic_json(group_dir / "result.json", group)
        update_run_state(group_dir, **group)
        if args.result_format == "json":
            print(json.dumps(group, ensure_ascii=False))
        else:
            _print_result(worker_args, worker_result)
        return int(worker_result["exit_code"])

    if _read_json(group_dir / "state.json").get("cancel_requested"):
        group = {"run_id": group_id, "status": "cancelled", "exit_code": 130,
                 "worker_run_id": worker_id, "worker": worker_result, "advisor": None}
        _atomic_json(group_dir / "result.json", group)
        update_run_state(group_dir, **group)
        if args.result_format == "json":
            print(json.dumps(group, ensure_ascii=False))
        else:
            _stream_to_console(Path(worker_result["stdout_path"]), sys.stdout)
            print("\n[agent_delegate] advisor phase cancelled before launch", file=sys.stderr)
        return 130

    worker_text, worker_output_truncated = _read_output(
        Path(worker_result["stdout_path"]), 50000,
    )
    if worker_output_truncated:
        worker_text = (
            "[truncated: showing only the last 50000 bytes of worker output]\n"
            + worker_text
        )
    advisor_prompt = (
        f"Original task:\n{args.prompt}\n\nWorker ({args.worker_model}) findings:\n"
        f"{worker_text}\n\nReview these findings. Do not perform edits."
    )
    update_run_state(group_dir, status="running", phase="advisor")
    advisor_args = child_namespace(advisor_prompt, args.advisor_model, "advisor")
    advisor_id, advisor_dir = new_run(_config_from_args(advisor_args))
    update_run_state(group_dir, advisor_run_id=advisor_id)
    if _read_json(group_dir / "state.json").get("cancel_requested"):
        update_run_state(advisor_dir, cancel_requested=True, status="cancel_pending")
    advisor_result = execute_managed(advisor_args, advisor_id, advisor_dir)
    group = {
        "run_id": group_id, "status": advisor_result["status"],
        "exit_code": advisor_result["exit_code"],
        "worker_run_id": worker_id, "advisor_run_id": advisor_id,
        "worker_output_truncated": worker_output_truncated,
        "worker": worker_result, "advisor": advisor_result,
    }
    _atomic_json(group_dir / "result.json", group)
    update_run_state(group_dir, **group)
    if args.result_format == "json":
        print(json.dumps(group, ensure_ascii=False))
    else:
        if advisor_result["status"] != "completed":
            _stream_to_console(Path(worker_result["stdout_path"]), sys.stdout)
            print("\n[agent_delegate] advisor review failed; worker findings above are unreviewed",
                  file=sys.stderr)
        _print_result(advisor_args, advisor_result)
    return int(advisor_result["exit_code"])


def management_main(argv: list[str]) -> int | None:
    if not argv:
        return None
    command = argv[0]
    if command == "_guard":
        return guard_main(argv[1:])
    if command == "_worker" and len(argv) == 2:
        return worker_main(argv[1])
    if command == "diagnose":
        return diagnose_main(argv[1:])
    if command == "advise":
        return advisor_main(argv[1:])
    if command in {"status", "wait", "cancel"}:
        parser = argparse.ArgumentParser(prog=f"agent_delegate.py {command}")
        parser.add_argument("run_id")
        if command == "status":
            parser.add_argument("--json", action="store_true")
        elif command == "wait":
            parser.add_argument("--timeout", type=positive_int, default=900)
            parser.add_argument("--json", action="store_true")
        parsed = parser.parse_args(argv[1:])
        if command == "status":
            return status_main(parsed.run_id, parsed.json)
        if command == "wait":
            return wait_main(parsed.run_id, parsed.timeout, parsed.json)
        return cancel_main(parsed.run_id)
    return None


def main() -> int:
    managed = management_main(sys.argv[1:])
    if managed is not None:
        return managed
    parser = build_delegate_parser()
    args = parser.parse_args()

    if args.checkpoint and not args.project:
        parser.error("--checkpoint requires --project")
    if args.background:
        return start_background(args)
    config = _config_from_args(args)
    run_id, directory = new_run(config, run_id=getattr(args, "run_id", None))
    result = execute_managed(args, run_id, directory)
    _print_result(args, result)
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
