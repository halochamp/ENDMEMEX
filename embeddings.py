"""Embedding vector codec and pure identity/error-classification helpers.

Scoped deliberately narrow. The companion HTTP client chain
(_embed_health_probe through embed_texts, ensure_embed_server, spawn/resolve
Python interpreter discovery) is NOT here. That chain has deep internal call coupling
between monkeypatched functions (_embed_health calls _embed_health_probe,
both patched; ensure_embed_server composes _embed_health/embed_start_lock/
_spawn_embed_server, all patched) where moving a caller without moving its
callee -- or vice versa -- silently breaks a patch point's effect on real
behavior. This slice only takes functions with zero internal calls into that
patched chain, verified by grep against test_endeavor_db.py.
"""
from __future__ import annotations

import hashlib
import struct
from typing import Any

from embed_config import EMBED_DIM, MODEL_NAME as EMBED_MODEL_NAME


def embedding_hash(content: str) -> str:
    # Prefixing with the model name means a future model swap automatically
    # invalidates every stored hash — no separate migration/version bump needed.
    return hashlib.sha256(f"{EMBED_MODEL_NAME}:{content}".encode("utf-8")).hexdigest()


def pack_embedding(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}e", *vector)


def valid_embedding_blob(blob: bytes | None) -> bool:
    return isinstance(blob, bytes) and len(blob) == EMBED_DIM * 2


def unpack_embedding(blob: bytes) -> list[float]:
    if not valid_embedding_blob(blob):
        raise ValueError(f"embedding blob must be exactly {EMBED_DIM * 2} bytes")
    return list(struct.unpack(f"<{EMBED_DIM}e", blob))


def embed_identity_matches(health: dict[str, Any] | None) -> bool:
    return bool(
        health
        and health.get("model") == EMBED_MODEL_NAME
        and health.get("dim") == EMBED_DIM
    )


def localhost_permission_denied(error: dict[str, Any] | None) -> bool:
    if not error:
        return False
    message = str(error.get("message", "")).lower()
    return bool(
        error.get("type") == "PermissionError"
        or error.get("errno") in (1, 13)
        or "operation not permitted" in message
        or "permission denied" in message
    )
