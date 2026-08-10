"""argparse `type=` validators for agent_delegate.py's CLI parser.

Phase 5 slice, agent_delegate.py side: none of the three is a monkeypatch
target (grep-confirmed against test_agent_delegate.py), so this is a plain
re-export, not a forwarding wrapper. Each function's `__module__` changes
from "agent_delegate" to "delegate_cli_validators", which moves
`agent_delegate_contract_sha256_by_python_minor` in
phase0_golden_contract.json (one hash per Python minor) -- regenerated in
the same commit, verified via a single-token payload diff first.
"""
from __future__ import annotations

import argparse


def nonnegative_int(value: str) -> int:
    try:
        ivalue = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be an integer, got {value!r}")
    if ivalue < 0:
        raise argparse.ArgumentTypeError(f"must be zero or positive, got {value!r}")
    return ivalue


def nonnegative_float(value: str) -> float:
    try:
        fvalue = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a number, got {value!r}")
    if fvalue < 0:
        raise argparse.ArgumentTypeError(f"must be zero or positive, got {value!r}")
    return fvalue


def positive_int(value: str) -> int:
    try:
        ivalue = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be an integer, got {value!r}")
    if ivalue <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}")
    return ivalue
