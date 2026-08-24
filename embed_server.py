"""embed_server.py — lazy-loaded MiniLM embedding companion for ENDMEMEX
(127.0.0.1:8770).

This process's own lifecycle is the embedding model's "warm" state.
endeavor_db.py spawns it on
first embedding need (ingest or `query --semantic`) and talks to it over
HTTP; the model loads once at process startup and the process exits itself
after IDLE_TIMEOUT_SEC of no requests to release RAM. If this companion
can't be reached, endeavor_db.py's semantic pass is silently skipped and
lexical FTS search keeps working — same graceful-degrade shape as VOX's
F5-TTS engine falling back to the "say" engine.
"""
from __future__ import annotations

import logging
import os
import threading
import time

os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field
import uvicorn

from embed_config import EMBED_DIM, EMBED_PORT, IDLE_TIMEOUT_SEC, MAX_TEXTS_PER_REQUEST, MODEL_NAME

log = logging.getLogger("embed_server")

_MODEL = None
_ACTIVITY_LOCK = threading.Lock()
_LAST_ACTIVITY_AT: float | None = None
_ACTIVE_REQUESTS = 0
_KEEP_WARM = IDLE_TIMEOUT_SEC <= 0

# This is a localhost service, but another local process must not be able to
# turn one request into an unbounded allocation before MiniLM receives it.
MAX_CHARS_PER_TEXT = 20_000
MAX_TOTAL_CHARS_PER_REQUEST = 200_000


def _load_model() -> None:
    global _MODEL, _LAST_ACTIVITY_AT
    from sentence_transformers import SentenceTransformer

    # The companion is an optional local capability. Never turn a first query
    # into an implicit network download: use the Hugging Face cache or fail
    # fast so endeavor_db.py can keep serving lexical FTS-only search.
    _MODEL = SentenceTransformer(MODEL_NAME, local_files_only=True)
    with _ACTIVITY_LOCK:
        _LAST_ACTIVITY_AT = time.monotonic()
    log.info("embed_server ready (%s)", MODEL_NAME)


def _begin_activity() -> None:
    global _ACTIVE_REQUESTS, _LAST_ACTIVITY_AT
    with _ACTIVITY_LOCK:
        _ACTIVE_REQUESTS += 1
        _LAST_ACTIVITY_AT = time.monotonic()


def _finish_activity() -> None:
    global _ACTIVE_REQUESTS, _LAST_ACTIVITY_AT
    with _ACTIVITY_LOCK:
        _ACTIVE_REQUESTS = max(0, _ACTIVE_REQUESTS - 1)
        _LAST_ACTIVITY_AT = time.monotonic()


def _idle_expired(now: float, timeout_sec: float = IDLE_TIMEOUT_SEC) -> bool:
    with _ACTIVITY_LOCK:
        return (
            not _KEEP_WARM
            and timeout_sec > 0
            and
            _MODEL is not None
            and _ACTIVE_REQUESTS == 0
            and _LAST_ACTIVITY_AT is not None
            and now - _LAST_ACTIVITY_AT >= timeout_sec
        )


def _idle_watchdog() -> None:
    while True:
        time.sleep(min(30, max(1, IDLE_TIMEOUT_SEC)))
        if _idle_expired(time.monotonic()):
            log.info("embed_server idle for %ss; exiting to release MiniLM RAM", IDLE_TIMEOUT_SEC)
            os._exit(0)


class EmbedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    texts: list[str] = Field(max_length=MAX_TEXTS_PER_REQUEST)


class WarmModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keep_warm: bool


api = FastAPI(title="ENDEAVOR embedding companion")


@api.on_event("startup")
def startup() -> None:
    _load_model()
    threading.Thread(target=_idle_watchdog, name="embed-idle-watchdog", daemon=True).start()


@api.get("/health")
def get_health():
    return {
        "status": "ready" if _MODEL is not None else "loading",
        "model": MODEL_NAME,
        "dim": EMBED_DIM,
        "keep_warm": _KEEP_WARM,
    }


@api.post("/embed")
def embed(body: EmbedRequest):
    if any(len(text) > MAX_CHARS_PER_TEXT for text in body.texts):
        raise HTTPException(
            status_code=422,
            detail={"reason": "text_too_large", "max_chars_per_text": MAX_CHARS_PER_TEXT},
        )
    if sum(map(len, body.texts)) > MAX_TOTAL_CHARS_PER_REQUEST:
        raise HTTPException(
            status_code=422,
            detail={"reason": "request_too_large", "max_total_chars": MAX_TOTAL_CHARS_PER_REQUEST},
        )
    if _MODEL is None:
        raise HTTPException(status_code=503, detail={"reason": "model_loading"})
    if not body.texts:
        return {"embeddings": []}
    _begin_activity()
    try:
        # One embedding per input text, same order — callers rely on this to
        # zip results back onto their own row/text lists.
        vectors = _MODEL.encode(
            body.texts, normalize_embeddings=True, show_progress_bar=False, convert_to_numpy=True
        )
        return {"embeddings": vectors.tolist()}
    except Exception as exc:
        log.exception("embed_server encode failed")
        raise HTTPException(status_code=500, detail={"reason": "encode_failed", "message": str(exc)[:200]}) from exc
    finally:
        _finish_activity()


@api.post("/warm-mode")
def set_warm_mode(body: WarmModeRequest):
    global _KEEP_WARM
    with _ACTIVITY_LOCK:
        _KEEP_WARM = body.keep_warm
        # A transition back to normal idle mode starts a fresh timeout rather
        # than immediately killing a model that may have been warm for hours.
        global _LAST_ACTIVITY_AT
        _LAST_ACTIVITY_AT = time.monotonic()
    return {"status": "ready" if _MODEL is not None else "loading", "keep_warm": _KEEP_WARM}


if __name__ == "__main__":
    uvicorn.run(api, host="127.0.0.1", port=EMBED_PORT, log_level="warning")
