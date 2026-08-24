"""Tests for watch_computer_use_handoff.py — no real Codex/DB call is made."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import watch_computer_use_handoff as m  # noqa: E402


def handoff_payload(sequence=5, agent="claude", next_steps="hand off to Codex"):
    return {"checkpoint": {"session_id": m.SESSION, "agent": agent,
                           "sequence": sequence, "next_steps": next_steps}}


class StateIOTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(self.enterContext(
            __import__("tempfile").TemporaryDirectory()))
        self.state_path = self.tmp_dir / "state.json"
        self.patcher = mock.patch.object(m, "STATE", self.state_path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_read_missing_file_returns_empty(self):
        self.assertEqual(m.read_state(), {})

    def test_read_malformed_json_returns_empty_not_raise(self):
        self.state_path.write_text("{not valid json")
        self.assertEqual(m.read_state(), {})

    def test_read_non_object_json_returns_empty(self):
        self.state_path.write_text("[]")
        self.assertEqual(m.read_state(), {})

    def test_write_then_read_roundtrip(self):
        m.write_state(7)
        self.assertEqual(m.read_state(), {"sequence": 7})

    def test_write_does_not_leave_tmp_file_behind(self):
        m.write_state(3)
        leftovers = list(self.tmp_dir.glob("*.tmp"))
        self.assertEqual(leftovers, [])


class MainOrderingTest(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(self.enterContext(
            __import__("tempfile").TemporaryDirectory()))
        self.state_path = self.tmp_dir / "state.json"
        self.enterContext(mock.patch.object(m, "STATE", self.state_path))
        self.enterContext(mock.patch.object(m, "PROJECT", "demo"))
        self.enterContext(mock.patch.object(m, "SESSION", "session-1"))

    def test_missing_configuration_fails_before_reading_handoff(self):
        handoff = mock.Mock()
        with mock.patch.object(m, "PROJECT", ""), \
             mock.patch.object(m, "SESSION", ""), \
             mock.patch.object(m, "handoff", handoff):
            code = m.main()
        self.assertEqual(code, 2)
        handoff.assert_not_called()

    def _mock_handoff(self, payload):
        return mock.patch.object(m, "handoff", return_value=payload)

    def test_failed_launch_does_not_advance_state(self):
        # Regression: a missing/unlaunchable Codex binary must not mark the
        # handoff as handled, or it is permanently lost.
        with self._mock_handoff(handoff_payload(sequence=9)), \
             mock.patch.object(m.subprocess, "run", side_effect=OSError("no such file")):
            code = m.main()
        self.assertEqual(code, 1)
        self.assertEqual(m.read_state(), {})

    def test_successful_launch_advances_state(self):
        with self._mock_handoff(handoff_payload(sequence=9)), \
             mock.patch.object(m.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0)):
            code = m.main()
        self.assertEqual(code, 0)
        self.assertEqual(m.read_state(), {"sequence": 9})

    def test_failed_child_exit_does_not_advance_state(self):
        with self._mock_handoff(handoff_payload(sequence=9)), \
             mock.patch.object(m.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 7)):
            code = m.main()
        self.assertEqual(code, 7)
        self.assertEqual(m.read_state(), {})

    def test_invalid_checkpoint_shape_is_rejected_without_launch(self):
        run = mock.Mock()
        with self._mock_handoff({"checkpoint": []}), \
             mock.patch.object(m.subprocess, "run", run):
            code = m.main()
        self.assertEqual(code, 1)
        run.assert_not_called()

    def test_invalid_checkpoint_sequence_is_rejected_without_launch(self):
        payload = handoff_payload(sequence="not-an-integer")
        run = mock.Mock()
        with self._mock_handoff(payload), mock.patch.object(m.subprocess, "run", run):
            code = m.main()
        self.assertEqual(code, 1)
        run.assert_not_called()

    def test_invalid_saved_sequence_is_treated_as_unhandled(self):
        self.state_path.write_text(json.dumps({"sequence": "corrupt"}))
        with self._mock_handoff(handoff_payload(sequence=9)), \
             mock.patch.object(m.subprocess, "run",
                               return_value=subprocess.CompletedProcess([], 0)):
            code = m.main()
        self.assertEqual(code, 0)
        self.assertEqual(m.read_state(), {"sequence": 9})

    def test_already_handled_sequence_skips_launch(self):
        m.write_state(9)
        run = mock.Mock()
        with self._mock_handoff(handoff_payload(sequence=9)), \
             mock.patch.object(m.subprocess, "run", run):
            code = m.main()
        self.assertEqual(code, 0)
        run.assert_not_called()

    def test_wrong_session_skips_launch(self):
        payload = handoff_payload(sequence=9)
        payload["checkpoint"]["session_id"] = "some-other-session"
        run = mock.Mock()
        with self._mock_handoff(payload), \
             mock.patch.object(m.subprocess, "run", run):
            code = m.main()
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertEqual(m.read_state(), {})

    def test_next_steps_without_terra_or_codex_skips_launch(self):
        payload = handoff_payload(sequence=9, next_steps="continue with Sonnet")
        run = mock.Mock()
        with self._mock_handoff(payload), \
             mock.patch.object(m.subprocess, "run", run):
            code = m.main()
        self.assertEqual(code, 0)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
