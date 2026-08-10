"""Pure FTS query-term, alias-expansion, and typo-correction primitives.

None of these are monkeypatch seams (confirmed by grep against
test_endeavor_db.py: no mock.patch.object and no direct `db.<name> = ...`
assignment references any name here), so endeavor_db.py re-exports them
directly rather than through forwarding wrappers -- the same treatment
config.py's constants get, not activity.py/documents.py's function wrappers.

The one exception is typo_corrected_terms, which needs `table_exists` to
check whether the vocabulary table exists yet. It takes that as an injected
function rather than importing db_connection/endeavor_db itself, mirroring
db_connection.py's own `table_exists_fn=` pattern.
"""
from __future__ import annotations

import re
import sqlite3
import unicodedata
from typing import Callable

QUERY_ALIASES = {
    "วิธีแก้": ("fix", "fixed", "resolution", "solution"),
    "แก้": ("fix", "fixed", "resolution"),
    "แก้ไข": ("fix", "fixed", "resolution"),
    "วิธีเทรน": ("train", "training"),
    "เทรน": ("train", "training"),
    "ฝึก": ("train", "training"),
    "สาเหตุ": ("cause", "root cause", "mechanism"),
    "ทดสอบ": ("test", "verify", "validation", "regression"),
    "ส่งต่องาน": ("handoff", "checkpoint", "resume"),
    "ติดlimit": ("session limit", "context limit", "checkpoint"),
}
STOP_WORDS = {"วิธี", "ให้", "ทำ", "หา", "เรื่อง", "ของ", "และ", "หน่อย", "อะไร", "how", "to", "the", "a", "an"}


def fts_atom(term: str) -> str:
    clean = term.replace('"', "")
    suffix = "*" if clean.isascii() and len(clean) >= 4 else ""
    return f'"{clean}"{suffix}'


def query_terms(query: str) -> list[str]:
    # NFKC makes equivalent Unicode spellings deterministic. Keep Thai
    # combining marks (U+0E00..U+0E7F) in one query term: the v9 tokenizer
    # indexes them too, rather than silently dropping tone/vowel marks.
    normalized = unicodedata.normalize("NFKC", query)
    normalized = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", normalized).casefold()
    normalized = re.sub(r"[_-]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized.strip())
    terms = re.findall(r"[\w\u0e00-\u0e7f]+", normalized, flags=re.UNICODE)
    return [term for term in terms if term not in STOP_WORDS][:12]


def fts_expression(query: str, operator: str = "AND") -> str:
    terms = query_terms(query)
    expressions = []
    for term in terms[:12]:
        # unicode61 has no English stemmer. Prefix matching makes natural
        # queries such as "train agent" find "training agents" while Thai
        # terms remain exact (Thai suffixes are not English morphology).
        alternatives = [fts_atom(term), *(fts_atom(alias) for alias in QUERY_ALIASES.get(term, ()))]
        expressions.append(alternatives[0] if len(alternatives) == 1 else f"({' OR '.join(alternatives)})")
    return f" {operator} ".join(expressions)


def damerau_levenshtein(left: str, right: str) -> int:
    """Small, dependency-free edit distance for conservative typo recovery."""
    previous = list(range(len(right) + 1))
    previous_previous: list[int] | None = None
    for i, left_char in enumerate(left, start=1):
        current = [i]
        for j, right_char in enumerate(right, start=1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (left_char != right_char),
            ))
            if (
                previous_previous is not None and i > 1 and j > 1
                and left_char == right[j - 2] and left[i - 2] == right_char
            ):
                current[-1] = min(current[-1], previous_previous[j - 2] + 1)
        previous_previous, previous = previous, current
    return previous[-1]


def typo_corrected_terms(
    conn: sqlite3.Connection, terms: list[str], vocab_table: str,
    *, table_exists_fn: Callable[[sqlite3.Connection, str], bool],
) -> tuple[list[str], list[str]]:
    """Return only unambiguous ASCII spelling corrections with provenance.

    FTS is not semantic search: a correction is deliberately withheld for
    Thai, IDs, numbers, short tokens, and ties. The row-vocab table keeps the
    candidate source bounded to indexed terms without materializing documents.
    """
    if not table_exists_fn(conn, vocab_table):
        return terms, []
    corrected = list(terms)
    reasons: list[str] = []
    for index, term in enumerate(terms):
        if not re.fullmatch(r"[a-z]+", term) or len(term) < 5:
            continue
        max_distance = 1 if len(term) <= 7 else 2
        rows = conn.execute(
            f"SELECT term FROM {vocab_table} WHERE length(term) BETWEEN ? AND ?",
            (len(term) - max_distance, len(term) + max_distance),
        ).fetchall()
        # A known spelling is never a typo. Another query word may have made
        # the full AND expression miss, but changing this exact term would be
        # surprising and can silently broaden a correct query (failure ->
        # failures, for example).
        if any(row["term"] == term for row in rows):
            continue
        options: list[tuple[int, str]] = []
        for row in rows:
            candidate = row["term"]
            if not candidate.isascii() or not candidate.isalpha():
                continue
            distance = damerau_levenshtein(term, candidate)
            if 0 < distance <= max_distance:
                options.append((distance, candidate))
        options.sort()
        if not options or (len(options) > 1 and options[0][0] == options[1][0]):
            continue
        distance, candidate = options[0]
        corrected[index] = candidate
        reasons.append(f"typo:{term}→{candidate}:d={distance}")
    return corrected, reasons


def detect_intent(query: str) -> str:
    lower = query.lower()
    # This phrase names a documented training failure class. The project
    # memory also contains many incidents using the same words, so classify it
    # before generic bug terms and prefer the reusable training guidance.
    if "silent failure" in lower or "fail→detect" in lower or "fail->detect" in lower:
        return "training_method"
    if any(word in lower for word in ("bug", "แก้", "fix", "root cause", "สาเหตุ")):
        return "bug_fix"
    if any(word in lower for word in ("train", "training", "เทรน", "ฝึก", "prompt")):
        return "training_method"
    if any(word in lower for word in ("test", "verify", "regression", "ทดสอบ")):
        return "testing"
    if any(word in lower for word in ("checkpoint", "handoff", "resume", "limit", "ส่งต่อ")):
        return "checkpoint_resume"
    return "general"
