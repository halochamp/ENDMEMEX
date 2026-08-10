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
import migrations


class MigrationsExtractionTest(unittest.TestCase):
    def test_imports_are_one_way(self):
        endmemex = Path(__file__).resolve().parent.parent
        script = (
            "import migrations\n"
            "if 'endeavor_db' in __import__('sys').modules:\n"
            "    raise SystemExit('endeavor_db imported as a side effect of importing migrations')\n"
            "import endeavor_db\n"
        )
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(endmemex))
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=str(endmemex), env=env,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_endeavor_db_imports_migrations_in_production(self):
        endmemex = Path(__file__).resolve().parent.parent
        importer_pattern = re.compile(r"^\s*(?:from|import)\s+migrations\b", re.MULTILINE)
        importers = sorted(
            path.relative_to(endmemex).as_posix()
            for path in endmemex.glob("*.py")
            if path.name != "migrations.py" and importer_pattern.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(importers, ["endeavor_db.py"])

    def test_rebuild_and_migrate_wrappers_inject_execute_sql_script(self):
        # A crude but direct proof that the wrapper actually forwards
        # endeavor_db's own execute_sql_script rather than importing its own
        # copy: swap in a spy and confirm the DDL still executes through it.
        calls = []
        real_execute = endeavor_db.execute_sql_script

        def spy(conn, script):
            calls.append(script)
            return real_execute(conn, script)

        with tempfile.TemporaryDirectory() as directory:
            conn = endeavor_db.connect(Path(directory) / "memory.sqlite3")
            endeavor_db.initialize(conn)
            original = endeavor_db.execute_sql_script
            endeavor_db.execute_sql_script = spy
            try:
                endeavor_db._rebuild_fts(conn)
            finally:
                endeavor_db.execute_sql_script = original
            conn.close()
        self.assertTrue(calls, "wrapper must call endeavor_db's own execute_sql_script")

    def test_dedupe_and_ensure_components_are_the_identical_objects(self):
        self.assertIs(endeavor_db._dedupe_symmetric_relations, migrations.dedupe_symmetric_relations)
        self.assertIs(endeavor_db._ensure_memory_components, migrations.ensure_memory_components)


if __name__ == "__main__":
    unittest.main()
