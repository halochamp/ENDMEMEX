# Developer: Poomwat Jarussri
# Email: champoomwat@gmail.com
# GitHub: https://github.com/halochamp
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_delegate
import agent_mcp_server as mcp


class AgentMcpServerContractTest(unittest.TestCase):
    def test_server_exposes_three_strictly_scoped_tools(self):
        self.assertEqual(
            [tool["name"] for tool in mcp.TOOLS],
            ["endeavor_agent_start", "endeavor_agent_status", "endeavor_agent_cancel"],
        )
        for tool in mcp.TOOLS:
            with self.subTest(tool=tool["name"]):
                self.assertFalse(tool["inputSchema"]["additionalProperties"])
                self.assertIn("readOnlyHint", tool["annotations"])
                self.assertIn("openWorldHint", tool["annotations"])
                self.assertNotIn("title", tool)

        self.assertTrue(mcp.TOOL_BY_NAME["endeavor_agent_start"]["annotations"]["openWorldHint"])
        self.assertTrue(mcp.TOOL_BY_NAME["endeavor_agent_start"]["annotations"]["destructiveHint"])
        self.assertTrue(mcp.TOOL_BY_NAME["endeavor_agent_cancel"]["annotations"]["destructiveHint"])
        self.assertIn(mcp.START_USAGE_EXAMPLE, mcp.SERVER_INSTRUCTIONS)
        self.assertIn(mcp.START_USAGE_EXAMPLE, mcp.TOOL_BY_NAME["endeavor_agent_start"]["description"])
        self.assertIn(mcp.MODEL_SELECTION_GUIDANCE, mcp.SERVER_INSTRUCTIONS)
        self.assertIn(mcp.MODEL_SELECTION_GUIDANCE, mcp.TOOL_BY_NAME["endeavor_agent_start"]["description"])
        self.assertIn(mcp.AGENT_OPERATING_PRINCIPLES, mcp.SERVER_INSTRUCTIONS)
        self.assertIn(mcp.AGENT_OPERATING_PRINCIPLES, mcp.START_USAGE_HINT)
        self.assertIn(mcp.AGENT_OPERATING_PRINCIPLES, mcp.TOOL_BY_NAME["endeavor_agent_start"]["description"])

    def test_model_catalog_exposes_copy_ready_values_for_each_target(self):
        self.assertEqual(mcp.MODEL_CATALOG_DATE, "2026-08-30")
        self.assertEqual(
            mcp.CODEX_MODEL_IDS,
            ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"),
        )
        self.assertEqual(
            mcp.CLAUDE_MODEL_IDS,
            ("claude-fable-5", "claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5-20251001"),
        )
        self.assertEqual(
            mcp.CLAUDE_MODEL_ALIASES,
            ("fable", "opus", "sonnet", "haiku"),
        )
        self.assertIn("gemini-3.7-flash-medium", mcp.AGY_MODEL_SLUGS)
        self.assertEqual(mcp.AGY_RECOMMENDED_MODEL, "gemini-3.7-flash-medium")
        for target, example in mcp.START_USAGE_EXAMPLES.items():
            with self.subTest(target=target):
                self.assertIn(f'"target": "{target}"', example)
                self.assertIn('"model": "', example)

    def test_start_schema_accepts_antigravity_target(self):
        self.assertEqual(
            mcp.TOOL_BY_NAME["endeavor_agent_start"]["inputSchema"]["properties"]["target"]["enum"],
            ["codex", "claude", "antigravity"],
        )
        self.assertIsNone(mcp.validate_arguments("endeavor_agent_start", {
            "target": "antigravity", "prompt": "Inspect file",
        }))

    def test_start_forwards_only_read_only_managed_policy(self):
        with mock.patch.object(mcp, "admitted_start", return_value='{"run_id":"abcdefgh"}') as run:
            result = mcp.call("endeavor_agent_start", {
                "target": "claude",
                "prompt": "Review the current diff",
                "role": "reviewer",
                "model": "claude-sonnet-5",
                "reasoning_effort": "high",
                "timeout": 1200,
                "parent_record": "AUDIT-MEM-001",
        })

        self.assertEqual(result, '{"run_id":"abcdefgh"}')
        command = run.call_args.args[0]
        self.assertEqual(command[0], "claude")
        self.assertEqual(command[-2:], ["--", "Review the current diff"])
        self.assertIn("--background", command)
        self.assertIn("--isolated", command)
        self.assertIn("--stream-progress", command)
        self.assertIn("--run-id", command)
        uuid.UUID(hex=command[command.index("--run-id") + 1])
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertEqual(command[command.index("--role") + 1], "reviewer")
        self.assertEqual(command[command.index("--model") + 1], "claude-sonnet-5")
        self.assertEqual(command[command.index("--reasoning-effort") + 1], "high")
        allowed_index = command.index("--allowed-tools")
        self.assertEqual(command[allowed_index:allowed_index + 4], [
            "--allowed-tools", "Read", "Grep", "Glob",
        ])
        self.assertNotIn("--checkpoint", command)
        self.assertNotIn("--permission-mode", command)

    def test_start_defaults_are_bounded_and_read_only(self):
        with mock.patch.object(mcp, "admitted_start", return_value="{}") as run:
            mcp.call("endeavor_agent_start", {"target": "codex", "prompt": "Inspect file"})
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--role") + 1], "worker")
        self.assertEqual(command[command.index("--timeout") + 1], "900")
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")

    def test_codex_workspace_write_is_explicit_and_worker_only(self):
        with mock.patch.object(mcp, "admitted_start", return_value="{}") as run:
            result = mcp.call("endeavor_agent_start", {
                "target": "codex", "prompt": "Implement the bounded change",
                "role": "worker", "access": "workspace_write",
            })
        self.assertEqual(result, "{}")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")
        self.assertNotIn("--permission-mode", command)
        self.assertNotIn("Edit", command)

    def test_claude_workspace_write_is_edit_only_without_shell(self):
        with mock.patch.object(mcp, "admitted_start", return_value="{}") as run:
            result = mcp.call("endeavor_agent_start", {
                "target": "claude", "prompt": "Edit the requested file",
                "role": "worker", "access": "workspace_write",
            })
        self.assertEqual(result, "{}")
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")
        self.assertEqual(
            command[command.index("--permission-mode") + 1], "acceptEdits",
        )
        allowed = command.index("--allowed-tools")
        self.assertEqual(command[allowed:allowed + 4], [
            "--allowed-tools", "Read", "Grep", "Glob",
        ])
        available = command.index("--available-tools")
        self.assertEqual(command[available:available + 6], [
            "--available-tools", "Read", "Grep", "Glob", "Edit", "Write",
        ])
        self.assertNotIn("Bash", command)

    def test_antigravity_workspace_write_forwards_sandbox_without_claude_flags(self):
        # The mcp layer just forwards --sandbox generically for antigravity;
        # the actual --mode/--dangerously-skip-permissions translation
        # happens downstream in delegate_lifecycle.build_command, not here.
        with mock.patch.object(mcp, "admitted_start", return_value="{}") as run:
            result = mcp.call("endeavor_agent_start", {
                "target": "antigravity", "prompt": "Edit the requested file",
                "role": "worker", "access": "workspace_write",
            })
        self.assertEqual(result, "{}")
        command = run.call_args.args[0]
        self.assertEqual(command[0], "antigravity")
        self.assertEqual(command[command.index("--sandbox") + 1], "workspace-write")
        self.assertNotIn("--permission-mode", command)
        self.assertNotIn("--available-tools", command)

    def test_workspace_write_rejects_reviewer_and_advisor_before_launch(self):
        with mock.patch.object(mcp, "admitted_start") as run:
            for role in ("reviewer", "advisor"):
                with self.subTest(role=role):
                    result = mcp.call("endeavor_agent_start", {
                        "target": "codex", "prompt": "Review", "role": role,
                        "access": "workspace_write",
                    })
                    self.assertEqual(
                        result,
                        "[error] workspace_write requires role=worker; "
                        "reviewer and advisor are read-only" + mcp.START_USAGE_HINT,
                    )
        run.assert_not_called()

    def test_prompt_starting_with_dashes_is_terminated_from_options(self):
        with mock.patch.object(mcp, "admitted_start", return_value="{}") as run:
            mcp.call("endeavor_agent_start", {
                "target": "claude", "prompt": "--review the diff",
            })
        self.assertEqual(run.call_args.args[0][-2:], ["--", "--review the diff"])

    def test_status_and_cancel_are_separate_dispatches(self):
        run_id = "12345678abcdef"
        with mock.patch.object(mcp, "run_backend", return_value="{}") as run:
            mcp.call("endeavor_agent_status", {"run_id": run_id})
            mcp.call("endeavor_agent_cancel", {"run_id": run_id})
        self.assertEqual(run.call_args_list[0].args[0], ["status", run_id, "--json"])
        self.assertEqual(run.call_args_list[1].args[0], ["cancel", run_id])

    def test_invalid_arguments_fail_before_dispatch(self):
        with mock.patch.object(mcp, "admitted_start") as run:
            unknown = mcp.call("endeavor_agent_start", {
                "target": "claude", "prompt": "x", "sandbox": "workspace-write",
            })
            bad_target = mcp.call("endeavor_agent_start", {"target": "other", "prompt": "x"})
            bad_timeout = mcp.call("endeavor_agent_start", {
                "target": "codex", "prompt": "x", "timeout": 3601,
            })
            bad_run_id = mcp.call("endeavor_agent_cancel", {"run_id": "../unsafe"})
        self.assertEqual(unknown, "[error] unknown argument(s): sandbox" + mcp.START_USAGE_HINT)
        self.assertIn("target must be one of", bad_target)
        self.assertEqual(bad_timeout, "[error] timeout must be <= 3600" + mcp.START_USAGE_HINT)
        self.assertEqual(bad_run_id, "[error] run_id has an invalid format")
        run.assert_not_called()

    def test_missing_start_arguments_include_a_correct_usage_example(self):
        result = mcp.call("endeavor_agent_start", {})
        self.assertIn("[error] missing required argument(s): target, prompt", result)
        self.assertIn(mcp.START_USAGE_HINT, result)
        self.assertIn(mcp.START_USAGE_EXAMPLE, result)

    def test_backend_timeout_and_invalid_json_are_controlled_errors(self):
        with mock.patch.object(mcp.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 15)):
            self.assertEqual(
                mcp.run_backend(["status", "12345678"]),
                "[error] agent_delegate control command timed out after 15s",
            )
            self.assertEqual(
                mcp.run_backend(["codex", "--background"], recovery_run_id="feedface1234"),
                "[error] agent_delegate control command timed out after 15s; "
                "recover with run_id feedface1234",
            )
        completed = subprocess.CompletedProcess(["x"], 0, stdout="not-json", stderr="")
        with mock.patch.object(mcp.subprocess, "run", return_value=completed):
            self.assertIn("returned invalid JSON", mcp.run_backend(["status", "12345678"]))

    def test_tool_failure_sets_mcp_is_error(self):
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "endeavor_agent_status", "arguments": {"run_id": "bad"}},
        }
        stdin = io.StringIO(json.dumps(request) + "\n")
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout):
            mcp.serve()
        result = json.loads(stdout.getvalue())["result"]
        self.assertTrue(result["isError"])

    def test_annotations_use_compatible_protocol_revision(self):
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        }
        stdin = io.StringIO(json.dumps(request) + "\n")
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout):
            mcp.serve()
        result = json.loads(stdout.getvalue())["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")

    def test_legacy_protocol_omits_newer_tool_annotations(self):
        requests = [
            {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            },
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        stdin = io.StringIO("".join(json.dumps(item) + "\n" for item in requests))
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout):
            mcp.serve()
        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2024-11-05")
        self.assertTrue(all(
            "annotations" not in tool for tool in responses[1]["result"]["tools"]
        ))

    def test_unknown_json_rpc_method_returns_standard_error(self):
        request = {"jsonrpc": "2.0", "id": 9, "method": "unknown/method"}
        stdin = io.StringIO(json.dumps(request) + "\n")
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout):
            mcp.serve()
        response = json.loads(stdout.getvalue())
        self.assertEqual(response["id"], 9)
        self.assertEqual(response["error"]["code"], -32601)

    def test_invalid_json_rpc_id_is_rejected_with_null_id(self):
        request = {"jsonrpc": "2.0", "id": {"bad": True}, "method": "ping"}
        stdin = io.StringIO(json.dumps(request) + "\n")
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout):
            mcp.serve()
        response = json.loads(stdout.getvalue())
        self.assertIsNone(response["id"])
        self.assertEqual(response["error"]["code"], -32600)

    def test_ping_batch_and_notifications_follow_json_rpc_contract(self):
        batch = [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
        stdin = io.StringIO(
            json.dumps(batch) + "\n"
            + json.dumps({"jsonrpc": "2.0", "method": "unknown/notification"}) + "\n"
        )
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout):
            mcp.serve()
        lines = stdout.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        responses = json.loads(lines[0])
        self.assertEqual(responses[0], {"jsonrpc": "2.0", "id": 1, "result": {}})
        self.assertEqual(responses[1]["id"], 2)
        self.assertEqual(len(responses[1]["result"]["tools"]), 3)

    def test_admission_limits_concurrency_and_start_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            runs.mkdir()
            with mock.patch.object(mcp, "RUNS_DIR", runs), \
                 mock.patch.object(mcp, "ADMISSION_LOCK", root / "admission.lock"), \
                 mock.patch.object(mcp, "START_HISTORY", root / "history.json"), \
                 mock.patch.object(mcp, "run_backend", return_value="{}") as backend:
                for index in range(mcp.MAX_CONCURRENT_RUNS):
                    run = runs / f"active{index:04d}"
                    run.mkdir()
                    (run / "state.json").write_text(json.dumps({
                        "status": "running", "manager_pid": __import__("os").getpid(),
                    }))
                blocked = mcp.admitted_start(["claude"], "blocked1234")
                self.assertIn("concurrency limit", blocked)
                backend.assert_not_called()

                for state in runs.glob("*/state.json"):
                    state.write_text(json.dumps({"status": "completed"}))
                for _ in range(mcp.MAX_STARTS_PER_WINDOW):
                    self.assertEqual(mcp.admitted_start(["claude"], "allowed1234"), "{}")
                limited = mcp.admitted_start(["claude"], "limited1234")
                self.assertIn("rate limit", limited)

    def test_active_run_count_ignores_reused_pid_after_owner_death(self):
        # Regression: a dead manager whose persisted child pid has since been
        # reused by an unrelated process must not count as an active run.
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        dead_pid = proc.pid
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            run_dir = runs / "run1"
            run_dir.mkdir()
            (run_dir / "state.json").write_text(json.dumps({
                "status": "running",
                "manager_pid": dead_pid,
                "pid": os.getpid(),
                "process_start_token": "wrong-token",
            }))
            with mock.patch.object(mcp, "RUNS_DIR", runs):
                self.assertEqual(mcp._active_run_count(), 0)

    def test_active_run_count_ignores_reused_pid_without_published_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            run_dir = runs / "run1"
            run_dir.mkdir()
            (run_dir / "state.json").write_text(json.dumps({
                "status": "running",
                "pid": os.getpid(),
                "process_start_token": "wrong-token",
            }))
            with mock.patch.object(mcp, "RUNS_DIR", runs):
                self.assertEqual(mcp._active_run_count(), 0)

    def test_active_run_count_counts_matching_child_identity_after_owner_death(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        dead_pid = proc.pid
        real_token = agent_delegate._process_start_token(os.getpid())
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp)
            run_dir = runs / "run1"
            run_dir.mkdir()
            (run_dir / "state.json").write_text(json.dumps({
                "status": "running",
                "manager_pid": dead_pid,
                "pid": os.getpid(),
                "process_start_token": real_token,
            }))
            with mock.patch.object(mcp, "RUNS_DIR", runs):
                self.assertEqual(mcp._active_run_count(), 1)

    def test_non_object_json_rpc_request_returns_error_and_server_continues(self):
        stdin = io.StringIO('[]\n{"jsonrpc":"2.0","id":1,"method":"initialize"}\n')
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout):
            mcp.serve()

        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], -32600)
        self.assertEqual(responses[1]["result"]["serverInfo"]["name"], "endeavor-agents")


