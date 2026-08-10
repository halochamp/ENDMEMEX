from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import errors
import endeavor_db


class ErrorsExtractionTest(unittest.TestCase):
    def test_imports_are_one_way(self):
        endmemex = Path(__file__).resolve().parent.parent
        script = (
            "import errors\n"
            "if 'endeavor_db' in __import__('sys').modules:\n"
            "    raise SystemExit('endeavor_db imported as a side effect of importing errors')\n"
            "import endeavor_db\n"
        )
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(endmemex))
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=str(endmemex), env=env,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_named_modules_import_errors_in_production(self):
        # sessions.py is a deliberate second importer (AmbiguousSessionError,
        # SessionNotFoundError), added with the sessions.py slice: neither
        # exception is itself a monkeypatch target, so a second importer adds
        # no seam risk.
        endmemex = Path(__file__).resolve().parent.parent
        importer_pattern = re.compile(r"^\s*(?:from|import)\s+errors\b", re.MULTILINE)
        importers = sorted(
            path.relative_to(endmemex).as_posix()
            for path in endmemex.glob("*.py")
            if path.name != "errors.py" and importer_pattern.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(importers, ["endeavor_db.py", "sessions.py"])

    def test_endeavor_db_exceptions_are_the_identical_class_objects(self):
        self.assertIs(endeavor_db.SessionNotFoundError, errors.SessionNotFoundError)
        self.assertIs(endeavor_db.AmbiguousSessionError, errors.AmbiguousSessionError)

    def test_no_exception_instance_crosses_a_process_boundary(self):
        # The deferred risk this extraction addresses was module/pickle
        # identity for an exception instance serialized across a process
        # boundary. Confirm mechanically, not just by prose, that neither
        # subprocess-based bridge (mcp_server.py's JSON stdout/stderr,
        # agent_delegate.py's JSON state files) ever pickles one.
        endmemex = Path(__file__).resolve().parent.parent
        for name in ("mcp_server.py", "agent_delegate.py", "agent_mcp_server.py"):
            with self.subTest(module=name):
                text = (endmemex / name).read_text(encoding="utf-8")
                self.assertNotIn("pickle", text)
                self.assertNotIn("SessionNotFoundError", text)
                self.assertNotIn("AmbiguousSessionError", text)


if __name__ == "__main__":
    unittest.main()
