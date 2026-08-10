from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import endeavor_db
import primitives


class PrimitivesExtractionTest(unittest.TestCase):
    def test_imports_are_one_way(self):
        endmemex = Path(__file__).resolve().parent.parent
        script = (
            "import primitives\n"
            "if 'endeavor_db' in __import__('sys').modules:\n"
            "    raise SystemExit('endeavor_db imported as a side effect of importing primitives')\n"
            "import endeavor_db\n"
        )
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(endmemex))
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=str(endmemex), env=env,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_named_modules_import_primitives_in_production(self):
        # record_lifecycle.py is a deliberate second importer (batched,
        # json_text, normalize_memory_id, now_utc), added with the
        # record_lifecycle.py slice; sessions.py is a third (json_text,
        # now_utc), added with the sessions.py slice; cli_parser.py is a
        # fourth (parse_feedback_result_id, an argparse type= validator),
        # added with the cli_parser.py slice. See test_config_extraction.py's
        # equivalent invariant for why growth of this set must stay explicit.
        endmemex = Path(__file__).resolve().parent.parent
        importer_pattern = re.compile(r"^\s*(?:from|import)\s+primitives\b", re.MULTILINE)
        importers = sorted(
            path.relative_to(endmemex).as_posix()
            for path in endmemex.glob("*.py")
            if path.name != "primitives.py" and importer_pattern.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(importers, ["cli_parser.py", "endeavor_db.py", "record_lifecycle.py", "sessions.py"])

    def test_facade_functions_are_the_identical_objects(self):
        self.assertIs(endeavor_db.now_utc, primitives.now_utc)
        self.assertIs(endeavor_db.json_text, primitives.json_text)
        self.assertIs(endeavor_db.normalize_memory_id, primitives.normalize_memory_id)
        self.assertIs(endeavor_db.parse_relation_spec, primitives.parse_relation_spec)
        self.assertIs(endeavor_db.parse_feedback_result_id, primitives.parse_feedback_result_id)
        self.assertIs(endeavor_db._batched, primitives.batched)

    def test_normalize_memory_id_rejects_and_accepts_the_same_shapes_as_before(self):
        self.assertEqual(endeavor_db.normalize_memory_id("mem-001"), "MEM-001")
        for bad in ("nohyphen", "", "MEM", "MEM-", "-001", "mem 001"):
            with self.assertRaises(ValueError):
                endeavor_db.normalize_memory_id(bad)

    def test_parse_relation_spec_validates_relation_and_normalizes_target(self):
        relation, target = endeavor_db.parse_relation_spec("resolves:mem-002")
        self.assertEqual((relation, target), ("resolves", "MEM-002"))
        with self.assertRaises(ValueError):
            endeavor_db.parse_relation_spec("not_a_relation:mem-002")
        with self.assertRaises(ValueError):
            endeavor_db.parse_relation_spec("resolvesmem-002")  # missing ':' separator

    def test_parse_feedback_result_id_distinguishes_int_and_memory_id(self):
        self.assertEqual(endeavor_db.parse_feedback_result_id("42"), 42)
        self.assertEqual(endeavor_db.parse_feedback_result_id("mem-003"), "MEM-003")

    def test_batched_default_size_comes_from_config_sql_batch_size(self):
        values = list(range(endeavor_db.SQL_BATCH_SIZE + 5))
        batches = list(endeavor_db._batched(values))
        self.assertEqual(len(batches[0]), endeavor_db.SQL_BATCH_SIZE)
        self.assertEqual(sum(len(batch) for batch in batches), len(values))

    def test_batched_explicit_size_overrides_default(self):
        batches = list(endeavor_db._batched(range(7), size=3))
        self.assertEqual([len(batch) for batch in batches], [3, 3, 1])


if __name__ == "__main__":
    unittest.main()
