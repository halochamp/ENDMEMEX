from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import endeavor_db
import record_lifecycle


class RecordLifecycleExtractionTest(unittest.TestCase):
    def test_imports_are_one_way(self):
        endmemex = Path(__file__).resolve().parent.parent
        script = (
            "import record_lifecycle\n"
            "if 'endeavor_db' in __import__('sys').modules:\n"
            "    raise SystemExit('endeavor_db imported as a side effect of importing record_lifecycle')\n"
            "import endeavor_db\n"
        )
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(endmemex))
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=str(endmemex), env=env,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_endeavor_db_imports_record_lifecycle_in_production(self):
        endmemex = Path(__file__).resolve().parent.parent
        importer_pattern = re.compile(r"^\s*(?:from|import)\s+record_lifecycle\b", re.MULTILINE)
        importers = sorted(
            path.relative_to(endmemex).as_posix()
            for path in endmemex.glob("*.py")
            if path.name != "record_lifecycle.py" and importer_pattern.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(importers, ["endeavor_db.py"])

    def test_facade_functions_are_the_identical_objects(self):
        self.assertIs(endeavor_db.add_memory_relation, record_lifecycle.add_memory_relation)
        self.assertIs(endeavor_db.compact_result, record_lifecycle.compact_result)
        self.assertIs(endeavor_db.memory_record_context, record_lifecycle.memory_record_context)
        self.assertIs(endeavor_db.memory_relation_health, record_lifecycle.memory_relation_health)
        self.assertIs(endeavor_db.search_memory_records, record_lifecycle.search_memory_records)
        self.assertIs(endeavor_db._current_ids_map, record_lifecycle._current_ids_map)
        self.assertIs(endeavor_db._insert_memory_relation, record_lifecycle._insert_memory_relation)
        self.assertIs(endeavor_db._memory_record_dict, record_lifecycle._memory_record_dict)
        self.assertIs(endeavor_db._memory_records_dicts, record_lifecycle._memory_records_dicts)
        self.assertIs(endeavor_db._terminal_current_ids, record_lifecycle._terminal_current_ids)

    def test_annotate_staleness_is_a_forwarding_wrapper_injecting_root(self):
        # Not an identical-object check (endeavor_db.annotate_staleness is a
        # thin wrapper, not a re-export) -- assert the wrapper actually
        # forwards ROOT so a future edit that breaks the injection fails here
        # instead of only showing up as a real-filesystem behavior drift.
        self.assertIsNot(endeavor_db.annotate_staleness, record_lifecycle.annotate_staleness)
        with mock.patch.object(endeavor_db, "_annotate_staleness_impl") as spy:
            endeavor_db.annotate_staleness(mock.sentinel.conn, mock.sentinel.results)
        spy.assert_called_once_with(mock.sentinel.conn, mock.sentinel.results, root=endeavor_db.ROOT)

    def test_four_functions_reaching_the_embedding_seam_stayed_in_endeavor_db(self):
        # Confirms the deliberate scoping decision from the AST call-graph
        # Call-graph walk: these four call an
        # outward monkeypatch target by bare identifier and must not move.
        for name in ("create_memory_record", "update_memory_record", "semantic_memory_records", "search_all"):
            with self.subTest(name=name):
                self.assertIn(name, endeavor_db.__dict__)
                self.assertEqual(getattr(endeavor_db.__dict__[name], "__module__", None), "endeavor_db")
                self.assertFalse(hasattr(record_lifecycle, name))

    def test_add_and_query_a_memory_relation_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = endeavor_db.connect(Path(directory) / "memory.sqlite3")
            endeavor_db.initialize(conn)
            source = endeavor_db.create_memory_record(
                conn, None, "demo", "audit", "Audit title", "audit body text", "open", "codex",
            )
            target = endeavor_db.create_memory_record(
                conn, None, "demo", "fix", "Fix title", "fix body text", "open", "codex",
            )
            endeavor_db.add_memory_relation(conn, target, "resolves", source, "", "codex")
            context = endeavor_db.memory_record_context(conn, source, depth=1)
            self.assertEqual(context["record"]["effective_status"], "resolved")
            health = endeavor_db.memory_relation_health(conn)
            self.assertTrue(health["ok"])
            results = endeavor_db.search_memory_records(conn, "audit body")
            self.assertTrue(any(item["id"] == source for item in results))
            conn.close()

    def test_annotate_staleness_and_compact_result_via_facade(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "memory.md"
            source.write_text("# Project\n\nOriginal searchable content.\n", encoding="utf-8")
            conn = endeavor_db.connect(root / "memory.sqlite3")
            endeavor_db.initialize(conn)
            endeavor_db.ingest_markdown(conn, source, "demo", "project_memory", embed=False)
            with mock.patch.object(endeavor_db, "ROOT", root):
                results = endeavor_db.search_all(conn, "searchable content", "demo", None, 5)
                endeavor_db.annotate_staleness(conn, results)
            self.assertFalse(results[0]["stale"])
            compact = endeavor_db.compact_result(results[0])
            self.assertIn("location", compact)
            conn.close()


if __name__ == "__main__":
    unittest.main()
