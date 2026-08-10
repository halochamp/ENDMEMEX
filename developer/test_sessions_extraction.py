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
import sessions


class SessionsExtractionTest(unittest.TestCase):
    def test_imports_are_one_way(self):
        endmemex = Path(__file__).resolve().parent.parent
        script = (
            "import sessions\n"
            "if 'endeavor_db' in __import__('sys').modules:\n"
            "    raise SystemExit('endeavor_db imported as a side effect of importing sessions')\n"
            "import endeavor_db\n"
        )
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(endmemex))
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=str(endmemex), env=env,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_endeavor_db_imports_sessions_in_production(self):
        endmemex = Path(__file__).resolve().parent.parent
        importer_pattern = re.compile(r"^\s*(?:from|import)\s+sessions\b", re.MULTILINE)
        importers = sorted(
            path.relative_to(endmemex).as_posix()
            for path in endmemex.glob("*.py")
            if path.name != "sessions.py" and importer_pattern.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(importers, ["endeavor_db.py"])

    def test_facade_functions_are_the_identical_objects(self):
        self.assertIs(endeavor_db.start_session, sessions.start_session)
        self.assertIs(endeavor_db.resolve_session, sessions.resolve_session)
        self.assertIs(endeavor_db.resolve_or_start_checkpoint_session, sessions.resolve_or_start_checkpoint_session)
        self.assertIs(endeavor_db.row_dict, sessions.row_dict)
        self.assertIs(endeavor_db.handoff, sessions.handoff)
        self.assertIs(endeavor_db.paused_handoffs, sessions.paused_handoffs)
        self.assertIs(endeavor_db.checkpoint_timeline, sessions.checkpoint_timeline)
        self.assertIs(endeavor_db.render_checkpoint_timeline, sessions.render_checkpoint_timeline)
        self.assertIs(endeavor_db._pending_session_entries, sessions._pending_session_entries)
        self.assertIs(endeavor_db._presence_row_dict, sessions._presence_row_dict)
        self.assertIs(endeavor_db._prune_expired_presence_rows, sessions._prune_expired_presence_rows)
        self.assertIs(endeavor_db._sidecar_lock, sessions._sidecar_lock)
        self.assertIs(endeavor_db._reap_stale_sidecar_temps, sessions._reap_stale_sidecar_temps)

    def test_insert_session_is_not_reexported(self):
        # _insert_session's only two original callers (start_session,
        # resolve_or_start_checkpoint_session) moved with it -- nothing in
        # endeavor_db.py or the test suite looks it up by that name, so it is
        # not part of the facade surface at all.
        self.assertFalse(hasattr(endeavor_db, "_insert_session"))

    def test_eight_checkpoint_and_directory_coupled_functions_stayed_in_endeavor_db(self):
        # Confirms the deliberate scoping decision from the transitive-
        # closure AST call-graph walk.
        for name in (
            "add_checkpoint", "prune_checkpoints_globally", "set_checkpoint_pinned",
            "pinned_checkpoint_warning", "_presence_sidecar_path", "_presence_sidecar_lock",
            "_sync_freshness_path", "sync_freshness_report",
        ):
            with self.subTest(name=name):
                self.assertIn(name, endeavor_db.__dict__)
                self.assertEqual(getattr(endeavor_db.__dict__[name], "__module__", None), "endeavor_db")
                self.assertFalse(hasattr(sessions, name))

    def test_start_session_and_handoff_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = endeavor_db.connect(Path(directory) / "memory.sqlite3")
            endeavor_db.initialize(conn)
            session_id = endeavor_db.start_session(conn, "demo", "goal text", "codex", {})
            resolved = endeavor_db.resolve_session(conn, session_id, None)
            self.assertEqual(resolved["id"], session_id)
            checkpoint_id = endeavor_db.add_checkpoint(
                conn, resolved, "codex", {"summary": "did a thing", "status": "paused"},
            )
            self.assertGreater(checkpoint_id, 0)
            result = endeavor_db.handoff(conn, session_id, None)
            self.assertEqual(result["checkpoint"]["id"], checkpoint_id)
            paused = endeavor_db.paused_handoffs(conn)
            self.assertTrue(any(item["session"]["id"] == session_id for item in paused))
            conn.close()

    def test_ambiguous_session_error_raised_through_the_facade(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = endeavor_db.connect(Path(directory) / "memory.sqlite3")
            endeavor_db.initialize(conn)
            endeavor_db.start_session(conn, "demo", "goal one", "codex", {})
            endeavor_db.start_session(conn, "demo", "goal two", "codex", {})
            with self.assertRaises(endeavor_db.AmbiguousSessionError):
                endeavor_db.resolve_session(conn, None, "demo")
            conn.close()


class CheckpointTimelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = Path(self.tmp.name) / "memory.sqlite3"
        self.conn = endeavor_db.connect(self.database)
        endeavor_db.initialize(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _timeline(self, **kwargs):
        kwargs.setdefault("per_session_cap", endeavor_db.MAX_CHECKPOINTS)
        kwargs.setdefault("total_cap", endeavor_db.MAX_TOTAL_CHECKPOINTS)
        return endeavor_db.checkpoint_timeline(self.conn, **kwargs)

    def test_filters_by_project_agent_status_and_session(self):
        demo_id = endeavor_db.start_session(self.conn, "demo", "demo goal", "codex", {})
        demo = endeavor_db.resolve_session(self.conn, demo_id, None)
        endeavor_db.add_checkpoint(self.conn, demo, "codex", {"summary": "demo work", "status": "paused"})
        other_id = endeavor_db.start_session(self.conn, "other", "other goal", "claude", {})
        other = endeavor_db.resolve_session(self.conn, other_id, None)
        endeavor_db.add_checkpoint(self.conn, other, "claude", {"summary": "other work", "status": "active"})

        self.assertEqual(
            {r["session_id"] for r in self._timeline(project="demo")["records"]}, {demo_id},
        )
        self.assertEqual(
            {r["session_id"] for r in self._timeline(agent="claude")["records"]}, {other_id},
        )
        self.assertEqual(
            {r["session_id"] for r in self._timeline(session_status="active")["records"]}, {other_id},
        )
        self.assertEqual(
            {r["checkpoint_id"] for r in self._timeline(session_id=demo_id)["records"]},
            {self._timeline(session_id=demo_id)["records"][0]["checkpoint_id"]},
        )
        self.assertEqual(self._timeline(project="nonexistent")["records"], [])

    def test_checkpoint_status_is_derived_current_vs_historical(self):
        session_id = endeavor_db.start_session(self.conn, "demo", "goal", "codex", {})
        session = endeavor_db.resolve_session(self.conn, session_id, None)
        endeavor_db.add_checkpoint(self.conn, session, "codex", {"summary": "first", "status": "active"})
        endeavor_db.add_checkpoint(self.conn, session, "codex", {"summary": "second", "status": "paused"})

        records = self._timeline()["records"]
        by_summary = {r["summary"]: r["checkpoint_status"] for r in records}
        self.assertEqual(by_summary["second"], "current")
        self.assertEqual(by_summary["first"], "historical")
        # session_status reflects the session's current lifecycle state for
        # every row of that session, not a per-checkpoint historical snapshot.
        self.assertTrue(all(r["session_status"] == "paused" for r in records))

    def test_default_order_is_deterministic_newest_first_with_id_tiebreak(self):
        session_id = endeavor_db.start_session(self.conn, "demo", "goal", "codex", {})
        session = endeavor_db.resolve_session(self.conn, session_id, None)
        # Same created_at timestamp forces the tiebreak onto checkpoint id.
        with mock.patch.object(endeavor_db, "now_utc", return_value="2026-01-01T00:00:00+00:00"):
            endeavor_db.add_checkpoint(self.conn, session, "codex", {"summary": "one"})
            endeavor_db.add_checkpoint(self.conn, session, "codex", {"summary": "two"})

        newest_first = self._timeline()["records"]
        self.assertEqual([r["summary"] for r in newest_first], ["two", "one"])
        oldest_first = self._timeline(oldest_first=True)["records"]
        self.assertEqual([r["summary"] for r in oldest_first], ["one", "two"])

    def test_limit_default_and_clamp_to_500_with_truncated_flag(self):
        session_id = endeavor_db.start_session(self.conn, "demo", "goal", "codex", {})
        session = endeavor_db.resolve_session(self.conn, session_id, None)
        for i in range(5):
            endeavor_db.add_checkpoint(self.conn, session, "codex", {"summary": f"cp{i}"})

        default_view = self._timeline()
        self.assertEqual(default_view["limit"], 100)
        self.assertFalse(default_view["truncated"])
        self.assertEqual(default_view["count"], 5)
        self.assertEqual(default_view["total_matching"], 5)

        capped = self._timeline(limit=2)
        self.assertEqual(capped["limit"], 2)
        self.assertEqual(capped["count"], 2)
        self.assertEqual(capped["total_matching"], 5)
        self.assertTrue(capped["truncated"])

        over_max = self._timeline(limit=10_000)
        self.assertEqual(over_max["limit"], 500)

    def test_json_array_fields_are_deserialized(self):
        session_id = endeavor_db.start_session(self.conn, "demo", "goal", "codex", {})
        session = endeavor_db.resolve_session(self.conn, session_id, None)
        endeavor_db.add_checkpoint(self.conn, session, "codex", {
            "summary": "with evidence",
            "files_changed": ["a.py", "b.py"],
            "commands_run": ["pytest"],
            "verification": ["all green"],
        })
        record = self._timeline()["records"][0]
        self.assertEqual(record["files_changed"], ["a.py", "b.py"])
        self.assertEqual(record["commands_run"], ["pytest"])
        self.assertEqual(record["verification"], ["all green"])

    def test_malformed_non_list_evidence_field_is_normalized_not_split_into_characters(self):
        # New writes validate their payload shape, but historical/manual rows
        # can still contain a JSON string instead of a JSON array (row_dict's
        # json.loads succeeds either way -- a bare string is valid JSON).
        # Reproduced live: this used to render as "n, o, t, _, a, ..." plus a
        # fabricated file:// link for the "." character.
        session_id = endeavor_db.start_session(self.conn, "demo", "goal", "codex", {})
        session = endeavor_db.resolve_session(self.conn, session_id, None)
        endeavor_db.add_checkpoint(self.conn, session, "codex", {"summary": "bare"})
        # Simulate a historical/manual malformed row: json_text() on a plain
        # string, not a list.
        with self.conn:
            self.conn.execute(
                "UPDATE checkpoints SET files_changed = ?, commands_run = ?, verification = ? "
                "WHERE session_id = ?",
                ('"not_a_list.py"', '"pytest -q"', '"looks fine"', session_id),
            )

        record = self._timeline()["records"][0]
        self.assertEqual(record["files_changed"], ["not_a_list.py"])
        self.assertEqual(record["commands_run"], ["pytest -q"])
        self.assertEqual(record["verification"], ["looks fine"])

        rendered = endeavor_db.render_checkpoint_timeline(self._timeline())
        self.assertIn("Files changed: not_a_list.py", rendered)
        self.assertNotIn("n, o, t, _, a", rendered)

    def test_legacy_non_string_evidence_members_are_normalized_before_rendering(self):
        session_id = endeavor_db.start_session(self.conn, "demo", "goal", "codex", {})
        session = endeavor_db.resolve_session(self.conn, session_id, None)
        endeavor_db.add_checkpoint(self.conn, session, "codex", {"summary": "bare"})
        with self.conn:
            self.conn.execute(
                "UPDATE checkpoints SET files_changed = ?, commands_run = ?, verification = ? "
                "WHERE session_id = ?",
                ('[123, {"a": 1}]', '[false]', '[null]', session_id),
            )

        record = self._timeline()["records"][0]
        self.assertEqual(record["files_changed"], ["123", '{"a": 1}'])
        self.assertEqual(record["commands_run"], ["false"])
        self.assertEqual(record["verification"], ["null"])
        rendered = endeavor_db.render_checkpoint_timeline(
            self._timeline(), root=Path(self.tmp.name),
        )
        self.assertIn('Files changed: 123, {"a": 1}', rendered)
        self.assertIn("Commands run: false", rendered)
        self.assertIn("Verification: null", rendered)

    def test_retention_notice_reflects_injected_caps_not_hardcoded_values(self):
        data = self._timeline(per_session_cap=7, total_cap=42)
        self.assertIn("7 per session", data["retention_notice"])
        self.assertIn("42 total globally", data["retention_notice"])
        self.assertNotIn("500", data["retention_notice"])

    def test_legacy_empty_evidence_is_not_fabricated(self):
        session_id = endeavor_db.start_session(self.conn, "demo", "goal", "codex", {})
        session = endeavor_db.resolve_session(self.conn, session_id, None)
        endeavor_db.add_checkpoint(self.conn, session, "codex", {"summary": "bare minimum"})
        record = self._timeline()["records"][0]
        self.assertEqual(record["work_done"], "")
        self.assertEqual(record["current_state"], "")
        self.assertEqual(record["next_steps"], "")
        self.assertEqual(record["blockers"], "")
        self.assertEqual(record["files_changed"], [])
        self.assertEqual(record["commands_run"], [])
        self.assertEqual(record["verification"], [])
        rendered = endeavor_db.render_checkpoint_timeline(self._timeline())
        self.assertIn("(not recorded)", rendered)
        self.assertIn("(none)", rendered)

    def test_render_links_files_that_resolve_under_root_and_leaves_others_plain(self):
        real_file = Path(self.tmp.name) / "real.py"
        real_file.write_text("# real\n", encoding="utf-8")
        session_id = endeavor_db.start_session(self.conn, "demo", "goal", "codex", {})
        session = endeavor_db.resolve_session(self.conn, session_id, None)
        endeavor_db.add_checkpoint(self.conn, session, "codex", {
            "summary": "evidence", "files_changed": ["real.py", "does_not_exist.py"],
        })
        rendered = endeavor_db.render_checkpoint_timeline(self._timeline(), root=Path(self.tmp.name))
        self.assertIn(f"[real.py](file://{real_file.resolve()})", rendered)
        self.assertIn("does_not_exist.py", rendered)
        self.assertNotIn("[does_not_exist.py]", rendered)

    def test_render_without_root_never_links(self):
        session_id = endeavor_db.start_session(self.conn, "demo", "goal", "codex", {})
        session = endeavor_db.resolve_session(self.conn, session_id, None)
        endeavor_db.add_checkpoint(self.conn, session, "codex", {
            "summary": "evidence", "files_changed": ["ENDMEMEX/sessions.py"],
        })
        rendered = endeavor_db.render_checkpoint_timeline(self._timeline())
        self.assertNotIn("file://", rendered)
        self.assertIn("ENDMEMEX/sessions.py", rendered)

    def test_read_only_connection_can_run_the_timeline(self):
        session_id = endeavor_db.start_session(self.conn, "demo", "goal", "codex", {})
        session = endeavor_db.resolve_session(self.conn, session_id, None)
        endeavor_db.add_checkpoint(self.conn, session, "codex", {"summary": "x"})
        self.conn.close()
        ro_conn = endeavor_db.connect(self.database, read_only=True)
        try:
            before = ro_conn.total_changes
            data = endeavor_db.checkpoint_timeline(
                ro_conn, per_session_cap=endeavor_db.MAX_CHECKPOINTS, total_cap=endeavor_db.MAX_TOTAL_CHECKPOINTS,
            )
            self.assertEqual(ro_conn.total_changes, before)
            self.assertEqual(data["count"], 1)
        finally:
            ro_conn.close()
            self.conn = endeavor_db.connect(self.database)  # tearDown expects an open self.conn


if __name__ == "__main__":
    unittest.main()
