from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_delegate
import delegate_cli_validators


class DelegateCliValidatorsExtractionTest(unittest.TestCase):
    def test_imports_are_one_way(self):
        endmemex = Path(__file__).resolve().parent.parent
        script = (
            "import delegate_cli_validators\n"
            "if 'agent_delegate' in __import__('sys').modules:\n"
            "    raise SystemExit('agent_delegate imported as a side effect of importing delegate_cli_validators')\n"
            "import agent_delegate\n"
        )
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(endmemex))
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=str(endmemex), env=env,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_agent_delegate_imports_delegate_cli_validators_in_production(self):
        endmemex = Path(__file__).resolve().parent.parent
        importer_pattern = re.compile(r"^\s*(?:from|import)\s+delegate_cli_validators\b", re.MULTILINE)
        importers = sorted(
            path.relative_to(endmemex).as_posix()
            for path in endmemex.glob("*.py")
            if path.name != "delegate_cli_validators.py" and importer_pattern.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(importers, ["agent_delegate.py"])

    def test_facade_functions_are_the_identical_objects(self):
        self.assertIs(agent_delegate.positive_int, delegate_cli_validators.positive_int)
        self.assertIs(agent_delegate.nonnegative_int, delegate_cli_validators.nonnegative_int)
        self.assertIs(agent_delegate.nonnegative_float, delegate_cli_validators.nonnegative_float)

    def test_the_moved_functions_are_still_wired_as_the_parsers_type_validators(self):
        parser = agent_delegate.build_delegate_parser()
        timeout_action = next(a for a in parser._actions if a.dest == "timeout")
        self.assertIs(timeout_action.type, delegate_cli_validators.positive_int)
        min_chars_action = next(a for a in parser._actions if a.dest == "min_output_chars")
        self.assertIs(min_chars_action.type, delegate_cli_validators.nonnegative_int)
        retry_delay_action = next(a for a in parser._actions if a.dest == "retry_delay")
        self.assertIs(retry_delay_action.type, delegate_cli_validators.nonnegative_float)

    def test_validator_behavior_unchanged(self):
        self.assertEqual(agent_delegate.positive_int("5"), 5)
        with self.assertRaises(argparse.ArgumentTypeError):
            agent_delegate.positive_int("0")
        self.assertEqual(agent_delegate.nonnegative_int("0"), 0)
        with self.assertRaises(argparse.ArgumentTypeError):
            agent_delegate.nonnegative_int("-1")
        self.assertEqual(agent_delegate.nonnegative_float("0.5"), 0.5)
        with self.assertRaises(argparse.ArgumentTypeError):
            agent_delegate.nonnegative_float("-0.1")


if __name__ == "__main__":
    unittest.main()
