"""Real stdio/CLI smoke for the public grouped memory server, without services."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ENDMEMEX = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENDMEMEX))
import endeavor_db


class GroupedStdioIntegrationTest(unittest.TestCase):
    def test_list_dispatch_and_errors_through_real_stdio_and_cli(self):
        with tempfile.TemporaryDirectory(prefix="endmemex-grouped-") as directory:
            database = Path(directory) / "memory.sqlite3"
            conn = endeavor_db.connect(database)
            try:
                endeavor_db.initialize(conn)
                conn.commit()
            finally:
                conn.close()
            calls = [
                {"method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "contract-smoke", "version": "1"}}},
                {"method": "tools/list"},
                {"method": "tools/call", "params": {"name": "endmemex_search", "arguments": {"action": "query", "query": "missing", "semantic": "off"}}},
                {"method": "tools/call", "params": {"name": "endmemex_context", "arguments": {"action": "handoff", "project": "P", "all_paused": False}}},
                {"method": "tools/call", "params": {"name": "endmemex_context", "arguments": {"action": "pending", "all_projects": False}}},
                {"method": "tools/call", "params": {"name": "endmemex_session", "arguments": {"action": "pin", "checkpoint_id": 1, "agent": "codex", "pinned": False}}},
                {"method": "tools/call", "params": {"name": "endmemex_records", "arguments": {"action": "update", "id": "AUDIT-P-1", "agent": "codex", "status": "superseded"}}},
                {"method": "tools/call", "params": {"name": "endmemex_search", "arguments": {"action": "unknown"}}},
                {"method": "ping"},
            ]
            requests = [{"jsonrpc": "2.0", "id": i, **call} for i, call in enumerate(calls, 1)]
            result = subprocess.run(
                [sys.executable, str(ENDMEMEX / "mcp_server.py")],
                input="".join(json.dumps(request) + "\n" for request in requests),
                capture_output=True, text=True, timeout=30,
                env={**os.environ, "ENDEAVOR_DB_PATH": str(database)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            responses = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual([response["id"] for response in responses], list(range(1, 10)))
            tools = responses[1]["result"]["tools"]
            self.assertEqual([tool["name"] for tool in tools], [
                "endmemex_context", "endmemex_search", "endmemex_records", "endmemex_session",
                "endmemex_events", "endmemex_presence", "endmemex_admin",
            ])
            self.assertEqual(json.loads(responses[2]["result"]["content"][0]["text"]), [])
            self.assertIsNone(json.loads(responses[3]["result"]["content"][0]["text"])["session"])
            for response in responses[4:8]:
                self.assertTrue(response["result"]["isError"])
                self.assertTrue(response["result"]["content"][0]["text"].startswith("[error]"))
            self.assertEqual(responses[8]["result"], {})
            # All smoke calls are reads or validation failures: no freshness or
            # activity exports and no semantic model should be started.
            self.assertFalse((Path(directory) / "ACTIVITY.md").exists())


if __name__ == "__main__":
    unittest.main()
