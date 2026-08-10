from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import activity
import endeavor_db


class ActivityExtractionTest(unittest.TestCase):
    def test_imports_are_one_way(self):
        endmemex = Path(__file__).resolve().parent.parent
        script = (
            "import activity\n"
            "if 'endeavor_db' in __import__('sys').modules:\n"
            "    raise SystemExit('endeavor_db imported as a side effect of importing activity')\n"
            "import endeavor_db\n"
        )
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(endmemex))
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=str(endmemex), env=env,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_named_modules_import_activity_in_production(self):
        endmemex = Path(__file__).resolve().parent.parent
        importer_pattern = re.compile(r"^\s*(?:from|import)\s+activity\b", re.MULTILINE)
        importers = sorted(
            path.relative_to(endmemex).as_posix()
            for path in endmemex.glob("*.py")
            if path.name != "activity.py" and importer_pattern.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(importers, ["cli_parser.py", "endeavor_db.py"])

    def _seed(self, conn):
        endeavor_db.initialize(conn)
        conn.execute(
            "INSERT INTO activity_log(agent, action, project, detail, created_at) VALUES(?, ?, ?, ?, ?)",
            ("claude", "ingest", "DEMO", json.dumps({"source": "x", "entries": 3}), endeavor_db.now_utc()),
        )
        conn.commit()

    def test_patching_endeavor_db_render_activity_controls_refresh_activity_export(self):
        # refresh_activity_export must call the module-level (patchable) name,
        # not activity.render_activity directly -- this is the seam
        # test_refresh_activity_export_failure_never_raises in
        # test_endeavor_db.py exercises for the real function; here we assert
        # the reverse: a working patch is actually honored end to end.
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "memory.sqlite3"
            conn = endeavor_db.connect(db_path)
            self._seed(conn)
            sentinel = "SENTINEL-DIGEST"
            with mock.patch.object(endeavor_db, "render_activity", return_value=sentinel):
                target = endeavor_db.refresh_activity_export(conn, db_path)
            self.assertEqual(target.read_text(encoding="utf-8"), sentinel)
            conn.close()

    def test_patching_endeavor_db_max_activity_log_rows_controls_prune_activity_log(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = endeavor_db.connect(Path(directory) / "memory.sqlite3")
            endeavor_db.initialize(conn)
            for i in range(5):
                conn.execute(
                    "INSERT INTO activity_log(agent, action, project, detail, created_at) VALUES(?, ?, ?, ?, ?)",
                    ("claude", "ingest", "DEMO", "{}", endeavor_db.now_utc()),
                )
            conn.commit()
            original = endeavor_db.MAX_ACTIVITY_LOG_ROWS
            endeavor_db.MAX_ACTIVITY_LOG_ROWS = 2
            try:
                removed = endeavor_db.prune_activity_log(conn)
            finally:
                endeavor_db.MAX_ACTIVITY_LOG_ROWS = original
            self.assertEqual(removed, 3)
            remaining = conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0]
            self.assertEqual(remaining, 2)
            conn.close()

    def test_activity_module_functions_match_facade_output(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = endeavor_db.connect(Path(directory) / "memory.sqlite3")
            self._seed(conn)
            self.assertEqual(
                endeavor_db.render_activity(conn, 10),
                activity.render_activity(conn, 10, export_max=activity.ACTIVITY_EXPORT_MAX),
            )
            conn.close()


if __name__ == "__main__":
    unittest.main()
