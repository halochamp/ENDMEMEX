"""Shared constants for the MiniLM embedding companion (embed_server.py) and
endeavor_db.py's semantic-search client.

No heavy imports here — endeavor_db.py stays standard-library only and reads
these constants without ever importing sentence_transformers/torch itself.
"""
from __future__ import annotations

import os

EMBED_PORT = 8770
EMBED_BASE_URL = f"http://127.0.0.1:{EMBED_PORT}"
MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
EMBED_DIM = 384
# Hard server-side cap (embed_server.py's EmbedRequest.texts Field(max_length=...)).
# config.py's EMBED_BATCH_SIZE must never exceed this -- a client batch larger
# than the server accepts gets a 422, which the client's broad exception
# handler folds into the same generic "embedding_request_failed" reason as a
# transient network error, silently and permanently failing that batch.
MAX_TEXTS_PER_REQUEST = 128
# 1 hour — release MiniLM RAM after this much inactivity. Env-overridable
# only to make the idle-watchdog itself testable live in seconds instead of
# an hour; not meant to be tuned in normal use.
try:
    IDLE_TIMEOUT_SEC = int(os.environ.get("ENDEAVOR_EMBED_IDLE_TIMEOUT_SEC", "3600"))
except ValueError:
    IDLE_TIMEOUT_SEC = 3600
