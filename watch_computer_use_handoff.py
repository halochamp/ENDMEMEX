#!/usr/bin/env python3
"""Launch one Codex follow-up when Claude hands a configured session back.

Set ``ENDMEMEX_HANDOFF_PROJECT`` and ``ENDMEMEX_HANDOFF_SESSION`` in the
supervising process. The watcher exits with code 2 when either is missing.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from config import ROOT

HERE = Path(__file__).resolve().parent
DB = HERE / "endeavor_db.py"
STATE = HERE / ".computer_use_handoff_watch.json"
CODEX = Path(shutil.which("codex") or Path.home() / ".local" / "bin" / "codex")
PROJECT = os.environ.get("ENDMEMEX_HANDOFF_PROJECT", "").strip()
SESSION = os.environ.get("ENDMEMEX_HANDOFF_SESSION", "").strip()


def handoff() -> dict:
    result = subprocess.run(
        ["python3", str(DB), "handoff", "--project", PROJECT, "--json"],
        cwd=ROOT, text=True, capture_output=True, check=True,
    )
    return json.loads(result.stdout)


def read_state() -> dict:
    # A sync service can leave a partially propagated/truncated file mid-write;
    # a bad state file must not crash this LaunchAgent every 60s forever.
    if not STATE.exists():
        return {}
    try:
        state = json.loads(STATE.read_text())
        return state if isinstance(state, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def write_state(sequence: int) -> None:
    # Write-then-rename so a crash/interrupt mid-write never leaves a
    # half-written STATE file for read_state() to trip over next run.
    tmp = STATE.with_suffix(f".json.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps({"sequence": sequence}, indent=2) + "\n")
        os.replace(tmp, STATE)
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    if not PROJECT or not SESSION:
        return 2
    data = handoff()
    if not isinstance(data, dict):
        return 1
    checkpoint = data.get("checkpoint")
    if checkpoint is None:
        checkpoint = {}
    elif not isinstance(checkpoint, dict):
        return 1
    if checkpoint.get("session_id") != SESSION or checkpoint.get("agent") != "claude":
        return 0
    try:
        sequence = int(checkpoint.get("sequence", 0))
    except (TypeError, ValueError):
        return 1
    next_steps = checkpoint.get("next_steps", "")
    if "Codex" not in next_steps:
        return 0
    previous = read_state()
    try:
        previous_sequence = int(previous.get("sequence", 0))
    except (TypeError, ValueError):
        previous_sequence = 0
    if sequence <= previous_sequence:
        return 0
    prompt = (
        "A Claude checkpoint has handed the computer-use plan back to Codex. "
        f"Run `python3 endeavor_db.py handoff --project {PROJECT} --json`, "
        "read the latest handoff and its referenced plan files, then perform only the next "
        "Codex-owned work package. Follow AGENT.md, use project-memory checkpoints, "
        "do not run live GUI or LLM/GPU tests, and do not commit unrelated changes."
    )
    try:
        result = subprocess.run(
            [str(CODEX), "exec", "-C", str(ROOT), "-s", "workspace-write", prompt],
            cwd=ROOT, check=False,
        )
    except OSError:
        # Launch itself failed (missing/unexecutable binary) — do NOT mark
        # this sequence as handled, or the handoff is lost forever; the
        # next 60s tick will retry it once the binary is fixed.
        return 1
    if result.returncode != 0:
        # Starting the CLI is not success: authentication, configuration,
        # or child failures must leave the handoff unacknowledged for retry.
        return result.returncode or 1
    write_state(sequence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
