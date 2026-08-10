from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cli_parser
import cli_validators
import endeavor_db
import primitives


class CliParserExtractionTest(unittest.TestCase):
    def test_imports_are_one_way(self):
        endmemex = Path(__file__).resolve().parent.parent
        script = (
            "import cli_parser\n"
            "if 'endeavor_db' in __import__('sys').modules:\n"
            "    raise SystemExit('endeavor_db imported as a side effect of importing cli_parser')\n"
            "import endeavor_db\n"
        )
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(endmemex))
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=str(endmemex), env=env,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_endeavor_db_imports_cli_parser_in_production(self):
        endmemex = Path(__file__).resolve().parent.parent
        importer_pattern = re.compile(r"^\s*(?:from|import)\s+cli_parser\b", re.MULTILINE)
        importers = sorted(
            path.relative_to(endmemex).as_posix()
            for path in endmemex.glob("*.py")
            if path.name != "cli_parser.py" and importer_pattern.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(importers, ["endeavor_db.py"])

    def test_facade_build_parser_matches_the_direct_call(self):
        def subcommand_names(parser):
            subparsers = next(a for a in parser._actions if a.dest == "command")
            return set(subparsers.choices.keys())

        facade_names = subcommand_names(endeavor_db.build_parser())
        direct_names = subcommand_names(cli_parser.build_parser("some other docstring"))
        self.assertEqual(facade_names, direct_names)
        self.assertIn("record-add", facade_names)

    def test_description_is_injected_from_endeavor_db_not_cli_parser(self):
        # __doc__ inside build_parser's body is a free variable resolved
        # against the enclosing module at call time -- this is the actual
        # risk the module_doc parameter exists to prevent. Assert against
        # the live facade call, not just the parameter plumbing.
        parser = endeavor_db.build_parser()
        self.assertEqual(parser.description, endeavor_db.__doc__)
        self.assertNotEqual(parser.description, cli_parser.__doc__)

    def test_validators_are_still_wired_through_the_relocated_parser(self):
        parser = endeavor_db.build_parser()
        subparsers = next(
            action for action in parser._actions
            if action.dest == "command"
        )
        pack = subparsers.choices["pack"]
        budget_action = next(a for a in pack._actions if a.dest == "budget_chars")
        self.assertIs(budget_action.type, cli_validators.pack_budget)

    def test_timeline_subcommand_has_all_filters_and_sensible_defaults(self):
        parser = endeavor_db.build_parser()
        subparsers = next(action for action in parser._actions if action.dest == "command")
        timeline = subparsers.choices["timeline"]
        dests = {a.dest for a in timeline._actions}
        self.assertTrue({"project", "agent", "status", "session", "limit", "oldest_first", "json"} <= dests)
        limit_action = next(a for a in timeline._actions if a.dest == "limit")
        self.assertEqual(limit_action.default, 100)
        self.assertIs(limit_action.type, cli_validators.positive_int)
        status_action = next(a for a in timeline._actions if a.dest == "status")
        self.assertEqual(status_action.choices, ("active", "paused", "completed", "blocked"))
        agent_action = next(a for a in timeline._actions if a.dest == "agent")
        self.assertEqual(agent_action.choices, ("codex", "claude", "human", "system"))
        parsed = timeline.parse_args([])
        self.assertIsNone(parsed.project)
        self.assertFalse(parsed.oldest_first)
        self.assertFalse(parsed.json)

    def test_parse_feedback_result_id_is_still_wired_through_the_relocated_parser(self):
        # parse_feedback_result_id (primitives.py) is the one relocated
        # argparse type= validator that had neither this assertion nor an
        # error-probe entry -- an Opus advisor audit caught the gap: the
        # golden hash only pins the validator's __module__.__qualname__
        # (its name), never that it's still actually attached to --result.
        parser = endeavor_db.build_parser()
        subparsers = next(
            action for action in parser._actions
            if action.dest == "command"
        )
        feedback = subparsers.choices["feedback"]
        result_action = next(a for a in feedback._actions if a.dest == "selected_ids")
        self.assertIs(result_action.type, primitives.parse_feedback_result_id)


if __name__ == "__main__":
    unittest.main()
