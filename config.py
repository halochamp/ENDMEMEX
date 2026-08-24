"""Immutable paths and operational limits shared by ENDMEMEX modules.

This module is deliberately stdlib-only and import-side-effect free.  The
legacy ``endeavor_db`` module imports these names explicitly and remains the
compatibility façade for callers that patch its globals.
"""
from pathlib import Path

from embed_config import MAX_TEXTS_PER_REQUEST

HERE = Path(__file__).resolve().parent


def _workspace_root(here: Path) -> Path:
    """Return the Git workspace for nested and standalone ENDMEMEX layouts.

    Main development keeps ENDMEMEX inside a monorepo, while the public
    release makes ENDMEMEX itself the repository root. Prefer the standalone
    repository when both locations are Git worktrees: a clone may live inside
    an unrelated parent Git workspace.
    """
    for candidate in (here, here.parent):
        if (candidate / ".git").exists():
            return candidate
    # An unpacked source tree has no Git metadata. Keep the historical parent
    # fallback so callers retain the same workspace-relative behavior.
    return here.parent


ROOT = _workspace_root(HERE)
DEFAULT_DB = HERE / "endeavor_memory.sqlite3"
SCHEMA_PATH = HERE / "schema.sql"
SCHEMA_VERSION = "12"
# Bump INDEX_VERSION whenever chunk boundaries change so tracked documents are
# re-ingested instead of silently retaining old oversized rows.
INDEX_VERSION = "7"

# The multilingual MiniLM companion truncates inputs at 128 tokens. Thai
# reaches that limit at roughly 450 characters, so keep a small safety margin.
MAX_CHUNK_CHARS = 500
RESULT_CANDIDATES = 30
SEMANTIC_FULL_SCAN_LIMIT = 20_000
SEMANTIC_CHUNKS_PER_RECORD_LIMIT = 20
ANN_HELPER_PATH = HERE / "ann_index.py"
ANN_QUERY_TIMEOUT_SEC = 8
ANN_BUILD_TIMEOUT_SEC = 300
ANN_CANDIDATE_LIMIT = 200

EMBED_LOG_PATH = HERE / "embed_server.log"
EMBED_START_LOCK_PATH = HERE / ".embed_start.lock"
EMBED_HEALTH_TIMEOUT_SEC = 0.3  # opportunistic check must not stall a plain query
EMBED_STARTUP_TIMEOUT_SEC = 30
EMBED_REQUEST_TIMEOUT_SEC = 60
# Must never exceed embed_config.MAX_TEXTS_PER_REQUEST -- see its comment for
# why a larger client batch silently and permanently fails.
EMBED_BATCH_SIZE = MAX_TEXTS_PER_REQUEST
EMBED_PYTHON_ENV = "ENDEAVOR_EMBED_PYTHON"
EMBED_REQUIRED_MODULES = ("fastapi", "uvicorn", "sentence_transformers", "pydantic")
EMBED_PYTHON_PROBE_TIMEOUT_SEC = 5

MAX_CHECKPOINTS = 500  # per session; pinned checkpoints don't count against this
MAX_TOTAL_CHECKPOINTS = 10_000  # global ceiling; open sessions' newest checkpoints and pinned checkpoints are exempt
# Advisory only -- pinned rows are NEVER auto-pruned or blocked from growing past
# this (see prune_checkpoints_globally), so nothing enforces it. It only
# triggers pinned_checkpoint_warning() to surface a nudge at the point a pin
# happens (and in `stats`) so unbounded growth via over-pinning is visible
# instead of silent, the same failure mode MAX_TOTAL_CHECKPOINTS exists to prevent.
MAX_PINNED_CHECKPOINTS_WARN = 1_000
MAX_ACTIVITY_LOG_ROWS = 2_000
PRESENCE_STALE_SEC = 1800  # 2x the ~15min checkpoint cadence -- a row older than this is shown but flagged, never trusted as "currently working"
PRESENCE_ROW_MAX_AGE_DAYS = 3  # discard any abandoned/stopped presence row after this long; live agents heartbeat far more often
SIDECAR_TEMP_MAX_AGE_SEC = 600  # well past any live publisher's write+replace, so reaping cannot race one
PRESENCE_DIR = HERE / ".presence"
SYNC_FRESHNESS_DIR = HERE / ".sync_freshness"
SQL_BATCH_SIZE = 500
MAX_MEMORY_CONTEXT_RECORDS = 1_000
PACK_DEFAULT_BUDGET_CHARS = 6_000

MEMORY_RECORD_TYPES = ("audit", "fix", "verification", "decision", "knowledge")
MEMORY_RECORD_STATUSES = ("open", "current", "resolved", "accepted")
MEMORY_ACTION_STATES = ("actionable", "deferred", "blocked", "nonactionable", "done")
MEMORY_RELATIONS = ("references", "resolves", "verifies", "supersedes", "contradicts", "duplicates")
KNOWLEDGE_CATEGORIES = (
    "agent_training", "debugging", "testing", "architecture",
    "session_history", "project_memory", "documentation",
)
KNOWLEDGE_FTS_IDENTITY_EXPRESSION = "{tags}: (" + " OR ".join(KNOWLEDGE_CATEGORIES) + ")"

SEED_SOURCES = (
    (HERE / "README.md", "ENDMEMEX", "documentation"),
    (HERE / "ENDMEMEX_USER_MANUAL.md", "ENDMEMEX", "documentation"),
    (HERE / "AGENT.md", "ENDMEMEX", "documentation"),
)
