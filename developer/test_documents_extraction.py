from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import documents
import endeavor_db


class DocumentsExtractionTest(unittest.TestCase):
    def test_imports_are_one_way(self):
        endmemex = Path(__file__).resolve().parent.parent
        script = (
            "import documents\n"
            "if 'endeavor_db' in __import__('sys').modules:\n"
            "    raise SystemExit('endeavor_db imported as a side effect of importing documents')\n"
            "import endeavor_db\n"
        )
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(endmemex))
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=str(endmemex), env=env,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_endeavor_db_imports_documents_in_production(self):
        endmemex = Path(__file__).resolve().parent.parent
        importer_pattern = re.compile(r"^\s*(?:from|import)\s+documents\b", re.MULTILINE)
        importers = sorted(
            path.relative_to(endmemex).as_posix()
            for path in endmemex.glob("*.py")
            if path.name != "documents.py" and importer_pattern.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(importers, ["endeavor_db.py"])

    def test_patching_endeavor_db_max_chunk_chars_controls_markdown_chunks(self):
        # markdown_chunks in documents.py always takes an explicit max_chars --
        # the endeavor_db facade wrapper must resolve its own patchable
        # MAX_CHUNK_CHARS global and pass it in, not read a frozen default.
        original = endeavor_db.MAX_CHUNK_CHARS
        endeavor_db.MAX_CHUNK_CHARS = 20
        try:
            chunks = endeavor_db.markdown_chunks("# Heading\n\n" + "x" * 80, "source")
        finally:
            endeavor_db.MAX_CHUNK_CHARS = original
        self.assertTrue(all(len(chunk.content) <= 20 for chunk in chunks))

    def test_patching_endeavor_db_max_chunk_chars_controls_split_large_section(self):
        # The private _split_large_section wrapper is the seam
        # _refresh_memory_record_embedding_chunks calls internally -- confirm
        # it also resolves the patched global, not a frozen one.
        original = endeavor_db.MAX_CHUNK_CHARS
        endeavor_db.MAX_CHUNK_CHARS = 10
        try:
            pieces = list(endeavor_db._split_large_section("x" * 55, 1))
        finally:
            endeavor_db.MAX_CHUNK_CHARS = original
        self.assertTrue(all(len(piece) <= 10 for piece, _, _ in pieces))

    def test_facade_functions_match_documents_module_output(self):
        text = "# Heading\n\nSome body text\n\n## Sub\n\nMore text here\n"
        self.assertEqual(
            endeavor_db.markdown_chunks(text, "fallback"),
            documents.markdown_chunks(text, "fallback", max_chars=endeavor_db.MAX_CHUNK_CHARS),
        )
        self.assertEqual(
            endeavor_db.classify("project_memory", "Session 5 bug fix", "root cause found"),
            documents.classify("project_memory", "Session 5 bug fix", "root cause found"),
        )
        self.assertEqual(
            endeavor_db.extract_metadata("Session 5", "still open, not fixed", "debugging"),
            documents.extract_metadata("Session 5", "still open, not fixed", "debugging"),
        )


class GraphemeSafeSplitIndexTest(unittest.TestCase):
    def test_base_plus_single_combining_mark_is_never_torn_apart(self):
        # Regression: a Thai base consonant + one tone mark spanning the
        # split boundary used to return an index that split them apart
        # ('return index or ...' treated the correct answer, 0, as falsy).
        text = "ก่"
        split_at = documents.grapheme_safe_split_index(text, 1)
        self.assertIn(split_at, (0, len(text)))
        # Whichever safe answer it picks, the piece before the split must
        # never contain the base without its mark (or vice versa).
        piece = text[:split_at]
        self.assertNotEqual(piece, "ก")

    def test_split_large_section_keeps_base_and_mark_together(self):
        pieces = list(documents.split_large_section("ก่" + "y" * 20, 0, max_chars=1))
        self.assertEqual(pieces[0][0], "ก่")
        self.assertNotIn("ก", [piece for piece, _, _ in pieces[1:]])

    def test_all_combining_marks_makes_forward_progress(self):
        # Pathological input with no base character anywhere must not make
        # grapheme_safe_split_index return 0 for non-empty text (the caller's
        # while loop treats 0 as "no progress" and would spin forever).
        text = "่้๊๋" * 5
        split_at = documents.grapheme_safe_split_index(text, 3)
        self.assertGreater(split_at, 0)

    def test_split_large_section_terminates_on_all_combining_marks(self):
        # Direct regression for the infinite-loop failure mode: this call
        # must return, not hang.
        pieces = list(documents.split_large_section("่้๊๋" * 5, 0, max_chars=3))
        self.assertTrue(pieces)

    def test_zwj_emoji_sequence_is_never_torn_apart(self):
        family = "\U0001F468‍\U0001F469‍\U0001F467"
        for limit in range(len(family) + 1):
            split_at = documents.grapheme_safe_split_index(family, limit)
            self.assertIn(split_at, (0, len(family)))

    def test_plain_ascii_unaffected(self):
        self.assertEqual(documents.grapheme_safe_split_index("hello world", 5), 5)


class FencedCodeBlockHeadingTest(unittest.TestCase):
    """A `#` line inside a fenced code block is a comment, never a heading."""

    FENCED = (
        "# Guide\n\n## Setup\n\n```bash\n# Copy the file\ncp a b\n```\n\n"
        "## Deploy\n\nReal deploy instructions live here.\n"
    )

    def _headings(self, text):
        return [chunk.heading for chunk in documents.markdown_chunks(text, "fallback", max_chars=500)]

    def test_shell_comment_in_fence_is_not_indexed_as_a_section(self):
        self.assertNotIn("Copy the file", self._headings(self.FENCED))

    def test_later_real_heading_keeps_its_true_parent(self):
        # The regression this guards: `hierarchy = hierarchy[: level - 1]`
        # mutates a shared stack, so a bogus in-fence heading rewrote the
        # breadcrumb of every LATER real heading -- "## Deploy" was indexed
        # as "Copy the file > Deploy".
        self.assertIn("Guide > Deploy", self._headings(self.FENCED))

    def test_enclosing_section_is_not_truncated_at_the_fence(self):
        chunks = documents.markdown_chunks(self.FENCED, "fallback", max_chars=500)
        setup = next(chunk for chunk in chunks if chunk.heading == "Guide > Setup")
        self.assertIn("cp a b", setup.content)

    def test_tilde_fences_are_handled(self):
        text = "# Real\n\n~~~\n# not a heading\n~~~\n\n## Child\n\nbody\n"
        self.assertEqual(self._headings(text), ["Real", "Real > Child"])

    def test_longer_closing_fence_and_nested_backticks_do_not_end_the_block(self):
        # The inner ``` neither closes the ```` block nor lets "# still code"
        # escape as a heading; only the final ```` does.
        text = "# Real\n\n````\n```\n# still code\n```\n````\n\n## Child\n\nbody\n"
        self.assertEqual(self._headings(text), ["Real", "Real > Child"])

    def test_document_title_skips_a_fenced_heading(self):
        self.assertEqual(documents.document_title("```\n# fake\n```\n\n# True Title\n", "fb"), "True Title")
        self.assertEqual(documents.document_title("no headings here\n", "fallback"), "fallback")


class OpenStatusContextTest(unittest.TestCase):
    """`open`/`pending` need a status context; prose must not mean "open"."""

    def _status(self, heading, content):
        return documents.extract_metadata(heading, content, "debugging")["status"]

    def test_prose_use_of_open_is_not_an_open_issue(self):
        self.assertEqual(self._status("Task index", "then open only that section of the manual."), "")
        self.assertEqual(self._status("Query", "Open the cited sources before relying on them."), "")
        self.assertEqual(self._status("Notes", "Use an open-source library for this."), "")
        self.assertEqual(self._status("AWAKE", "keeps the app window open with a live countdown."), "")

    def test_prose_use_of_pending_is_not_an_open_issue(self):
        self.assertEqual(self._status("Perf", "high variance — pending separate investigation."), "")
        self.assertEqual(self._status("Turn", "should discard pending and not increment the log."), "")

    def test_status_context_still_marks_an_open_issue(self):
        self.assertEqual(self._status("Issue", "Still open."), "open")
        self.assertEqual(self._status("Issue", "status: open"), "open")
        self.assertEqual(self._status("Production gap", "⏳ Open items (audit round 2-5)"), "open")
        self.assertEqual(self._status("Ledger", "| O-34 | open | needs a fix |"), "open")
        self.assertEqual(self._status("Perf", "tool_count variance left open"), "open")

    def test_unambiguous_markers_need_no_context(self):
        self.assertEqual(self._status("Heading", "not fixed"), "open")
        self.assertEqual(self._status("Heading", "unfixed"), "open")
        self.assertEqual(self._status("Heading", "backlog"), "open")
        self.assertEqual(self._status("Heading", "ยังไม่แก้"), "open")

    def test_resolved_and_accepted_precedence_is_unchanged(self):
        self.assertEqual(self._status("Integration", "OpenAI integration fixed successfully."), "resolved")
        self.assertEqual(self._status("O-27 opened then ACCEPTED", "No implementation change."), "accepted")


if __name__ == "__main__":
    unittest.main()
