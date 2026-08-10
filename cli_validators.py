"""argparse `type=` validators for endeavor_db.py's CLI parser.

Phase 5 slice: none of the three is a monkeypatch target (grep-confirmed
against test_endeavor_db.py), so this is a plain re-export, not a
forwarding wrapper. The one real consequence of this move is declared and
handled in the same commit: each function's `__module__` changes from
"endeavor_db" to "cli_validators", and test_compatibility_contract.py's
`_stable()` renders a parser action's `type=` callable as
`f"{value.__module__}.{value.__qualname__}"` -- so `endeavor_db_parser_sha256`
in phase0_golden_contract.json must be (and was) regenerated alongside this
move, verified via a single-token payload diff before regenerating, not
discovered as a mystery failure.
"""
from __future__ import annotations

import argparse


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def pack_budget(value: str) -> int:
    parsed = int(value)
    if not 500 <= parsed <= 50_000:
        raise argparse.ArgumentTypeError("must be between 500 and 50000")
    return parsed


def nonempty_text(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return value
