from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import endeavor_db
import retrieval


class RetrievalExtractionTest(unittest.TestCase):
    def test_imports_are_one_way(self):
        endmemex = Path(__file__).resolve().parent.parent
        script = (
            "import retrieval\n"
            "if 'endeavor_db' in __import__('sys').modules:\n"
            "    raise SystemExit('endeavor_db imported as a side effect of importing retrieval')\n"
            "import endeavor_db\n"
        )
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(endmemex))
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=str(endmemex), env=env,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_named_modules_import_retrieval_in_production(self):
        # record_lifecycle.py is a deliberate second importer: it calls
        # retrieval.typo_corrected_terms directly with table_exists_fn=
        # table_exists (from db_connection.py) rather than routing through
        # endeavor_db's own _typo_corrected_terms wrapper, which would create
        # a reverse import (record_lifecycle.py -> endeavor_db.py). Safe
        # because typo_corrected_terms/fts_expression/query_terms are never
        # monkeypatch targets themselves (only the endeavor_db wrapper name
        # _typo_corrected_terms is referenced in tests, and only by direct
        # call, not patch).
        endmemex = Path(__file__).resolve().parent.parent
        importer_pattern = re.compile(r"^\s*(?:from|import)\s+retrieval\b", re.MULTILINE)
        importers = sorted(
            path.relative_to(endmemex).as_posix()
            for path in endmemex.glob("*.py")
            if path.name != "retrieval.py" and importer_pattern.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(importers, ["endeavor_db.py", "record_lifecycle.py"])

    def test_facade_functions_are_the_identical_objects(self):
        self.assertIs(endeavor_db.query_terms, retrieval.query_terms)
        self.assertIs(endeavor_db.fts_expression, retrieval.fts_expression)
        self.assertIs(endeavor_db.detect_intent, retrieval.detect_intent)

    def test_typo_corrected_terms_wrapper_injects_table_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = endeavor_db.connect(Path(directory) / "memory.sqlite3")
            endeavor_db.initialize(conn)
            source = Path(directory) / "typo.md"
            source.write_text("# Detection\n\nA silent failure must be detected.\n", encoding="utf-8")
            endeavor_db.ingest_markdown(conn, source, "demo", "project_memory", embed=False)
            corrected, reasons = endeavor_db._typo_corrected_terms(conn, ["failuer"], "knowledge_fts_terms")
            self.assertEqual(corrected, ["failure"])
            self.assertEqual(len(reasons), 1)
            conn.close()

    def test_query_terms_keep_thai_combining_marks_as_one_term(self):
        # A behavioral spot check, not just an identity check -- confirms the
        # Thai codepoint range regex survived the extraction unchanged.
        terms = retrieval.query_terms("ก่อน")
        self.assertEqual(terms, ["ก่อน"])

    def test_names_not_reexported_on_endeavor_db(self):
        # fts_atom/damerau_levenshtein/QUERY_ALIASES/STOP_WORDS have no
        # reader in endeavor_db.py and no test reference against
        # endeavor_db -- pinned as deliberately absent from the facade, the
        # same treatment as endeavor_db.py's own _insert_session and
        # agent_delegate.py's TRANSIENT_MARKERS. An earlier version of
        # These were not re-exported;
        # this test catches that claim (or a real future re-export) drifting
        # from this assertion again.
        for name in ("fts_atom", "damerau_levenshtein", "QUERY_ALIASES", "STOP_WORDS"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(endeavor_db, name), name)
                self.assertTrue(hasattr(retrieval, name), name)


if __name__ == "__main__":
    unittest.main()
