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
import delegate_lifecycle


class DelegateLifecycleExtractionTest(unittest.TestCase):
    def test_imports_are_one_way(self):
        endmemex = Path(__file__).resolve().parent.parent
        script = (
            "import delegate_lifecycle\n"
            "if 'agent_delegate' in __import__('sys').modules:\n"
            "    raise SystemExit('agent_delegate imported as a side effect of importing delegate_lifecycle')\n"
            "import agent_delegate\n"
        )
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(endmemex))
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=str(endmemex), env=env,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_agent_delegate_imports_delegate_lifecycle_in_production(self):
        endmemex = Path(__file__).resolve().parent.parent
        importer_pattern = re.compile(r"^\s*(?:from|import)\s+delegate_lifecycle\b", re.MULTILINE)
        importers = sorted(
            path.relative_to(endmemex).as_posix()
            for path in endmemex.glob("*.py")
            if path.name != "delegate_lifecycle.py" and importer_pattern.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(importers, ["agent_delegate.py"])

    def test_facade_functions_and_constants_are_the_identical_objects(self):
        for name in (
            "now_iso", "_safe_run_id", "parse_depth", "infer_caller", "effective_model",
            "effective_reasoning_effort", "build_command", "base_entry", "_event_texts",
            "_event_error_texts", "_filter_plain_progress", "classify_error", "validate_result",
            "is_transient", "role_policy_error", "_config_from_args",
            "_darwin_bsd_info", "_iso_age_seconds",
            "DEPTH_ENV", "CALLER_ENV", "MAX_DEPTH", "LOG_PROMPT_CHARS", "PROGRESS_TAIL_CHARS",
            "EXIT_ARTIFACT_LIMIT", "EXIT_REAP_FAILED", "READ_ONLY_CLAUDE_TOOLS",
        ):
            with self.subTest(name=name):
                self.assertIs(getattr(agent_delegate, name), getattr(delegate_lifecycle, name))

    def test_transient_markers_darwin_class_and_process_group_exists_not_reexported(self):
        # No reader in agent_delegate.py and no test reference -- unlike
        # LOG_PROMPT_CHARS (tested directly), these have no facade-identity
        # reason to stay on the module.
        for name in ("TRANSIENT_MARKERS", "_DarwinProcBsdInfo", "_process_group_exists"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(agent_delegate, name))
                self.assertTrue(hasattr(delegate_lifecycle, name))

    def test_write_checkpoint_stayed_because_it_reads_root_and_db_script(self):
        # Confirms the deliberate scoping decision: write_checkpoint was in
        # the same transitively-clean candidate set but reads ROOT/DB_SCRIPT,
        # which stay defined in agent_delegate.py (ROOT feeds
        # build_delegate_parser's --cwd default; HERE, which ROOT derives
        # from, uses Path(__file__), and Path is itself a monkeypatch target).
        self.assertIn("write_checkpoint", agent_delegate.__dict__)
        self.assertEqual(agent_delegate.write_checkpoint.__module__, "agent_delegate")
        self.assertFalse(hasattr(delegate_lifecycle, "write_checkpoint"))

    def test_build_command_produces_the_same_shape_via_the_facade(self):
        args = argparse.Namespace(
            target="codex", cwd="/tmp", sandbox="read-only", isolated=False, model=None,
            reasoning_effort=None, json=False, stream_progress=False, prompt="hello",
        )
        cmd = agent_delegate.build_command(args, "codex-binary")
        self.assertEqual(cmd[0], "codex-binary")
        self.assertIn("--", cmd)
        self.assertEqual(cmd[-1], "hello")

    def test_classify_error_and_is_transient_agree_through_the_facade(self):
        args = argparse.Namespace(target="claude", role="worker")
        error_kind, _hint = agent_delegate.classify_error(args, "claude", 124, "", "")
        self.assertEqual(error_kind, "timeout")
        self.assertTrue(agent_delegate.is_transient(124, error_kind))


if __name__ == "__main__":
    unittest.main()
