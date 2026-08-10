#!/usr/bin/env python3
"""Deterministic long-running CLI fixture for managed lifecycle integration tests."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time


if "--version" in sys.argv:
    print("fake-agent-cli 1.0")
    raise SystemExit(0)

print(json.dumps({
    "type": "stream_event",
    "event": {"type": "message_start"},
}), flush=True)
print(json.dumps({
    "type": "stream_event",
    "event": {"type": "content_block_delta", "delta": {"text": "fixture progress"}},
}), flush=True)

if os.environ.get("FAKE_AGENT_MODE") == "self-sigterm":
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(5)  # unreachable: default SIGTERM disposition kills us first

if os.environ.get("FAKE_AGENT_MODE") in {
    "spawn-descendant-exit", "spawn-stubborn-descendant-exit",
}:
    stubborn = os.environ["FAKE_AGENT_MODE"] == "spawn-stubborn-descendant-exit"
    child_code = (
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
        if stubborn else "import time; time.sleep(30)"
    )
    descendant = subprocess.Popen(
        [sys.executable, "-c", child_code], stdin=subprocess.DEVNULL,
        stdout=None if stubborn else subprocess.DEVNULL,
        stderr=None if stubborn else subprocess.DEVNULL,
    )
    print(json.dumps({
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {
            "text": f" descendant:{descendant.pid}",
        }},
    }), flush=True)
    raise SystemExit(0)

while True:
    time.sleep(0.1)
