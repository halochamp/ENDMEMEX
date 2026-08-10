from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cli_validators
import endeavor_db


class CliValidatorsExtractionTest(unittest.TestCase):
    def test_imports_are_one_way(self):
        endmemex = Path(__file__).resolve().parent.parent
        script = (
            "import cli_validators\n"
            "if 'endeavor_db' in __import__('sys').modules:\n"
            "    raise SystemExit('endeavor_db imported as a side effect of importing cli_validators')\n"
            "import endeavor_db\n"
        )
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(endmemex))
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=str(endmemex), env=env,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_named_modules_import_cli_validators_in_production(self):
        endmemex = Path(__file__).resolve().parent.parent
        importer_pattern = re.compile(r"^\s*(?:from|import)\s+cli_validators\b", re.MULTILINE)
        importers = sorted(
            path.relative_to(endmemex).as_posix()
            for path in endmemex.glob("*.py")
            if path.name != "cli_validators.py" and importer_pattern.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(importers, ["cli_parser.py", "endeavor_db.py"])

    def test_facade_functions_are_the_identical_objects(self):
        self.assertIs(endeavor_db.positive_int, cli_validators.positive_int)
        self.assertIs(endeavor_db.pack_budget, cli_validators.pack_budget)
        self.assertIs(endeavor_db.nonempty_text, cli_validators.nonempty_text)

    def test_the_moved_functions_are_still_wired_as_the_parsers_type_validators(self):
        # This is the behavior the golden-hash regeneration is standing in
        # for: confirms the live parser actually uses these objects, not
        # just that the names resolve.
        parser = endeavor_db.build_parser()
        subparsers = next(
            action for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        backfill = subparsers.choices["embed-backfill"]
        batch_size_action = next(a for a in backfill._actions if a.dest == "batch_size")
        self.assertIs(batch_size_action.type, cli_validators.positive_int)

        pack = subparsers.choices["pack"]
        budget_action = next(a for a in pack._actions if a.dest == "budget_chars")
        self.assertIs(budget_action.type, cli_validators.pack_budget)

        pending = subparsers.choices["pending"]
        project_action = next(a for a in pending._actions if a.dest == "project")
        self.assertIs(project_action.type, cli_validators.nonempty_text)

    def test_validator_behavior_unchanged(self):
        self.assertEqual(endeavor_db.positive_int("5"), 5)
        with self.assertRaises(argparse.ArgumentTypeError):
            endeavor_db.positive_int("0")
        self.assertEqual(endeavor_db.pack_budget("1000"), 1000)
        with self.assertRaises(argparse.ArgumentTypeError):
            endeavor_db.pack_budget("499")
        self.assertEqual(endeavor_db.nonempty_text("x"), "x")
        with self.assertRaises(argparse.ArgumentTypeError):
            endeavor_db.nonempty_text("   ")


if __name__ == "__main__":
    unittest.main()
