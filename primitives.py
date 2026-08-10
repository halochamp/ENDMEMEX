"""Small stdlib-only pure helpers shared across ENDMEMEX modules.

None of these are monkeypatch targets in test_endeavor_db.py -- they are
plain value transforms with no I/O beyond stdlib -- so endeavor_db.py
re-exports them directly rather than through a forwarding wrapper.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from config import MEMORY_RELATIONS, SQL_BATCH_SIZE

MEMORY_ID_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def normalize_memory_id(value: str) -> str:
    record_id = value.strip().upper()
    if not MEMORY_ID_RE.fullmatch(record_id):
        raise ValueError(
            "record ID must be uppercase segments joined by hyphens, for example AUDIT-MEM-001"
        )
    return record_id


def parse_relation_spec(value: str) -> tuple[str, str]:
    relation, separator, target_id = value.partition(":")
    relation = relation.strip().lower()
    if not separator or relation not in MEMORY_RELATIONS:
        raise ValueError(f"relation must be RELATION:TARGET where RELATION is one of {MEMORY_RELATIONS}")
    return relation, normalize_memory_id(target_id)


def parse_feedback_result_id(value: str) -> int | str:
    value = value.strip()
    return int(value) if value.isdigit() else normalize_memory_id(value)


def batched(values: Iterable[Any], size: int = SQL_BATCH_SIZE) -> Iterable[list[Any]]:
    batch: list[Any] = []
    for value in values:
        batch.append(value)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
