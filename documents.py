"""Pure Markdown chunking, classification, and metadata extraction.

Every function here takes its size limits as parameters rather than reading
a module global (e.g. the chunk-size cap) directly. That keeps the existing
`endeavor_db.py` facade functions -- which tests patch via
`mock.patch.object(endeavor_db, "MAX_CHUNK_CHARS", ...)` or a direct
`endeavor_db.MAX_CHUNK_CHARS = ...` assignment -- as the real, load-bearing
seam: a facade wrapper resolves its own patchable default and passes it in
explicitly, so a patch on `endeavor_db.MAX_CHUNK_CHARS` still controls real
chunking behavior after this extraction.

Document *discovery* (matching `sync_tracked.py`'s tracked-document list) and
ingestion *orchestration* (writing chunks to SQLite) are deliberately not
here. Discovery stays coupled to `sync_tracked.py`, while DB-writing logic
(`ingest_markdown`, `prune_documents`) stays in `endeavor_db.py` so this
module remains pure and independently testable.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class MarkdownChunk:
    heading: str
    title: str
    content: str
    line_start: int
    line_end: int


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
# CommonMark fenced block: up to three leading spaces, then 3+ backticks or
# tildes. A backtick opener's info string may not itself contain a backtick.
_FENCE_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")


def iter_heading_lines(lines: list[str]) -> Iterable[tuple[int, int, str]]:
    """Yield ``(line_index, level, name)`` for real Markdown headings only.

    A ``# comment`` inside a fenced code block is shell/Python syntax, not a
    heading. Parsing it as one did three things: truncated the enclosing real
    section at the fence, indexed the code as a standalone chunk titled with
    the comment, and -- worst -- rewrote the shared breadcrumb stack in
    ``markdown_chunks``, so every LATER real heading inherited the comment as
    its parent. 48 of 147 tracked documents contained such lines.
    """
    fence: str | None = None
    for index, line in enumerate(lines):
        match = _FENCE_RE.match(line)
        if fence is None:
            if match and not (match.group("fence")[0] == "`" and "`" in match.group("info")):
                fence = match.group("fence")
            elif (heading := _HEADING_RE.match(line)) is not None:
                yield index, len(heading.group(1)), heading.group(2).strip()
        elif (
            match
            and match.group("fence")[0] == fence[0]
            and len(match.group("fence")) >= len(fence)
            and not match.group("info").strip()
        ):
            # Only a closing fence of the same character, at least as long as
            # the opener and with no info string, ends the block.
            fence = None


def document_title(text: str, fallback: str) -> str:
    """First real heading of a document, ignoring fenced code blocks."""
    for _index, _level, name in iter_heading_lines(text.splitlines()):
        return name
    return fallback


def grapheme_safe_split_index(text: str, limit: int) -> int:
    """Return a split at or before *limit* that does not strand an extender.

    Python's standard library does not expose the full Unicode grapheme-break
    algorithm, but Markdown ingestion chiefly needs to keep combining marks,
    variation selectors, emoji modifiers, and ZWJ sequences attached to their
    base. Walking the rare boundary backwards preserves the hard size cap
    while preventing Thai tone marks (and the common emoji cases) from
    starting the next chunk.
    """
    index = min(limit, len(text))
    if index >= len(text):
        return index

    def extends_grapheme(char: str) -> bool:
        codepoint = ord(char)
        return (
            unicodedata.category(char).startswith("M")
            or 0xFE00 <= codepoint <= 0xFE0F
            or 0xE0100 <= codepoint <= 0xE01EF
            or 0x1F3FB <= codepoint <= 0x1F3FF
        )

    original_index = index
    while index > 0:
        if extends_grapheme(text[index]) or text[index] == "\u200d":
            index -= 1
            continue
        if text[index - 1] == "\u200d":
            index -= 1
            while index > 0 and extends_grapheme(text[index - 1]):
                index -= 1
            if index > 0:
                index -= 1
            continue
        break
    if index > 0:
        return index
    # No safe boundary exists at or before `limit`: the grapheme cluster
    # starting at text[0] itself extends past `limit` (a base character with
    # more combining marks than fit under the cap, or an all-combining-mark
    # run with no base at all). Scan forward past the end of that cluster
    # instead of cutting through it -- this can violate the cap for this one
    # cluster, but it never strands a mark from its base and always advances
    # (returning 0 here would make the caller's split loop spin forever on an
    # unchanged remainder).
    index = original_index
    while index < len(text) and (
        extends_grapheme(text[index]) or text[index] == "\u200d" or text[index - 1] == "\u200d"
    ):
        index += 1
    return index or len(text)


def split_large_section(text: str, start_line: int, max_chars: int) -> Iterable[tuple[str, int, int]]:
    lines = text.splitlines()
    if len(text) <= max_chars:
        yield text.strip(), start_line, start_line + max(len(lines) - 1, 0)
        return

    blocks: list[tuple[list[str], int, int]] = []
    current: list[str] = []
    block_start = start_line
    for offset, line in enumerate(lines):
        if not line.strip() and current:
            blocks.append((current, block_start, start_line + offset - 1))
            current = []
        elif line.strip():
            if not current:
                block_start = start_line + offset
            current.append(line)
    if current:
        blocks.append((current, block_start, start_line + len(lines) - 1))

    current, current_chars, part_start, part_end = [], 0, start_line, start_line
    for block, block_line_start, block_line_end in blocks:
        block_text = "\n".join(block)
        # A prose paragraph can itself exceed the cap. Preserve paragraph
        # boundaries when possible, but hard-split this fallback so the cap is
        # a real contract for embedding and retrieval callers.
        while len(block_text) > max_chars:
            if current:
                yield "\n\n".join(current).strip(), part_start, part_end
                current, current_chars = [], 0
            split_at = grapheme_safe_split_index(block_text, max_chars)
            piece = block_text[:split_at]
            stripped_piece = piece.strip()
            leading_removed = len(piece) - len(piece.lstrip())
            trailing_kept = len(piece.rstrip())
            piece_start = block_line_start + piece[:leading_removed].count("\n")
            piece_end = block_line_start + piece[:trailing_kept].count("\n")
            yield stripped_piece, piece_start, piece_end
            block_text = block_text[split_at:]
            block_line_start += piece.count("\n")
            part_start = block_line_start
        addition = len(block_text) + (2 if current else 0)
        if current and current_chars + addition > max_chars:
            yield "\n\n".join(current).strip(), part_start, part_end
            current, current_chars, part_start = [], 0, block_line_start
        if not current:
            part_start = block_line_start
        current.append(block_text)
        current_chars += addition
        part_end = block_line_end
    if current:
        yield "\n\n".join(current).strip(), part_start, part_end


def markdown_chunks(text: str, fallback_title: str, *, max_chars: int) -> list[MarkdownChunk]:
    lines = text.splitlines()
    headings: list[tuple[int, int, str, str]] = []
    hierarchy: list[str] = []
    for index, level, name in iter_heading_lines(lines):
        hierarchy = hierarchy[: level - 1]
        hierarchy.append(name)
        headings.append((index, level, name, " > ".join(hierarchy)))

    sections: list[tuple[str, str, int, int]] = []
    if not headings:
        sections.append((fallback_title, fallback_title, 0, len(lines)))
    else:
        if headings[0][0] > 0:
            sections.append((fallback_title, fallback_title, 0, headings[0][0]))
        for pos, (line_index, _level, name, path) in enumerate(headings):
            end = headings[pos + 1][0] if pos + 1 < len(headings) else len(lines)
            sections.append((name, path, line_index + 1, end))

    chunks: list[MarkdownChunk] = []
    for name, heading_path, content_start, content_end in sections:
        section_lines = lines[content_start:content_end]
        populated = [index for index, line in enumerate(section_lines) if line.strip()]
        if not populated:
            # Parent headings with no body are context for child chunks, not
            # useful standalone retrieval results.
            continue
        first_content = populated[0]
        last_content = populated[-1]
        body = "\n".join(section_lines[first_content:last_content + 1])
        pieces = list(split_large_section(body, content_start + first_content + 1, max_chars))
        for part, (piece, line_start, line_end) in enumerate(pieces, start=1):
            title = heading_path if len(pieces) == 1 else f"{heading_path} (part {part}/{len(pieces)})"
            chunks.append(MarkdownChunk(heading_path, title, piece, line_start, line_end))
    return chunks


def classify(kind: str, heading: str, content: str) -> str:
    haystack = f"{heading}\n{content[:600]}".lower()
    if kind == "training_guide":
        return "agent_training"
    if any(word in haystack for word in ("bug", "debug", "root cause", "แก้ bug", "failure")):
        return "debugging"
    if any(word in haystack for word in ("test", "verify", "validation", "regression")):
        return "testing"
    if any(word in haystack for word in ("architecture", "design", "schema", "routing")):
        return "architecture"
    if any(word in haystack for word in ("session", "checkpoint", "handoff")):
        return "session_history"
    if kind == "project_memory":
        return "project_memory"
    return "documentation"


_BUG_ID_RE = re.compile(r"\b(?:V\d+(?:-[A-Z0-9]+)+|O-\d+)\b", re.IGNORECASE)
_SESSION_RE = re.compile(r"\bSession\s+(\d+)\b", re.IGNORECASE)
_DATE_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
_MODULE_RE = re.compile(r"`([^`\n]+?\.(?:py|js|ts|md|sql))`")
# These markers only ever mean "unresolved", so they stand alone.
_STRONG_OPEN_STATUS_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:not[\s-]+fixed|unfixed|backlog)(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
# "open" and "pending" are ordinary English words -- "open the file",
# "open-source", "pending investigation", "discard pending". Matching them
# bare labelled 40% of every indexed status='open' row from prose alone
# (499 of 1,255 rows measured against the live index), which is exactly the
# field agents read to answer "what is still unresolved". They now require a
# status context. The hyphen is excluded from both boundaries because the old
# lookarounds allowed "open-source" through.
_OPEN_WORD = r"(?:open|pending)(?![A-Za-z0-9_-])"
_CONTEXTUAL_OPEN_STATUS_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:status|สถานะ)\s*[:=]\s*" + _OPEN_WORD
    + r"|(?<![A-Za-z0-9_-])(?:still|remains?|left|currently)\s+" + _OPEN_WORD
    + r"|(?<![A-Za-z0-9_-])(?:open|pending)\s+"
      r"(?:issue|bug|item|task|question|problem|work|fix)s?(?![A-Za-z0-9_-])"
    + r"|(?<![A-Za-z0-9_-])(?:open|pending)[ \t]*$"
    # Deliberately no '#' in this bullet class. A real heading is a section
    # boundary, so a '#' line surviving inside a section body is a fenced code
    # comment -- and the fence fix keeps code blocks inside their parent
    # section, which would make '# pending' set the whole section open. No
    # legitimate case needs it: "### Open Issues" already matches the
    # "open issue|item" branch above.
    + r"|^[ \t]*[|\-*>]+[ \t]*" + _OPEN_WORD
    + r"|\|[ \t]*" + _OPEN_WORD + r"[ \t]*\|",
    re.IGNORECASE | re.MULTILINE,
)
_ACCEPTED_STATUS_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:accepted|won['’]?t[\s-]+fix|deprioritized)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_RESOLVED_STATUS_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:resolved|fixed)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)


def extract_metadata(heading: str, content: str, category: str) -> dict[str, Any]:
    haystack = f"{heading}\n{content[:2000]}"
    lower = haystack.lower()
    # Negative phrases must win over their positive substrings: "not fixed"
    # contains "fixed", but represents an open issue.
    if (
        _STRONG_OPEN_STATUS_RE.search(lower)
        or _CONTEXTUAL_OPEN_STATUS_RE.search(lower)
        or any(token in lower for token in ("ยังไม่แก้", "เปิดค้าง"))
    ):
        status = "open"
    elif _ACCEPTED_STATUS_RE.search(lower):
        status = "accepted"
    elif _RESOLVED_STATUS_RE.search(lower) or any(token in lower for token in ("แก้แล้ว", "ผ่านแล้ว")):
        status = "resolved"
    else:
        status = ""
    modules = sorted(set(_MODULE_RE.findall(haystack)))[:6]
    bug_ids = sorted({match.upper() for match in _BUG_ID_RE.findall(haystack)})
    session_match = _SESSION_RE.search(heading)
    date_match = _DATE_RE.search(haystack)
    parent_heading = heading.rsplit(" > ", 1)[0] if " > " in heading else heading
    return {
        "type": category,
        "status": status,
        "bug_ids": bug_ids,
        "module": modules[0] if modules else "",
        "modules": modules,
        "session": f"Session {session_match.group(1)}" if session_match else "",
        "date": date_match.group(0) if date_match else "",
        "parent_heading": parent_heading,
    }
