from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp_server as mcp


class McpServerContractTest(unittest.TestCase):
    def test_run_uses_current_interpreter_and_a_bounded_timeout(self):
        completed = subprocess.CompletedProcess(["x"], 0, stdout="{}\n", stderr="")
        with mock.patch.object(mcp.subprocess, "run", return_value=completed) as backend:
            self.assertEqual(mcp.run(["query", "cache"]), "{}")
        self.assertEqual(backend.call_args.args[0], [sys.executable, str(mcp.DB), "query", "cache"])
        self.assertEqual(backend.call_args.kwargs["timeout"], mcp.DB_COMMAND_TIMEOUT_S)

    def test_run_returns_controlled_error_when_backend_times_out(self):
        with mock.patch.object(
            mcp.subprocess, "run", side_effect=subprocess.TimeoutExpired("endeavor_db", 60)
        ):
            self.assertEqual(
                mcp.run(["query", "cache"]),
                "[error] endeavor_db command timed out after 60s",
            )

    def test_run_wraps_launch_and_os_errors_without_changing_timeout_behavior(self):
        failures = (
            OSError("launch failed"),
            FileNotFoundError(2, "missing interpreter"),
            PermissionError(13, "permission denied"),
        )
        for failure in failures:
            with self.subTest(error=type(failure).__name__), mock.patch.object(
                mcp.subprocess, "run", side_effect=failure
            ):
                result = mcp.run(["query", "cache"])
            self.assertTrue(result.startswith("[error] failed to launch endeavor_db: "))
            self.assertIn(str(failure), result)

    def test_server_instructions_cover_cross_tool_workflow(self):
        instructions = mcp.SERVER_INSTRUCTIONS
        self.assertIn("bootstrap", instructions)
        self.assertIn("query", instructions)
        self.assertIn("Checkpoint", instructions)
        self.assertIn("authenticated write_gateway.py", instructions)
        self.assertIn("Never store secrets", instructions)
        self.assertIn("presence is opt-in", instructions)
        self.assertIn("only when the user asks", instructions)
        self.assertNotIn("Call presence_start/presence_heartbeat/presence_stop to announce active work", instructions)

    def test_presence_tool_contract_requires_opt_in(self):
        descriptions = {
            tool["name"]: tool["description"]
            for tool in mcp.TOOLS
            if tool["name"].startswith("endeavor_presence_")
        }
        self.assertIn("Use only when the user explicitly asks", descriptions["endeavor_presence_start"])
        self.assertIn("opted-in presence_start", descriptions["endeavor_presence_heartbeat"])
        self.assertIn("opt-in workflow", descriptions["endeavor_presence_stop"])

    def test_every_tool_has_strict_schema_and_annotations(self):
        self.assertEqual(len(mcp.TOOLS), 23)
        for tool in mcp.TOOLS:
            with self.subTest(tool=tool["name"]):
                self.assertFalse(tool["inputSchema"]["additionalProperties"])
                self.assertIn("readOnlyHint", tool["annotations"])
                self.assertFalse(tool["annotations"]["destructiveHint"])
                self.assertIn("Returns", tool["description"])

    def test_readiness_is_a_read_only_one_call_preflight(self):
        tool = mcp.TOOL_BY_NAME["endeavor_memory_readiness"]
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        self.assertIn("never bootstraps", tool["description"])
        with mock.patch.object(mcp, "run", return_value="{}") as run:
            self.assertEqual(mcp.call("endeavor_memory_readiness", {"project": "DEMO"}), "{}")
        self.assertEqual(run.call_args.args[0], ["readiness", "--project", "DEMO", "--json"])

    def test_query_exposes_and_forwards_full_retrieval_scope(self):
        with mock.patch.object(mcp, "run", return_value="[]") as run:
            result = mcp.call("endeavor_memory_query", {
                "query": "cache race",
                "project": "DEMO",
                "category": "debugging",
                "status": "resolved",
                "module": "react.py",
                "bug_id": "BUG-1",
                "session_label": "S1",
                "semantic": "on",
                "limit": 7,
                "compact": False,
                "check_stale": True,
            })
        self.assertEqual(result, "[]")
        self.assertEqual(run.call_args.args[0], [
            "query", "cache race", "--json", "--project", "DEMO",
            "--category", "debugging", "--status", "resolved",
            "--module", "react.py", "--bug-id", "BUG-1",
            "--session-label", "S1", "--semantic", "on", "--limit", "7",
            "--check-stale",
        ])

    def test_query_checks_staleness_by_default_and_allows_explicit_opt_out(self):
        with mock.patch.object(mcp, "run", return_value="[]") as run:
            mcp.call("endeavor_memory_query", {"query": "cache"})
            mcp.call("endeavor_memory_query", {"query": "cache", "check_stale": False})
        self.assertIn("--check-stale", run.call_args_list[0].args[0])
        self.assertNotIn("--check-stale", run.call_args_list[1].args[0])
        self.assertIn("--no-check-stale", run.call_args_list[1].args[0])

    def test_record_mutation_session_close_and_feedback_have_mcp_parity(self):
        calls = [
            ("endeavor_memory_record_update", {
                "id": "AUDIT-DEMO-1", "agent": "codex", "status": "resolved",
                "action_state": "done", "metadata": {"source": "audit.md"},
            }),
            ("endeavor_memory_record_link", {
                "source_id": "FIX-DEMO-1", "relation": "resolves",
                "target_id": "AUDIT-DEMO-1", "note": "closed", "agent": "codex",
            }),
            ("endeavor_memory_session_close", {
                "project": "DEMO", "status": "completed", "agent": "codex",
            }),
            ("endeavor_memory_feedback", {
                "agent": "codex", "query": "cache", "results": [7, "AUDIT-DEMO-1"],
                "useful": True, "note": "used both",
            }),
        ]
        with mock.patch.object(mcp, "run", return_value="{}") as run:
            for name, arguments in calls:
                self.assertEqual(mcp.call(name, arguments), "{}")
        self.assertEqual(run.call_args_list[0].args[0], [
            "record-update", "AUDIT-DEMO-1", "--agent", "codex", "--status", "resolved",
            "--action-state", "done", "--metadata", '{"source": "audit.md"}',
        ])
        self.assertEqual(run.call_args_list[1].args[0], [
            "record-link", "FIX-DEMO-1", "resolves", "AUDIT-DEMO-1",
            "--agent", "codex", "--note", "closed",
        ])
        self.assertEqual(run.call_args_list[2].args[0], [
            "session-close", "--agent", "codex", "--project", "DEMO", "--status", "completed",
        ])
        self.assertEqual(run.call_args_list[3].args[0], [
            "feedback", "--agent", "codex", "--query", "cache", "--result", "7",
            "--result", "AUDIT-DEMO-1", "--useful", "yes", "--note", "used both",
        ])

    def test_record_update_requires_a_mutation_and_session_close_requires_one_scope(self):
        self.assertIn("at least one", mcp.call(
            "endeavor_memory_record_update", {"id": "AUDIT-DEMO-1", "agent": "codex"},
        ))
        self.assertIn("exactly one", mcp.call(
            "endeavor_memory_session_close", {"agent": "codex"},
        ))

    def test_durable_event_poll_and_ack_forward_host_cursor(self):
        with mock.patch.object(mcp, "run", return_value="{}") as run:
            mcp.call("endeavor_memory_event_poll", {
                "after": 12, "project": "DEMO", "limit": 30, "include_acked": True,
            })
            mcp.call("endeavor_memory_event_ack", {"event_id": 19, "agent": "codex"})
        self.assertEqual(run.call_args_list[0].args[0], [
            "event-poll", "--json", "--after", "12", "--project", "DEMO",
            "--limit", "30", "--include-acked",
        ])
        self.assertEqual(run.call_args_list[1].args[0], [
            "event-ack", "19", "--agent", "codex",
        ])

    def test_record_search_forwards_lifecycle_filters(self):
        with mock.patch.object(mcp, "run", return_value="[]") as run:
            result = mcp.call("endeavor_memory_record_search", {
                "query": "embedding audit",
                "project": "ENDMEMEX",
                "type": "audit",
                "limit": 12,
                "current_only": True,
            })
        self.assertEqual(result, "[]")
        self.assertEqual(run.call_args.args[0], [
            "record-search", "embedding audit", "--project", "ENDMEMEX",
            "--type", "audit", "--limit", "12", "--current-only",
        ])

    def test_pending_requires_one_scope_and_forwards_read_only_command(self):
        self.assertEqual(len(mcp.TOOL_BY_NAME["endeavor_memory_pending"]["inputSchema"]["oneOf"]), 2)
        with mock.patch.object(mcp, "run", return_value="{}") as run:
            invalid = mcp.call("endeavor_memory_pending", {})
            both = mcp.call("endeavor_memory_pending", {"project": "DEMO", "all_projects": True})
            project = mcp.call("endeavor_memory_pending", {"project": "DEMO"})
            all_projects = mcp.call("endeavor_memory_pending", {"all_projects": True})
        self.assertIn("exactly one", invalid)
        self.assertIn("exactly one", both)
        self.assertEqual(project, "{}")
        self.assertEqual(all_projects, "{}")
        self.assertEqual(run.call_args_list[0].args[0], ["pending", "--json", "--project", "DEMO"])
        self.assertEqual(run.call_args_list[1].args[0], ["pending", "--json", "--all-projects"])

    def test_handoff_requires_one_scope_and_exposes_all_paused_and_session_modes(self):
        with mock.patch.object(mcp, "run", return_value="{}") as run:
            invalid = mcp.call("endeavor_memory_handoff", {})
            both = mcp.call("endeavor_memory_handoff", {"project": "DEMO", "session": "sess-1"})
            project = mcp.call("endeavor_memory_handoff", {"project": "DEMO"})
            session = mcp.call("endeavor_memory_handoff", {"session": "sess-1"})
            all_paused = mcp.call("endeavor_memory_handoff", {"all_paused": True})
        self.assertIn("exactly one", invalid)
        self.assertIn("exactly one", both)
        self.assertEqual(project, "{}")
        self.assertEqual(session, "{}")
        self.assertEqual(all_paused, "{}")
        self.assertEqual(run.call_args_list[0].args[0], ["handoff", "--project", "DEMO", "--json"])
        self.assertEqual(run.call_args_list[1].args[0], ["handoff", "--session", "sess-1", "--json"])
        self.assertEqual(run.call_args_list[2].args[0], ["handoff", "--all-paused", "--json"])

    def test_timeline_forwards_all_filters_and_is_read_only(self):
        self.assertEqual(mcp.TOOL_BY_NAME["endeavor_memory_timeline"]["annotations"]["readOnlyHint"], True)
        self.assertNotIn("endeavor_memory_timeline", mcp.WRITE_TOOLS)
        with mock.patch.object(mcp, "run", return_value="{}") as run:
            result = mcp.call("endeavor_memory_timeline", {
                "project": "DEMO", "agent": "codex", "status": "paused",
                "session": "sess-1", "limit": 50, "oldest_first": True,
            })
        self.assertEqual(result, "{}")
        self.assertEqual(run.call_args.args[0], [
            "timeline", "--json", "--project", "DEMO", "--agent", "codex",
            "--status", "paused", "--session", "sess-1", "--limit", "50", "--oldest-first",
        ])

    def test_timeline_with_no_filters_forwards_bare_command(self):
        with mock.patch.object(mcp, "run", return_value="{}") as run:
            result = mcp.call("endeavor_memory_timeline", {})
        self.assertEqual(result, "{}")
        self.assertEqual(run.call_args.args[0], ["timeline", "--json"])

    def test_timeline_forwards_without_host_role_gate(self):
        with mock.patch.object(mcp, "run", return_value="{}") as run:
            result = mcp.call("endeavor_memory_timeline", {"project": "DEMO"})
        self.assertEqual(result, "{}")
        run.assert_called_once()

    def test_bootstrap_can_opt_into_pending_context(self):
        with mock.patch.object(mcp, "run", return_value="{}") as run:
            result = mcp.call("endeavor_memory_bootstrap", {
                "project": "DEMO", "session": "sess-1", "include_pending": True,
            })
        self.assertEqual(result, "{}")
        self.assertEqual(run.call_args.args[0], [
            "bootstrap", "--project", "DEMO", "--json", "--session", "sess-1", "--include-pending",
        ])

    def test_pack_and_checkpoint_forward_explicit_session_identity(self):
        with mock.patch.object(mcp, "run", return_value="{}") as run:
            mcp.call("endeavor_memory_pack", {"project": "DEMO", "session": "sess-1", "budget": 900})
            mcp.call("endeavor_memory_checkpoint", {
                "project": "DEMO", "session": "sess-1", "agent": "codex", "summary": "saved",
            })
        self.assertEqual(run.call_args_list[0].args[0], [
            "pack", "--project", "DEMO", "--json", "--session", "sess-1", "--budget", "900",
        ])
        self.assertEqual(run.call_args_list[1].args[0], [
            "checkpoint", "--project", "DEMO", "--agent", "codex", "--summary", "saved",
            "--session", "sess-1",
        ])

    def test_invalid_arguments_fail_before_dispatch(self):
        with mock.patch.object(mcp, "run") as run:
            unknown = mcp.call("endeavor_memory_query", {"query": "x", "typo": True})
            bad_enum = mcp.call("endeavor_memory_query", {"query": "x", "semantic": "maybe"})
            bad_budget = mcp.call("endeavor_memory_pack", {"project": "DEMO", "budget": 100000})
        self.assertEqual(unknown, "[error] unknown argument(s): typo")
        self.assertIn("semantic must be one of", bad_enum)
        self.assertEqual(bad_budget, "[error] budget must be <= 50000")
        run.assert_not_called()

    def test_presence_start_forwards_all_fields(self):
        with mock.patch.object(mcp, "run", return_value="{}") as run:
            mcp.call("endeavor_presence_start", {
                "agent": "claude", "project": "DEMO", "task": "writing tests",
                "instance": "a", "session": "sess-1",
            })
        self.assertEqual(run.call_args.args[0], [
            "presence-start", "--agent", "claude", "--project", "DEMO",
            "--task", "writing tests", "--instance", "a", "--session", "sess-1",
        ])

    def test_presence_heartbeat_forwards_task_and_instance(self):
        with mock.patch.object(mcp, "run", return_value='{"updated": true}') as run:
            mcp.call("endeavor_presence_heartbeat", {
                "agent": "claude", "project": "DEMO", "task": "still going", "instance": "a",
            })
        self.assertEqual(run.call_args.args[0], [
            "presence-heartbeat", "--agent", "claude", "--project", "DEMO",
            "--task", "still going", "--instance", "a",
        ])

    def test_presence_heartbeat_omits_task_flag_when_not_provided(self):
        # task=None must mean "just refresh the timestamp" (matches the CLI's
        # own None-vs-empty-string distinction), not "--task ''".
        with mock.patch.object(mcp, "run", return_value='{"updated": true}') as run:
            mcp.call("endeavor_presence_heartbeat", {"agent": "claude", "project": "DEMO"})
        self.assertEqual(run.call_args.args[0], [
            "presence-heartbeat", "--agent", "claude", "--project", "DEMO",
        ])

    def test_presence_stop_forwards_instance(self):
        with mock.patch.object(mcp, "run", return_value='{"updated": true}') as run:
            mcp.call("endeavor_presence_stop", {"agent": "claude", "project": "DEMO", "instance": "a"})
        self.assertEqual(run.call_args.args[0], [
            "presence-stop", "--agent", "claude", "--project", "DEMO", "--instance", "a",
        ])

    def test_presence_list_forwards_optional_project(self):
        with mock.patch.object(mcp, "run", return_value="{}") as run:
            mcp.call("endeavor_presence_list", {})
        self.assertEqual(run.call_args.args[0], ["presence", "--json"])
        with mock.patch.object(mcp, "run", return_value="{}") as run:
            mcp.call("endeavor_presence_list", {"project": "DEMO"})
        self.assertEqual(run.call_args.args[0], ["presence", "--json", "--project", "DEMO"])

    def test_sync_status_takes_no_arguments(self):
        with mock.patch.object(mcp, "run", return_value="{}") as run:
            result = mcp.call("endeavor_sync_status", {})
        self.assertEqual(result, "{}")
        self.assertEqual(run.call_args.args[0], ["sync-status", "--json"])
        rejected = mcp.call("endeavor_sync_status", {"project": "DEMO"})
        self.assertIn("unknown argument", rejected)

    def test_presence_list_and_sync_status_are_read_only(self):
        with mock.patch.object(mcp, "run", return_value="{}") as run:
            result_list = mcp.call("endeavor_presence_list", {})
            result_status = mcp.call("endeavor_sync_status", {})
        self.assertEqual(result_list, "{}")
        self.assertEqual(result_status, "{}")
        self.assertEqual(run.call_count, 2)

    def test_presence_start_forwards_with_or_without_legacy_confirm(self):
        with mock.patch.object(mcp, "run", return_value='{"updated": true}') as run:
            plain = mcp.call("endeavor_presence_start", {"agent": "claude", "project": "DEMO"})
            confirmed = mcp.call(
                "endeavor_presence_start", {"agent": "claude", "project": "DEMO", "confirm": True},
            )
        self.assertEqual(plain, '{"updated": true}')
        self.assertEqual(confirmed, '{"updated": true}')
        self.assertEqual(run.call_count, 2)

    def test_tool_failure_sets_mcp_is_error(self):
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "endeavor_memory_query", "arguments": {}},
        }
        stdin = io.StringIO(json.dumps(request) + "\n")
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout):
            mcp.serve()
        result = json.loads(stdout.getvalue())["result"]
        self.assertTrue(result["isError"])

    def test_unknown_json_rpc_method_returns_standard_error(self):
        request = {"jsonrpc": "2.0", "id": 9, "method": "unknown/method"}
        stdin = io.StringIO(json.dumps(request) + "\n")
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout):
            mcp.serve()
        response = json.loads(stdout.getvalue())
        self.assertEqual(response["id"], 9)
        self.assertEqual(response["error"]["code"], -32601)

    def test_ping_and_notifications_follow_json_rpc_contract(self):
        requests = [
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "method": "unknown/notification"},
            {"jsonrpc": "2.0", "id": 7, "method": "ping"},
        ]
        stdin = io.StringIO("".join(json.dumps(item) + "\n" for item in requests))
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout):
            mcp.serve()
        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(responses, [{"jsonrpc": "2.0", "id": 7, "result": {}}])

    def test_non_object_json_rpc_request_returns_error_and_server_continues(self):
        stdin = io.StringIO('[]\n{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), \
             mock.patch.object(sys, "stdout", stdout):
            mcp.serve()

        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(responses[0]["id"], None)
        self.assertEqual(responses[0]["error"]["code"], -32000)
        self.assertIn("JSON object", responses[0]["error"]["message"])
        self.assertEqual(responses[1]["id"], 1)
        self.assertIn("result", responses[1])


if __name__ == "__main__":
    unittest.main()
