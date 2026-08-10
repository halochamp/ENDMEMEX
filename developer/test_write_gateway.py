from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import write_gateway as gateway


class WriteGatewayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "gateway.sqlite3"
        self.conn = gateway.connect_store(self.path)
        gateway.initialize_receipts(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def request(self, key: str = "backup:request-001") -> dict:
        return {
            "idempotency_key": key,
            "command": "record-update",
            "arguments": ["AUDIT-DEMO-1", "--agent", "codex", "--action-state", "done"],
        }

    def test_request_boundary_is_allowlisted_and_forbids_database_or_file_injection(self):
        with self.assertRaisesRegex(ValueError, "allowlisted"):
            gateway.validate_request({
                "idempotency_key": "backup:bad-command", "command": "maintenance", "arguments": [],
            })
        for command, arguments in (
            ("checkpoint", ["--db", "/tmp/other.sqlite3"]),
            ("checkpoint", ["--db=/tmp/other.sqlite3"]),
            ("checkpoint", ["--payload", "/etc/passwd"]),
            ("checkpoint", ["--payload=/etc/passwd"]),
            ("record-add", ["--content-file", "/etc/passwd"]),
            ("record-add", ["--content-file=/etc/passwd"]),
            ("ingest", ["/etc/passwd", "--project", "DEMO"]),
        ):
            with self.subTest(command=command, arguments=arguments), self.assertRaises(ValueError):
                gateway.validate_request({
                    "idempotency_key": f"backup:{command}-safe", "command": command,
                    "arguments": arguments,
                })

    def test_idempotency_receipt_dispatches_once_and_replays_terminal_response(self):
        response = {"exit_code": 0, "stdout": '{"ok":true}', "stderr": ""}
        with mock.patch.object(gateway, "dispatch", return_value=response) as dispatch:
            first_status, first = gateway.process_request(self.conn, self.request())
            replay_status, replay = gateway.process_request(self.conn, self.request())
        self.assertEqual((first_status, replay_status), (200, 200))
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(first["status"], "completed")
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["response"], response)

    def test_processing_receipt_is_fail_closed_after_crash(self):
        request = self.request("backup:crash-001")
        digest = gateway.request_hash(gateway.validate_request(request))
        self.conn.execute(
            """INSERT INTO gateway_receipts(
                   idempotency_key, command, arguments, request_hash, status, created_at, updated_at
               ) VALUES(?, ?, '[]', ?, 'processing', 'old', 'old')""",
            (request["idempotency_key"], request["command"], digest),
        )
        self.conn.commit()
        with mock.patch.object(gateway, "dispatch") as dispatch:
            status, result = gateway.process_request(self.conn, request)
        self.assertEqual(status, 202)
        self.assertEqual(result["status"], "processing")
        dispatch.assert_not_called()

    def test_same_key_with_changed_request_is_rejected(self):
        with mock.patch.object(gateway, "dispatch", return_value={"exit_code": 0, "stdout": "", "stderr": ""}):
            gateway.process_request(self.conn, self.request())
        changed = self.request()
        changed["arguments"] = ["AUDIT-OTHER-1", "--agent", "codex", "--action-state", "done"]
        status, result = gateway.process_request(self.conn, changed)
        self.assertEqual(status, 409)
        self.assertIn("different request", result["error"])

    def test_machine_outbox_keeps_failed_delivery_queued_and_marks_success(self):
        outbox = sqlite3.connect(Path(self.tmp.name) / "outbox.sqlite3")
        outbox.row_factory = sqlite3.Row
        gateway.initialize_outbox(outbox)
        request = self.request("backup:outbox-001")
        gateway.enqueue(outbox, request)
        with mock.patch.object(gateway, "post_request", side_effect=OSError("offline")):
            result = gateway.flush_outbox(outbox, "https://main.example", "x" * 32)
        self.assertEqual(result, {"delivered": 0, "remaining": 1})
        with mock.patch.object(gateway, "post_request", return_value={"status": "completed"}):
            result = gateway.flush_outbox(outbox, "https://main.example", "x" * 32)
        self.assertEqual(result, {"delivered": 1, "remaining": 0})
        outbox.close()

    def test_machine_outbox_does_not_report_terminal_gateway_failure_as_delivered(self):
        outbox = sqlite3.connect(Path(self.tmp.name) / "failed-outbox.sqlite3")
        outbox.row_factory = sqlite3.Row
        gateway.initialize_outbox(outbox)
        request = self.request("backup:terminal-failure-001")
        gateway.enqueue(outbox, request)
        remote_failure = {"status": "failed", "response": {"exit_code": 2, "stderr": "invalid"}}
        with mock.patch.object(gateway, "post_request", return_value=remote_failure):
            result = gateway.flush_outbox(outbox, "https://main.example", "x" * 32)
        self.assertEqual(result, {"delivered": 0, "remaining": 1})
        row = outbox.execute(
            "SELECT status, response FROM gateway_outbox WHERE idempotency_key = ?", (request["idempotency_key"],)
        ).fetchone()
        self.assertEqual(row["status"], "queued")
        self.assertEqual(json.loads(row["response"]), remote_failure)
        outbox.close()

    def test_machine_outbox_keeps_crash_left_processing_receipt_visible(self):
        outbox = sqlite3.connect(Path(self.tmp.name) / "processing-outbox.sqlite3")
        outbox.row_factory = sqlite3.Row
        gateway.initialize_outbox(outbox)
        request = self.request("backup:processing-001")
        gateway.enqueue(outbox, request)
        processing = {"status": "processing", "response": None}
        with mock.patch.object(gateway, "post_request", return_value=processing):
            result = gateway.flush_outbox(outbox, "https://main.example", "x" * 32)
        self.assertEqual(result, {"delivered": 0, "remaining": 1})
        row = outbox.execute(
            "SELECT status, response FROM gateway_outbox WHERE idempotency_key = ?", (request["idempotency_key"],)
        ).fetchone()
        self.assertEqual(row["status"], "queued")
        self.assertEqual(json.loads(row["response"]), processing)
        outbox.close()


if __name__ == "__main__":
    unittest.main()