class AgentMcpEndToEndTest(unittest.TestCase):
    def test_stdio_workspace_write_start_audit_progress_and_cancel_cross_real_adapter(self):
        server = Path(mcp.__file__).resolve()
        fixture = server.parent / "developer" / "fake_agent_cli.py"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            os.symlink(fixture, bin_dir / "claude")
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
            env["ENDMEMEX_AGENT_RUNS_DIR"] = str(root / "runs")
            env.pop("ENDEAVOR_DELEGATE_DEPTH", None)
            proc = subprocess.Popen(
                [sys.executable, str(server)], cwd=server.parent.parent,
                env=env, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertIsNotNone(proc.stdin)
            self.assertIsNotNone(proc.stdout)

            def rpc(request):
                proc.stdin.write(json.dumps(request) + "\n")
                proc.stdin.flush()
                return json.loads(proc.stdout.readline())

            run_id = None
            try:
                self.assertEqual(rpc({
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26"},
                })["result"]["protocolVersion"], "2025-03-26")
                started = rpc({
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "endeavor_agent_start", "arguments": {
                        "target": "claude", "prompt": "integration", "timeout": 30,
                        "role": "worker", "access": "workspace_write",
                    }},
                })
                run_id = json.loads(started["result"]["content"][0]["text"])["run_id"]
                observed = None
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    response = rpc({
                        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "endeavor_agent_status",
                                   "arguments": {"run_id": run_id}},
                    })
                    observed = json.loads(response["result"]["content"][0]["text"])
                    if observed.get("progress_tail") == "fixture progress":
                        break
                    time.sleep(0.05)
                self.assertEqual(observed["status"], "running")
                self.assertEqual(observed["progress_tail"], "fixture progress")
                request = json.loads(
                    (root / "runs" / run_id / "request.json").read_text(),
                )
                self.assertEqual(request["role"], "worker")
                self.assertEqual(request["sandbox"], "workspace-write")
                self.assertEqual(
                    request["allowed_tools"], ["Read", "Grep", "Glob"],
                )
                self.assertEqual(
                    request["available_tools"], ["Read", "Grep", "Glob", "Edit", "Write"],
                )
                self.assertEqual(request["permission_mode"], "acceptEdits")
                cancelled = rpc({
                    "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "endeavor_agent_cancel",
                               "arguments": {"run_id": run_id}},
                })
                self.assertEqual(
                    json.loads(cancelled["result"]["content"][0]["text"])["status"],
                    "cancelled",
                )
            finally:
                if run_id:
                    rpc({
                        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
                        "params": {"name": "endeavor_agent_cancel",
                                   "arguments": {"run_id": run_id}},
                    })
                proc.terminate()
                proc.wait(timeout=5)
                proc.stdin.close()
                proc.stdout.close()
                if proc.stderr:
                    proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
