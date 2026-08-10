"""Tests for agent_delegate.py — no real CLI is spawned."""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_delegate  # noqa: E402


def run_main(argv, **env):
    with tempfile.TemporaryDirectory() as tmp_dir, \
         mock.patch.object(agent_delegate, "RUNS_DIR", Path(tmp_dir)), \
         mock.patch.object(sys, "argv", ["agent_delegate.py", *argv]), \
         mock.patch.dict(os.environ, env, clear=True):
        return agent_delegate.main()


def managed_child(stdout="", stderr="", code=0, captured=None):
    def fake(
        cmd, cwd, env, timeout, stdout_path, stderr_path,
        on_started=None, should_cancel=None,
    ):
        stdout_path.write_text(stdout)
        stderr_path.write_text(stderr)
        if captured is not None:
            captured.update({"cmd": cmd, "cwd": cwd, "env": env, "timeout": timeout})
        if on_started:
            on_started(4242)
        return code, 4242
    return fake


def delegate_args(*extra):
    return agent_delegate.build_delegate_parser().parse_args(["claude", "task", *extra])


class BuildCommandTest(unittest.TestCase):
    def _args(self, **overrides):
        base = dict(target="codex", prompt="do it", cwd="/tmp", json=False,
                    sandbox="read-only", model=None, allowed_tools=None,
                    available_tools=None,
                    reasoning_effort=None, permission_mode=None, isolated=False,
                    stream_progress=False)
        base.update(overrides)
        return mock.Mock(**base)

    def test_codex_command(self):
        cmd = agent_delegate.build_command(self._args(), "/bin/codex")
        self.assertEqual(cmd, ["/bin/codex", "exec", "-C", "/tmp", "-s",
                               "read-only", "--skip-git-repo-check", "-c",
                               'model_reasoning_effort="medium"', "--", "do it"])

    def test_codex_never_uses_removed_approval_flag(self):
        cmd = agent_delegate.build_command(self._args(), "/bin/codex")
        self.assertNotIn("-a", cmd)

    def test_isolated_codex_ignores_ambient_config_and_persistence(self):
        cmd = agent_delegate.build_command(self._args(isolated=True), "/bin/codex")
        self.assertIn("--ignore-user-config", cmd)
        self.assertIn("--ignore-rules", cmd)
        self.assertIn("--ephemeral", cmd)

    def test_codex_stream_progress_uses_jsonl_events(self):
        cmd = agent_delegate.build_command(
            self._args(stream_progress=True), "/bin/codex",
        )
        self.assertIn("--json", cmd)

    def test_codex_model_is_forwarded_without_allowlist(self):
        cmd = agent_delegate.build_command(
            self._args(model="gpt-future-codex"), "/bin/codex",
        )
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "gpt-future-codex")

    def test_codex_reasoning_effort_is_forwarded_as_config(self):
        cmd = agent_delegate.build_command(
            self._args(model="gpt-5.6-sol", reasoning_effort="high"), "/bin/codex",
        )
        self.assertEqual(
            cmd[cmd.index("-c") + 1], 'model_reasoning_effort="high"',
        )

    def test_claude_command(self):
        args = self._args(target="claude", allowed_tools=["Read", "Grep"],
                          permission_mode="plan")
        cmd = agent_delegate.build_command(args, "/bin/claude")
        self.assertEqual(cmd, ["/bin/claude", "-p", "--model", "haiku",
                               "--effort", "medium",
                               "--tools", "Read,Grep",
                               "--allowedTools", "Read", "Grep",
                               "--permission-mode", "plan", "--", "do it"])

    def test_claude_separates_available_tools_from_preapproved_tools(self):
        args = self._args(
            target="claude",
            available_tools=["Read", "Grep", "Glob", "Edit", "Write"],
            allowed_tools=["Read", "Grep", "Glob"],
            permission_mode="acceptEdits",
        )
        cmd = agent_delegate.build_command(args, "/bin/claude")
        self.assertEqual(
            cmd[cmd.index("--tools") + 1], "Read,Grep,Glob,Edit,Write",
        )
        allowed = cmd.index("--allowedTools")
        self.assertEqual(cmd[allowed:allowed + 4], [
            "--allowedTools", "Read", "Grep", "Glob",
        ])
        self.assertNotIn("Edit", cmd[allowed + 1:cmd.index("--permission-mode")])

    def test_isolated_claude_disables_customizations_mcp_and_persistence(self):
        cmd = agent_delegate.build_command(
            self._args(target="claude", isolated=True), "/bin/claude",
        )
        self.assertIn("--safe-mode", cmd)
        self.assertIn("--no-session-persistence", cmd)
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--mcp-config") + 1], '{"mcpServers":{}}')

    def test_claude_stream_progress_uses_partial_stream_json(self):
        cmd = agent_delegate.build_command(
            self._args(target="claude", stream_progress=True), "/bin/claude",
        )
        self.assertEqual(cmd[cmd.index("--output-format") + 1], "stream-json")
        self.assertIn("--include-partial-messages", cmd)
        self.assertIn("--verbose", cmd)

    def test_claude_full_model_id_is_forwarded_without_allowlist(self):
        cmd = agent_delegate.build_command(
            self._args(target="claude", model="claude-future-1"), "/bin/claude",
        )
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-future-1")

    def test_claude_reasoning_effort_is_forwarded(self):
        cmd = agent_delegate.build_command(
            self._args(target="claude", reasoning_effort="high"), "/bin/claude",
        )
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")

    def test_target_specific_model_defaults(self):
        self.assertIsNone(agent_delegate.effective_model(self._args()))
        self.assertEqual(
            agent_delegate.effective_model(self._args(target="claude")), "haiku",
        )

    def test_reasoning_effort_defaults_to_medium_for_both_targets(self):
        self.assertEqual(agent_delegate.effective_reasoning_effort(self._args()), "medium")
        self.assertEqual(
            agent_delegate.effective_reasoning_effort(self._args(target="claude")), "medium",
        )

    def test_claude_default_has_no_available_tools(self):
        cmd = agent_delegate.build_command(
            self._args(target="claude"), "/bin/claude",
        )
        index = cmd.index("--tools")
        self.assertEqual(cmd[index:index + 2], ["--tools", ""])
        self.assertEqual(cmd[-2:], ["--", "do it"])

    def test_json_flags(self):
        self.assertIn("--json", agent_delegate.build_command(
            self._args(json=True), "/bin/codex"))
        self.assertIn("--output-format", agent_delegate.build_command(
            self._args(target="claude", json=True), "/bin/claude"))

    def test_prompt_starting_with_dash_is_after_target_option_terminator(self):
        for target, binary in (("codex", "/bin/codex"), ("claude", "/bin/claude")):
            with self.subTest(target=target):
                cmd = agent_delegate.build_command(
                    self._args(target=target, prompt="--help"), binary,
                )
                self.assertEqual(cmd[-2:], ["--", "--help"])


class PositiveIntTest(unittest.TestCase):
    def test_accepts_positive(self):
        self.assertEqual(agent_delegate.positive_int("5"), 5)

    def test_rejects_zero(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            agent_delegate.positive_int("0")

    def test_rejects_negative(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            agent_delegate.positive_int("-3")

    def test_rejects_non_integer(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            agent_delegate.positive_int("soon")

    def test_timeout_flag_rejects_zero_at_parse_time(self):
        with self.assertRaises(SystemExit):
            run_main(["codex", "hi", "--timeout", "0"])


class ParseDepthTest(unittest.TestCase):
    def test_default_zero(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(agent_delegate.parse_depth(), 0)

    def test_parses_valid_int(self):
        with mock.patch.dict(os.environ, {agent_delegate.DEPTH_ENV: "1"}):
            self.assertEqual(agent_delegate.parse_depth(), 1)

    def test_garbage_value_fails_closed(self):
        with mock.patch.dict(os.environ, {agent_delegate.DEPTH_ENV: "not-a-number"}):
            self.assertEqual(agent_delegate.parse_depth(), agent_delegate.MAX_DEPTH)

    def test_negative_value_clamped_to_zero(self):
        with mock.patch.dict(os.environ, {agent_delegate.DEPTH_ENV: "-7"}):
            self.assertEqual(agent_delegate.parse_depth(), 0)


class InferCallerTest(unittest.TestCase):
    def test_caller_env_overrides_ambient_claudecode(self):
        # Regression: CLAUDECODE=1 is inherited by every descendant process
        # (confirmed live in this repo's own shell), so a Codex child must
        # not be misreported as "claude" just because that var is present.
        with mock.patch.dict(os.environ,
                             {agent_delegate.CALLER_ENV: "codex", "CLAUDECODE": "1"}):
            self.assertEqual(agent_delegate.infer_caller(), "codex")

    def test_falls_back_to_claudecode_when_unset(self):
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}, clear=True):
            self.assertEqual(agent_delegate.infer_caller(), "claude")

    def test_falls_back_to_codex_prefixed_vars(self):
        with mock.patch.dict(os.environ, {"CODEX_SESSION_ID": "x"}, clear=True):
            self.assertEqual(agent_delegate.infer_caller(), "codex")

    def test_unknown_when_nothing_present(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(agent_delegate.infer_caller(), "unknown")


class RecursionGuardTest(unittest.TestCase):
    def test_refuses_at_max_depth(self):
        with mock.patch.object(agent_delegate, "append_log") as log:
            code = run_main(["codex", "hi"], ENDEAVOR_DELEGATE_DEPTH="1")
        self.assertEqual(code, 2)
        log.assert_called_once()
        self.assertEqual(log.call_args[0][0]["exit_code"], 2)

    def test_child_env_increments_depth(self):
        captured = {}

        with mock.patch.object(agent_delegate, "run_child_to_files",
                               managed_child(stdout="ok", captured=captured)), \
             mock.patch.object(agent_delegate, "find_binary", return_value="/bin/x"), \
             mock.patch.object(agent_delegate, "_binary_version", return_value="test"), \
             mock.patch.object(agent_delegate, "append_log"):
            code = run_main(["codex", "hi"])
        self.assertEqual(code, 0)
        self.assertEqual(captured["env"][agent_delegate.DEPTH_ENV], "1")

    def test_run_main_ignores_inherited_delegate_depth(self):
        captured = {}

        # A sub-agent inherits this marker from its parent. The test helper
        # must still test a fresh top-level invocation unless a test passes a
        # depth explicitly through run_main().
        with mock.patch.dict(os.environ, {
            agent_delegate.DEPTH_ENV: "1",
            agent_delegate.CALLER_ENV: "claude",
        }, clear=False), \
             mock.patch.object(agent_delegate, "run_child_to_files",
                               managed_child(stdout="ok", captured=captured)), \
             mock.patch.object(agent_delegate, "find_binary", return_value="/bin/x"), \
             mock.patch.object(agent_delegate, "_binary_version", return_value="test"), \
             mock.patch.object(agent_delegate, "append_log"):
            code = run_main(["codex", "hi"])
        self.assertEqual(code, 0)
        self.assertEqual(captured["env"][agent_delegate.DEPTH_ENV], "1")

    def test_child_env_carries_explicit_caller_identity(self):
        captured = {}

        with mock.patch.object(agent_delegate, "run_child_to_files",
                               managed_child(stdout="ok", captured=captured)), \
             mock.patch.object(agent_delegate, "find_binary", return_value="/bin/x"), \
             mock.patch.object(agent_delegate, "_binary_version", return_value="test"), \
             mock.patch.object(agent_delegate, "append_log"):
            code = run_main(["codex", "hi"])
        self.assertEqual(code, 0)
        self.assertEqual(captured["env"][agent_delegate.CALLER_ENV], "codex")


class RunChildTest(unittest.TestCase):
    def test_state_callback_failure_kills_already_started_file_child(self):
        class FakeProc:
            pid = 4242

            def wait(self, timeout=None):
                return -signal.SIGKILL

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(agent_delegate.subprocess, "Popen",
                               return_value=FakeProc()) as popen, \
             mock.patch.object(agent_delegate.os, "killpg") as killpg:
            stdout_path = Path(tmp) / "stdout.log"
            stderr_path = Path(tmp) / "stderr.log"
            code, pid = agent_delegate.run_child_to_files(
                ["x"], "/tmp", {}, 5, stdout_path, stderr_path,
                on_started=mock.Mock(side_effect=OSError("state disk full")),
            )
            stderr = stderr_path.read_text()
        self.assertEqual((code, pid), (126, 4242))
        self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        killpg.assert_called_once_with(4242, signal.SIGKILL)
        self.assertIn("state disk full", stderr)

    def test_state_callback_failure_without_confirmed_reap_is_orphaned(self):
        class FakeProc:
            pid = 4242

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(agent_delegate.subprocess, "Popen", return_value=FakeProc()), \
             mock.patch.object(agent_delegate.os, "killpg", side_effect=PermissionError("denied")):
            code, pid = agent_delegate.run_child_to_files(
                ["x"], "/tmp", {}, 5, Path(tmp) / "stdout.log", Path(tmp) / "stderr.log",
                on_started=mock.Mock(side_effect=OSError("state disk full")),
            )
        self.assertEqual((code, pid), (agent_delegate.EXIT_REAP_FAILED, 4242))

    def test_interrupt_kills_file_child_group_and_returns_130(self):
        calls = 0

        class FakeProc:
            pid = 4242

            def wait(self, timeout=None):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise KeyboardInterrupt
                return -signal.SIGKILL

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(agent_delegate.subprocess, "Popen", return_value=FakeProc()), \
             mock.patch.object(agent_delegate.os, "killpg") as killpg:
            stdout_path = Path(tmp) / "stdout.log"
            stderr_path = Path(tmp) / "stderr.log"
            code, pid = agent_delegate.run_child_to_files(
                ["x"], "/tmp", {}, 5, stdout_path, stderr_path,
            )
            stderr = stderr_path.read_text()
        self.assertEqual((code, pid), (130, 4242))
        self.assertIn("interrupt", stderr)
        killpg.assert_called_once_with(4242, signal.SIGKILL)

    def test_file_child_honors_manager_owned_cancel_callback(self):
        calls = 0

        class FakeProc:
            pid = 4242

            def wait(self, timeout=None):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)
                return -signal.SIGTERM

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(agent_delegate.subprocess, "Popen", return_value=FakeProc()), \
             mock.patch.object(agent_delegate.os, "killpg") as killpg:
            stdout_path = Path(tmp) / "stdout.log"
            stderr_path = Path(tmp) / "stderr.log"
            code, pid = agent_delegate.run_child_to_files(
                ["x"], "/tmp", {}, 5, stdout_path, stderr_path,
                should_cancel=lambda: True,
            )
        self.assertEqual((code, pid), (130, 4242))
        killpg.assert_called_once_with(4242, signal.SIGTERM)

    def test_file_child_cancel_permission_failure_is_not_terminal_cancelled(self):
        class FakeProc:
            pid = 4242

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(agent_delegate.subprocess, "Popen", return_value=FakeProc()), \
             mock.patch.object(agent_delegate.os, "killpg", side_effect=PermissionError("denied")):
            stdout_path = Path(tmp) / "stdout.log"
            stderr_path = Path(tmp) / "stderr.log"
            code, pid = agent_delegate.run_child_to_files(
                ["x"], "/tmp", {}, 5, stdout_path, stderr_path,
                should_cancel=lambda: True,
            )
            stderr = stderr_path.read_text()
        self.assertEqual((code, pid), (agent_delegate.EXIT_REAP_FAILED, 4242))
        self.assertIn("permission denied", stderr)

    def test_file_child_not_reported_cancelled_until_sigkill_is_reaped(self):
        class FakeProc:
            pid = 4242

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(agent_delegate.subprocess, "Popen", return_value=FakeProc()), \
             mock.patch.object(agent_delegate.os, "killpg") as killpg:
            stdout_path = Path(tmp) / "stdout.log"
            stderr_path = Path(tmp) / "stderr.log"
            code, pid = agent_delegate.run_child_to_files(
                ["x"], "/tmp", {}, 5, stdout_path, stderr_path,
                should_cancel=lambda: True,
            )
            stderr = stderr_path.read_text()
        self.assertEqual((code, pid), (agent_delegate.EXIT_REAP_FAILED, 4242))
        self.assertEqual(killpg.call_args_list, [
            mock.call(4242, signal.SIGTERM), mock.call(4242, signal.SIGKILL),
        ])
        self.assertIn("remained after force-cancel", stderr)

    def test_timeout_reap_failure_is_not_reported_as_clean_timeout(self):
        class FakeProc:
            pid = 4242

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(agent_delegate.subprocess, "Popen", return_value=FakeProc()), \
             mock.patch.object(agent_delegate.time, "monotonic", side_effect=[0.0, 6.0]), \
             mock.patch.object(agent_delegate.os, "killpg") as killpg:
            stdout_path = Path(tmp) / "stdout.log"
            stderr_path = Path(tmp) / "stderr.log"
            code, pid = agent_delegate.run_child_to_files(
                ["x"], "/tmp", {}, 5, stdout_path, stderr_path,
            )
            stderr = stderr_path.read_text()
        self.assertEqual((code, pid), (agent_delegate.EXIT_REAP_FAILED, 4242))
        killpg.assert_called_once_with(4242, signal.SIGKILL)
        self.assertIn("remained after timeout", stderr)

    def test_timeout_signal_permission_failure_is_reap_failure(self):
        class FakeProc:
            pid = 4242

            def wait(self, timeout=None):
                return 0

        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(agent_delegate.subprocess, "Popen", return_value=FakeProc()), \
             mock.patch.object(agent_delegate.time, "monotonic", side_effect=[0.0, 6.0]), \
             mock.patch.object(agent_delegate.os, "killpg", side_effect=PermissionError("denied")):
            stdout_path = Path(tmp) / "stdout.log"
            stderr_path = Path(tmp) / "stderr.log"
            code, pid = agent_delegate.run_child_to_files(
                ["x"], "/tmp", {}, 5, stdout_path, stderr_path,
            )
        self.assertEqual((code, pid), (agent_delegate.EXIT_REAP_FAILED, 4242))

    def test_output_artifacts_are_capped_and_child_is_reaped(self):
        calls = 0
        with tempfile.TemporaryDirectory() as tmp:
            stdout_path = Path(tmp) / "stdout.log"
            stderr_path = Path(tmp) / "stderr.log"

            class FakeProc:
                pid = 4242

                def wait(self, timeout=None):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        stdout_path.write_text("x" * 100)
                        raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)
                    return -signal.SIGKILL

            with mock.patch.object(agent_delegate.subprocess, "Popen", return_value=FakeProc()), \
                 mock.patch.object(agent_delegate, "MAX_ARTIFACT_BYTES_PER_STREAM", 64), \
                 mock.patch.object(agent_delegate.os, "killpg") as killpg:
                code, pid = agent_delegate.run_child_to_files(
                    ["x"], "/tmp", {}, 5, stdout_path, stderr_path,
                )
            self.assertEqual((code, pid), (agent_delegate.EXIT_ARTIFACT_LIMIT, 4242))
            self.assertLessEqual(stdout_path.stat().st_size, 64)
            killpg.assert_called_once_with(4242, signal.SIGKILL)

    def test_real_pipe_reader_enforces_hard_output_cap(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(agent_delegate, "MAX_ARTIFACT_BYTES_PER_STREAM", 1024):
            stdout_path = Path(tmp) / "stdout.log"
            stderr_path = Path(tmp) / "stderr.log"
            code, pid = agent_delegate.run_child_to_files([
                sys.executable, "-c",
                "import os,time; os.write(1, b'x' * 8192); time.sleep(10)",
            ], tmp, os.environ.copy(), 5, stdout_path, stderr_path)
            self.assertEqual(code, agent_delegate.EXIT_ARTIFACT_LIMIT)
            self.assertIsInstance(pid, int)
            self.assertLessEqual(stdout_path.stat().st_size, 1024)
            self.assertFalse(agent_delegate._process_exists(pid))

    def test_artifact_cap_does_not_limit_unrelated_child_files(self):
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.object(agent_delegate, "MAX_ARTIFACT_BYTES_PER_STREAM", 1024):
            stdout_path = Path(tmp) / "stdout.log"
            stderr_path = Path(tmp) / "stderr.log"
            unrelated = Path(tmp) / "child-data.bin"
            code, _ = agent_delegate.run_child_to_files([
                sys.executable, "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'x' * 8192)",
                str(unrelated),
            ], tmp, os.environ.copy(), 5, stdout_path, stderr_path)
            self.assertEqual(code, 0)
            self.assertEqual(unrelated.stat().st_size, 8192)

    def test_progress_snapshot_normalizes_claude_partial_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.log"
            events = [
                {"type": "stream_event", "event": {
                    "type": "content_block_delta", "delta": {"text": "first "},
                }},
                {"type": "stream_event", "event": {
                    "type": "content_block_delta", "delta": {"text": "second"},
                }},
            ]
            path.write_text("".join(json.dumps(event) + "\n" for event in events))
            progress = agent_delegate.progress_snapshot(path, "claude", True)
        self.assertEqual(progress["progress_tail"], "first second")
        self.assertEqual(progress["progress_format"], "claude-jsonl+stderr")

    def test_progress_snapshot_includes_plain_stderr_progress_without_polluting_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            stdout = directory / "stdout.log"
            stderr = directory / "stderr.log"
            stdout.write_text(json.dumps({
                "type": "stream_event",
                "event": {"delta": {"text": "model response"}},
            }) + "\n")
            stderr.write_text("Thinking…\nRunning tool: rg\n")
            progress = agent_delegate.progress_snapshot(stdout, "codex", True, stderr)
        self.assertIn("model response", progress["progress_tail"])
        self.assertIn("Running tool: rg", progress["progress_tail"])
        self.assertEqual(progress["progress_bytes"],
                         progress["stdout_progress_bytes"] + progress["stderr_progress_bytes"])
        self.assertGreater(progress["stderr_progress_bytes"], 0)

    def test_plain_stderr_progress_hides_command_and_source_dumps(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            stdout = directory / "stdout.log"
            stderr = directory / "stderr.log"
            stdout.write_text("")
            stderr.write_text(
                "I'll inspect the WebSocket boundary.\n\n"
                "exec\n/bin/zsh -lc 'rg WebSocket'\n succeeded in 3ms:\n"
                "    10\tfrom fastapi import WebSocket\n    11\tdef websocket_endpoint():\n\n"
                "I found the origin check and will verify its tests.\n\n"
                "Running tool: pytest\n"
            )
            progress = agent_delegate.progress_snapshot(stdout, "codex", True, stderr)
        self.assertIn("I'll inspect the WebSocket boundary.", progress["progress_tail"])
        self.assertIn("I found the origin check", progress["progress_tail"])
        self.assertIn("Running tool: pytest", progress["progress_tail"])
        self.assertIn("กำลังตรวจสอบ source", progress["progress_tail"])
        self.assertNotIn("from fastapi", progress["progress_tail"])
        self.assertNotIn("/bin/zsh", progress["progress_tail"])

    def test_progress_snapshot_surfaces_codex_error_events(self):
        """A Codex run that fails before any success-shaped event (no
        message delta, no agent_message/reasoning item) must still surface
        its diagnostic in progress_tail -- previously _event_texts only
        recognized success shapes, so a run like this rendered an empty
        tail even though the artifact held the real error (observed live:
        an invalid --model value rejected by the account produced this
        exact event sequence with an empty progress_tail)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.log"
            events = [
                {"type": "thread.started", "thread_id": "abc"},
                {"type": "item.completed", "item": {
                    "id": "item_0", "type": "error",
                    "message": "Model metadata for `luna` not found.",
                }},
                {"type": "turn.started"},
                {"type": "error", "message": "invalid_request_error: bad model"},
                {"type": "turn.failed",
                 "error": {"message": "invalid_request_error: bad model"}},
            ]
            path.write_text("".join(json.dumps(event) + "\n" for event in events))
            progress = agent_delegate.progress_snapshot(path, "codex", True)
        self.assertIn("Model metadata for `luna` not found.", progress["progress_tail"])
        self.assertIn("invalid_request_error: bad model", progress["progress_tail"])
        # The identical message from `error` and `turn.failed.error` must be
        # deduplicated rather than appearing twice.
        self.assertEqual(progress["progress_tail"].count("invalid_request_error"), 1)

    def test_progress_snapshot_appends_error_after_partial_success_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.log"
            events = [
                {"type": "stream_event", "event": {
                    "type": "content_block_delta", "delta": {"text": "partial answer"},
                }},
                {"type": "error", "message": "connection reset mid-stream"},
            ]
            path.write_text("".join(json.dumps(event) + "\n" for event in events))
            progress = agent_delegate.progress_snapshot(path, "claude", True)
        self.assertIn("partial answer", progress["progress_tail"])
        self.assertIn("connection reset mid-stream", progress["progress_tail"])


class ManagedRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.runs = Path(self.tmp)
        self.enterContext(mock.patch.object(agent_delegate, "RUNS_DIR", self.runs))
        # A parent agent process legitimately carries the recursion marker.
        # Direct runtime unit tests exercise a fresh top-level delegation and
        # must not accidentally inherit that ambient marker from the test shell.
        self.enterContext(mock.patch.dict(
            os.environ, {agent_delegate.DEPTH_ENV: "0"}, clear=False,
        ))

    def execute(self, args, child):
        run_id, directory = agent_delegate.new_run(agent_delegate._config_from_args(args))
        with mock.patch.object(agent_delegate, "find_binary", return_value="/bin/claude"), \
             mock.patch.object(agent_delegate, "_binary_version", return_value="2.test"), \
             mock.patch.object(agent_delegate, "run_child_to_files", child), \
             mock.patch.object(agent_delegate, "append_log"):
            return agent_delegate.execute_managed(args, run_id, directory), directory

    def test_structured_result_has_artifacts_attribution_and_checksums(self):
        args = delegate_args("--role", "reviewer", "--expect-regex", "OK",
                             "--min-output-chars", "2")
        result, directory = self.execute(args, managed_child(stdout="OK\n"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["role"], "reviewer")
        self.assertEqual(result["binary_version"], "2.test")
        self.assertEqual(len(result["stdout_sha256"]), 64)
        self.assertTrue(Path(result["stdout_path"]).is_file())
        self.assertEqual(agent_delegate._read_json(directory / "state.json")["status"],
                         "completed")

    def test_validation_failure_is_exit_four(self):
        args = delegate_args("--expect-json")
        result, _ = self.execute(args, managed_child(stdout="not json"))
        self.assertEqual(result["exit_code"], 4)
        self.assertEqual(result["error_kind"], "result_validation_failed")
        self.assertTrue(result["validation_errors"])

    def test_stderr_progress_does_not_participate_in_stdout_validation(self):
        args = delegate_args("--expect-json", "--stream-progress")
        result, _ = self.execute(
            args,
            managed_child(stdout='{"answer": "valid"}\n',
                          stderr="Codex: preparing final answer\n"),
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["validation_errors"], [])
        self.assertIn("preparing final answer", result["progress_tail"])

    def test_interrupted_child_is_terminal_cancelled(self):
        args = delegate_args()
        result, _ = self.execute(
            args, managed_child(stderr="cancelled by interrupt", code=130),
        )
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["error_kind"], "cancelled")

    def test_cancel_before_first_launch_does_not_cite_nonexistent_artifacts(self):
        args = delegate_args()
        run_id, directory = agent_delegate.new_run(agent_delegate._config_from_args(args))
        agent_delegate.update_run_state(directory, cancel_requested=True,
                                        status="cancel_pending")
        with mock.patch.object(agent_delegate, "find_binary", return_value="/bin/claude"), \
             mock.patch.object(agent_delegate, "_binary_version", return_value="2.test"), \
             mock.patch.object(agent_delegate, "run_child_to_files") as child, \
             mock.patch.object(agent_delegate, "append_log"):
            result = agent_delegate.execute_managed(args, run_id, directory)
        child.assert_not_called()
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(result["stdout_path"])
        self.assertIsNone(result["stderr_path"])

    def test_invalid_expectation_regex_is_validation_failure(self):
        args = delegate_args("--expect-regex", "[")
        result, _ = self.execute(args, managed_child(stdout="anything"))
        self.assertEqual(result["exit_code"], 4)
        self.assertIn("invalid expectation regex", result["validation_errors"][0])

    def test_transient_error_retries_then_succeeds(self):
        calls = []

        def child(
            cmd, cwd, env, timeout, stdout_path, stderr_path,
            on_started=None, should_cancel=None,
        ):
            calls.append(cmd)
            if on_started:
                on_started(4242)
            if len(calls) == 1:
                stdout_path.write_text("")
                stderr_path.write_text("service unavailable")
                return 1, 4242
            stdout_path.write_text("OK")
            stderr_path.write_text("")
            return 0, 4242

        args = delegate_args("--retries", "1", "--retry-delay", "0")
        result, _ = self.execute(args, child)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], 2)

    def test_cancellation_prevents_transient_retry(self):
        calls = []
        args = delegate_args("--retries", "2", "--retry-delay", "0")
        run_id, directory = agent_delegate.new_run(agent_delegate._config_from_args(args))

        def child(
            cmd, cwd, env, timeout, stdout_path, stderr_path,
            on_started=None, should_cancel=None,
        ):
            calls.append(cmd)
            if on_started:
                on_started(4242)
            stdout_path.write_text("")
            stderr_path.write_text("service unavailable")
            agent_delegate.update_run_state(directory, cancel_requested=True)
            return 1, 4242

        with mock.patch.object(agent_delegate, "find_binary", return_value="/bin/claude"), \
             mock.patch.object(agent_delegate, "_binary_version", return_value="2.test"), \
             mock.patch.object(agent_delegate, "run_child_to_files", child), \
             mock.patch.object(agent_delegate, "append_log"):
            result = agent_delegate.execute_managed(args, run_id, directory)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(len(calls), 1)

    def test_cancellation_during_retry_backoff_is_terminal_cancelled(self):
        args = delegate_args("--retries", "1", "--retry-delay", "1")
        with mock.patch.object(agent_delegate, "wait_retry_or_cancel", return_value=False):
            result, _ = self.execute(
                args, managed_child(stderr="service unavailable", code=1),
            )
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["exit_code"], 130)
        self.assertEqual(result["error_kind"], "cancelled")

    def test_failed_manager_owned_cancel_is_not_overwritten_to_cancelled(self):
        args = delegate_args()
        run_id, directory = agent_delegate.new_run(agent_delegate._config_from_args(args))

        def child(
            cmd, cwd, env, timeout, stdout_path, stderr_path,
            on_started=None, should_cancel=None,
        ):
            if on_started:
                on_started(4242)
            agent_delegate.update_run_state(directory, cancel_requested=True)
            stdout_path.write_text("")
            stderr_path.write_text("force-cancel permission denied")
            return agent_delegate.EXIT_REAP_FAILED, 4242

        with mock.patch.object(agent_delegate, "find_binary", return_value="/bin/claude"), \
             mock.patch.object(agent_delegate, "_binary_version", return_value="2.test"), \
             mock.patch.object(agent_delegate, "run_child_to_files", child), \
             mock.patch.object(agent_delegate, "append_log"):
            result = agent_delegate.execute_managed(args, run_id, directory)
        self.assertEqual(result["status"], "orphaned")
        self.assertEqual(result["exit_code"], agent_delegate.EXIT_REAP_FAILED)
        self.assertEqual(result["error_kind"], "reap_failed")

    def test_callback_publication_failure_preserves_recovery_identity(self):
        args = delegate_args()
        real_update = agent_delegate.update_run_state

        def flaky_update(directory, **updates):
            if updates.get("status") == "running" and "pid" in updates:
                raise OSError("state publication failed")
            return real_update(directory, **updates)

        def child(
            cmd, cwd, env, timeout, stdout_path, stderr_path,
            on_started=None, should_cancel=None,
        ):
            try:
                on_started(4242)
            except OSError:
                pass
            stdout_path.write_text("")
            stderr_path.write_text("reap unconfirmed")
            return agent_delegate.EXIT_REAP_FAILED, 4242

        with mock.patch.object(agent_delegate, "update_run_state", side_effect=flaky_update), \
             mock.patch.object(agent_delegate, "_process_start_token", return_value="birth:1"):
            result, directory = self.execute(args, child)
        state = agent_delegate._read_json(directory / "state.json")
        self.assertEqual(result["status"], "orphaned")
        self.assertEqual(state["pid"], 4242)
        self.assertEqual(state["pgid"], 4242)
        self.assertEqual(state["process_start_token"], "birth:1")

    def test_codex_claude_login_failure_is_classified_as_sandbox_boundary(self):
        args = delegate_args("--caller", "codex")
        result, _ = self.execute(
            args, managed_child(stderr="Not logged in · Please run /login", code=1),
        )
        self.assertEqual(result["error_kind"], "sandbox_credential_unavailable")
        self.assertIn("outside", result["next_action"])

    def test_advisor_role_rejects_mutating_claude_tools(self):
        args = delegate_args("--role", "advisor", "--allowed-tools", "Read", "Edit")
        run_id, directory = agent_delegate.new_run(agent_delegate._config_from_args(args))
        with mock.patch.object(agent_delegate, "append_log"):
            result = agent_delegate.execute_managed(args, run_id, directory)
        self.assertEqual(result["exit_code"], 4)
        self.assertEqual(result["error_kind"], "role_policy_violation")
        self.assertIn("Edit", result["message"])

    def test_reviewer_role_rejects_mutating_available_claude_tools(self):
        args = delegate_args("--role", "reviewer", "--available-tools", "Read", "Write")
        run_id, directory = agent_delegate.new_run(agent_delegate._config_from_args(args))
        with mock.patch.object(agent_delegate, "append_log"):
            result = agent_delegate.execute_managed(args, run_id, directory)
        self.assertEqual(result["exit_code"], 4)
        self.assertEqual(result["error_kind"], "role_policy_violation")
        self.assertIn("Write", result["message"])

    def test_read_state_reports_log_activity(self):
        args = delegate_args()
        result, _ = self.execute(args, managed_child(stdout="OK"))
        state = agent_delegate.read_run_state(result["run_id"])
        self.assertIn("last_activity_epoch", state)

    def test_state_updates_take_exclusive_file_lock(self):
        args = delegate_args()
        _, directory = agent_delegate.new_run(agent_delegate._config_from_args(args))
        with mock.patch.object(agent_delegate.fcntl, "flock") as flock:
            agent_delegate.update_run_state(directory, phase="test")
        self.assertEqual(
            [call.args[1] for call in flock.call_args_list],
            [agent_delegate.fcntl.LOCK_EX, agent_delegate.fcntl.LOCK_UN],
        )

    def test_large_output_reader_seeks_bounded_tail(self):
        output = self.runs / "large.log"
        output.write_bytes(b"A" * 1024 + b"TAIL")
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("unbounded read")):
            text, truncated = agent_delegate._read_output(output, 16)
        self.assertTrue(truncated)
        self.assertEqual(text, "A" * 12 + "TAIL")

    def test_missing_artifact_checksum_is_none(self):
        self.assertIsNone(agent_delegate._sha256(self.runs / "missing.log"))


class BackgroundControlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.object(agent_delegate, "RUNS_DIR", Path(self.tmp)))

    def test_background_returns_trackable_run(self):
        args = delegate_args("--background")
        fake_proc = mock.Mock(pid=4321)
        stdout = __import__("io").StringIO()
        with mock.patch.object(agent_delegate.subprocess, "Popen",
                               return_value=fake_proc) as popen, \
             mock.patch.object(sys, "stdout", stdout):
            code = agent_delegate.start_background(args)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["next_action"], agent_delegate.BACKGROUND_NEXT_ACTION)
        state = agent_delegate.read_run_state(payload["run_id"])
        self.assertEqual(state["launcher_pid"], 4321)
        self.assertEqual(state["reasoning_effort"], "medium")
        self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_linux_birth_identity_fails_closed_without_procfs(self):
        with mock.patch.object(agent_delegate.sys, "platform", "linux"), \
             mock.patch.object(agent_delegate.Path, "exists", return_value=False), \
             mock.patch.object(agent_delegate.subprocess, "run") as run:
            token = agent_delegate._process_start_token(4242)
        self.assertIsNone(token)
        run.assert_not_called()

    def test_linux_group_skips_unreadable_unrelated_member(self):
        """A PermissionError reading one entry's /proc/<pid>/stat (e.g.
        hidepid, or a foreign-uid process) must not fail the whole
        enumeration closed: our own descendants are always same-uid and
        readable, so a pid we can't read cannot be one of them."""
        class UnreadableStat:
            def read_text(self):
                raise PermissionError("denied")

        class Entry:
            name = "99"

            def __truediv__(self, _name):
                return UnreadableStat()

        class ProcRoot:
            def is_dir(self):
                return True

            def iterdir(self):
                return [Entry()]

        with mock.patch.object(agent_delegate.sys, "platform", "linux"), \
             mock.patch.object(agent_delegate, "Path", return_value=ProcRoot()):
            occupied = agent_delegate._group_has_other_members(42, 1)
        self.assertFalse(occupied)

    def test_linux_group_enumeration_fails_closed_on_malformed_stat(self):
        class MalformedStat:
            def read_text(self):
                return "not a valid stat line"  # no ')' to split on

        class Entry:
            name = "99"

            def __truediv__(self, _name):
                return MalformedStat()

        class ProcRoot:
            def is_dir(self):
                return True

            def iterdir(self):
                return [Entry()]

        with mock.patch.object(agent_delegate.sys, "platform", "linux"), \
             mock.patch.object(agent_delegate, "Path", return_value=ProcRoot()):
            occupied = agent_delegate._group_has_other_members(42, 1)
        self.assertIsNone(occupied)

    def test_darwin_group_enumeration_fails_closed_on_full_pid_buffer(self):
        class ProcListPids:
            def __init__(self):
                self.calls = 0

            def __call__(self, _kind, _info, _buffer, size):
                self.calls += 1
                return 4 if self.calls == 1 else size

        proc_list = ProcListPids()
        library = mock.Mock(proc_listpids=proc_list)
        with mock.patch.object(agent_delegate.sys, "platform", "darwin"), \
             mock.patch.object(agent_delegate.ctypes, "CDLL", return_value=library):
            occupied = agent_delegate._group_has_other_members(42, 1)
        self.assertIsNone(occupied)

    def test_darwin_group_query_is_scoped_by_pgrp_not_all_pids(self):
        """proc_listpids must be called with PROC_PGRP_ONLY (kind 2) and the
        pgid as typeinfo, not PROC_ALL_PIDS -- listing every system pid and
        probing each one's pgid via proc_pidinfo previously failed closed
        (returned None) as soon as ANY unrelated, ambient process on the
        machine could be signalled but not introspected, which is the norm
        on a real desktop and was observed to make this helper permanently
        return None regardless of the guarded run's own group state."""
        def make_proc_list_pids():
            class ProcListPids:
                def __init__(self):
                    self.calls = 0

                def __call__(self, kind, info, buffer, _size):
                    self.calls += 1
                    assert kind == 2
                    assert info == 42
                    if self.calls == 1:
                        return 8
                    buffer[0] = 1  # own_pid must be excluded from the result
                    buffer[1] = 77  # a genuine other member of the group
                    return 8
            return ProcListPids()

        with mock.patch.object(agent_delegate.sys, "platform", "darwin"), \
             mock.patch.object(agent_delegate.ctypes, "CDLL",
                               return_value=mock.Mock(proc_listpids=make_proc_list_pids())):
            pids = agent_delegate._group_member_pids(42, 1)
        with mock.patch.object(agent_delegate.sys, "platform", "darwin"), \
             mock.patch.object(agent_delegate.ctypes, "CDLL",
                               return_value=mock.Mock(proc_listpids=make_proc_list_pids())):
            occupied = agent_delegate._group_has_other_members(42, 1)
        self.assertEqual(pids, [77])
        self.assertTrue(occupied)

    def test_darwin_group_reports_no_occupancy_when_only_self_present(self):
        class ProcListPids:
            def __init__(self):
                self.calls = 0

            def __call__(self, _kind, _info, buffer, _size):
                self.calls += 1
                if self.calls == 1:
                    return 4
                buffer[0] = 1  # own_pid only: group is otherwise empty
                return 4

        library = mock.Mock(proc_listpids=ProcListPids())
        with mock.patch.object(agent_delegate.sys, "platform", "darwin"), \
             mock.patch.object(agent_delegate.ctypes, "CDLL", return_value=library):
            occupied = agent_delegate._group_has_other_members(42, 1)
        self.assertFalse(occupied)

    def test_background_accepts_preallocated_run_id(self):
        run_id = "fixedrun1234"
        args = delegate_args("--background", "--run-id", run_id)
        stdout = __import__("io").StringIO()
        with mock.patch.object(agent_delegate.subprocess, "Popen", return_value=mock.Mock(pid=4321)), \
             mock.patch.object(sys, "stdout", stdout):
            code = agent_delegate.start_background(args)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["run_id"], run_id)
        self.assertTrue((Path(self.tmp) / run_id / "request.json").exists())

    def test_fast_background_worker_completion_is_not_overwritten_to_queued(self):
        args = delegate_args("--background")
        fake_proc = mock.Mock(pid=4321)

        def finish_before_parent_metadata(*_args, **_kwargs):
            directory = next(Path(self.tmp).iterdir())
            agent_delegate.update_run_state(directory, status="completed", exit_code=0)
            return fake_proc

        stdout = __import__("io").StringIO()
        with mock.patch.object(agent_delegate.subprocess, "Popen",
                               side_effect=finish_before_parent_metadata), \
             mock.patch.object(sys, "stdout", stdout):
            agent_delegate.start_background(args)
        directory = next(Path(self.tmp).iterdir())
        self.assertEqual(agent_delegate._read_json(directory / "state.json")["status"],
                         "completed")
        self.assertEqual(json.loads(stdout.getvalue())["status"], "completed")

    def test_status_uses_live_manager_instead_of_dead_child(self):
        run_id, directory = agent_delegate.new_run({
            "caller": "codex", "target": "claude", "model": "sonnet", "role": "worker",
        })
        agent_delegate.update_run_state(
            directory, status="running", manager_pid=111, pid=222,
        )
        stdout = __import__("io").StringIO()
        with mock.patch.object(agent_delegate, "_process_exists",
                               side_effect=lambda pid: pid == 111), \
             mock.patch.object(sys, "stdout", stdout):
            agent_delegate.status_main(run_id, as_json=True)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "running")

    def test_status_returns_incremental_progress_while_running(self):
        run_id, directory = agent_delegate.new_run({
            "caller": "codex", "target": "claude", "model": "haiku",
            "role": "reviewer", "stream_progress": True,
        })
        output = directory / "stdout.attempt-1.log"
        output.write_text(json.dumps({
            "type": "stream_event",
            "event": {"delta": {"text": "partial finding"}},
        }) + "\n")
        agent_delegate.update_run_state(
            directory, status="running", manager_pid=111, stdout_path=str(output),
        )
        stdout = __import__("io").StringIO()
        with mock.patch.object(agent_delegate, "_process_exists", return_value=True), \
             mock.patch.object(sys, "stdout", stdout):
            agent_delegate.status_main(run_id, as_json=True)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["progress_tail"], "partial finding")
        self.assertGreater(result["progress_bytes"], 0)

    def test_status_includes_stderr_progress_while_running(self):
        run_id, directory = agent_delegate.new_run({
            "caller": "codex", "target": "codex", "model": "test",
            "role": "worker", "stream_progress": True,
        })
        output = directory / "stdout.attempt-1.log"
        errors = directory / "stderr.attempt-1.log"
        output.write_text("")
        errors.write_text("Codex: analysing repository\n")
        agent_delegate.update_run_state(
            directory, status="running", manager_pid=111,
            stdout_path=str(output), stderr_path=str(errors),
        )
        captured = __import__("io").StringIO()
        with mock.patch.object(agent_delegate, "_process_exists", return_value=True), \
             mock.patch.object(sys, "stdout", captured):
            agent_delegate.status_main(run_id, as_json=True)
        result = json.loads(captured.getvalue())
        self.assertEqual(result["status"], "running")
        self.assertIn("analysing repository", result["progress_tail"])
        self.assertGreater(result["stderr_progress_bytes"], 0)

    def test_wait_timeout_returns_live_structured_state_and_stderr_progress(self):
        run_id, directory = agent_delegate.new_run({
            "caller": "codex", "target": "codex", "model": "test",
            "role": "worker", "stream_progress": True,
        })
        output = directory / "stdout.attempt-1.log"
        errors = directory / "stderr.attempt-1.log"
        output.write_text("")
        errors.write_text("Codex: still working\n")
        agent_delegate.update_run_state(
            directory, status="running", manager_pid=111,
            stdout_path=str(output), stderr_path=str(errors),
        )
        captured = __import__("io").StringIO()
        with mock.patch.object(agent_delegate, "_process_exists", return_value=True), \
             mock.patch.object(sys, "stdout", captured):
            code = agent_delegate.wait_main(run_id, timeout=0, as_json=True)
        result = json.loads(captured.getvalue())
        self.assertEqual(code, 124)
        self.assertEqual(result["status"], "running")
        self.assertTrue(result["wait_timed_out"])
        self.assertIn("still working", result["progress_tail"])

    def test_status_repairs_stale_ownerless_launch(self):
        run_id, directory = agent_delegate.new_run({
            "caller": "codex", "target": "claude", "model": "haiku", "role": "worker",
        })
        old = "2000-01-01T00:00:00+00:00"
        agent_delegate.update_run_state(directory, status="queued", created_at=old)
        stdout = __import__("io").StringIO()
        with mock.patch.object(sys, "stdout", stdout):
            agent_delegate.status_main(run_id, as_json=True)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_kind"], "worker_died")

    def test_cancel_is_manager_owned_and_never_signals_persisted_pgid(self):
        run_id, directory = agent_delegate.new_run({
            "caller": "codex", "target": "claude", "model": "sonnet", "role": "worker",
        })
        agent_delegate.update_run_state(directory, status="running", manager_pid=111, pgid=4242)

        def manager_finishes(_seconds):
            agent_delegate.update_run_state(directory, status="cancelled", exit_code=130)

        with mock.patch.object(agent_delegate.time, "sleep", side_effect=manager_finishes), \
             mock.patch.object(agent_delegate, "_process_exists", return_value=True), \
             mock.patch.object(agent_delegate.os, "killpg") as killpg, \
             mock.patch.object(sys, "stdout", __import__("io").StringIO()):
            code = agent_delegate.cancel_main(run_id)
        self.assertEqual(code, 0)
        killpg.assert_not_called()
        self.assertEqual(agent_delegate.read_run_state(run_id)["status"], "cancelled")

    def test_cancel_reports_failed_when_manager_is_gone(self):
        run_id, directory = agent_delegate.new_run({
            "caller": "codex", "target": "claude", "model": "sonnet", "role": "worker",
        })
        agent_delegate.update_run_state(directory, status="running", manager_pid=111, pgid=4242)
        with mock.patch.object(agent_delegate, "_process_exists", return_value=False), \
             mock.patch.object(agent_delegate.os, "killpg") as killpg, \
             mock.patch.object(sys, "stdout", __import__("io").StringIO()):
            code = agent_delegate.cancel_main(run_id)
        state = agent_delegate.read_run_state(run_id)
        self.assertEqual(code, 1)
        self.assertEqual(state["error_kind"], "cancel_manager_unavailable")
        killpg.assert_not_called()

    def test_cancel_does_not_overwrite_run_that_finishes_during_request(self):
        run_id, directory = agent_delegate.new_run({
            "caller": "codex", "target": "claude", "model": "sonnet", "role": "worker",
        })
        agent_delegate.update_run_state(directory, status="running")

        def finish(_seconds):
            agent_delegate.update_run_state(directory, status="completed", exit_code=0)

        with mock.patch.object(agent_delegate.time, "sleep", side_effect=finish), \
             mock.patch.object(agent_delegate.os, "killpg") as killpg, \
             mock.patch.object(sys, "stdout", __import__("io").StringIO()):
            code = agent_delegate.cancel_main(run_id)
        self.assertEqual(code, 0)
        killpg.assert_not_called()
        self.assertEqual(agent_delegate.read_run_state(run_id)["status"], "completed")

    def test_cancel_atomic_transition_cannot_overwrite_concurrent_completion(self):
        run_id, directory = agent_delegate.new_run({
            "caller": "codex", "target": "claude", "model": "sonnet", "role": "worker",
        })
        agent_delegate.update_run_state(directory, status="running", manager_pid=111)
        original = agent_delegate.update_run_state_if

        def complete_before_cas(target, predicate, **changes):
            agent_delegate.update_run_state(directory, status="completed", exit_code=0)
            return original(target, predicate, **changes)

        stdout = __import__("io").StringIO()
        with mock.patch.object(agent_delegate, "update_run_state_if",
                               side_effect=complete_before_cas), \
             mock.patch.object(sys, "stdout", stdout):
            code = agent_delegate.cancel_main(run_id)
        self.assertEqual(code, 0)
        self.assertEqual(agent_delegate.read_run_state(run_id)["status"], "completed")

    def test_status_repair_cannot_overwrite_concurrent_completion(self):
        run_id, directory = agent_delegate.new_run({
            "caller": "codex", "target": "claude", "model": "sonnet", "role": "worker",
        })
        agent_delegate.update_run_state(directory, status="running", manager_pid=111)
        original = agent_delegate.update_run_state_if

        def complete_before_cas(target, predicate, **changes):
            agent_delegate.update_run_state(directory, status="completed", exit_code=0)
            return original(target, predicate, **changes)

        stdout = __import__("io").StringIO()
        with mock.patch.object(agent_delegate, "_process_exists", return_value=False), \
             mock.patch.object(agent_delegate, "update_run_state_if",
                               side_effect=complete_before_cas), \
             mock.patch.object(sys, "stdout", stdout):
            agent_delegate.status_main(run_id, as_json=True)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "completed")

    def test_cancel_stays_pending_when_manager_has_not_confirmed_exit(self):
        run_id, directory = agent_delegate.new_run({
            "caller": "codex", "target": "claude", "model": "sonnet", "role": "worker",
        })
        agent_delegate.update_run_state(directory, status="running", manager_pid=111, pgid=4242)
        with mock.patch.object(agent_delegate, "CANCEL_GRACE_S", 0), \
             mock.patch.object(agent_delegate, "POST_KILL_DRAIN_S", 0), \
             mock.patch.object(agent_delegate.os, "killpg") as killpg, \
             mock.patch.object(sys, "stdout", __import__("io").StringIO()):
            code = agent_delegate.cancel_main(run_id)
        self.assertEqual(code, 0)
        killpg.assert_not_called()
        self.assertEqual(agent_delegate.read_run_state(run_id)["status"], "cancel_pending")

    def test_advisor_group_cancel_is_forwarded_to_active_child(self):
        group_id, group_dir = agent_delegate.new_run({
            "kind": "advisor_group", "caller": "codex", "target": "claude",
            "model": "sonnet->opus", "role": "orchestrator",
        })
        child_id, child_dir = agent_delegate.new_run({
            "caller": "codex", "target": "claude", "model": "sonnet", "role": "worker",
        })
        agent_delegate.update_run_state(child_dir, status="running", manager_pid=222, pgid=4242)
        agent_delegate.update_run_state(
            group_dir, status="running", worker_run_id=child_id, manager_pid=111,
        )
        with mock.patch.object(agent_delegate, "CANCEL_GRACE_S", 0), \
             mock.patch.object(agent_delegate, "POST_KILL_DRAIN_S", 0), \
             mock.patch.object(agent_delegate.os, "killpg") as killpg, \
             mock.patch.object(sys, "stdout", __import__("io").StringIO()):
            code = agent_delegate.cancel_main(group_id)
        self.assertEqual(code, 0)
        killpg.assert_not_called()
        self.assertEqual(agent_delegate.read_run_state(child_id)["status"], "cancel_pending")
        self.assertEqual(agent_delegate.read_run_state(group_id)["status"], "cancel_pending")

    def test_cancel_without_published_child_is_pending_not_false_terminal(self):
        run_id, directory = agent_delegate.new_run({
            "caller": "codex", "target": "claude", "model": "sonnet", "role": "worker",
        })
        agent_delegate.update_run_state(directory, status="queued")
        with mock.patch.object(agent_delegate, "CANCEL_GRACE_S", 0), \
             mock.patch.object(agent_delegate, "POST_KILL_DRAIN_S", 0), \
             mock.patch.object(agent_delegate.os, "killpg") as killpg, \
             mock.patch.object(sys, "stdout", __import__("io").StringIO()):
            code = agent_delegate.cancel_main(run_id)
        self.assertEqual(code, 0)
        killpg.assert_not_called()
        self.assertEqual(agent_delegate.read_run_state(run_id)["status"], "cancel_pending")

    def test_diagnose_rejects_nonexistent_explicit_binary(self):
        stdout = __import__("io").StringIO()
        with mock.patch.object(sys, "stdout", stdout):
            code = agent_delegate.diagnose_main([
                "claude", "--binary", "/definitely/missing/claude",
            ])
        self.assertEqual(code, 3)
        self.assertFalse(json.loads(stdout.getvalue())["binary_executable"])


class ManagedLifecycleIntegrationTest(unittest.TestCase):
    def test_real_background_start_progress_status_and_cancel(self):
        script = Path(agent_delegate.__file__).resolve()
        fixture = script.parent / "developer" / "fake_agent_cli.py"
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["ENDMEMEX_AGENT_RUNS_DIR"] = tmp
            env.pop(agent_delegate.DEPTH_ENV, None)
            started = subprocess.run([
                sys.executable, str(script), "claude", "integration fixture",
                "--binary", str(fixture), "--background", "--isolated",
                "--stream-progress", "--timeout", "30", "--allowed-tools", "Read",
            ], text=True, capture_output=True, check=True, timeout=5, env=env)
            run_id = json.loads(started.stdout)["run_id"]
            try:
                observed = None
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    status = subprocess.run([
                        sys.executable, str(script), "status", run_id, "--json",
                    ], text=True, capture_output=True, check=True, timeout=5, env=env)
                    observed = json.loads(status.stdout)
                    if observed.get("progress_tail") == "fixture progress":
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(observed)
                self.assertEqual(observed["status"], "running")
                self.assertEqual(observed["progress_tail"], "fixture progress")
                self.assertTrue(observed.get("process_start_token"))
                self.assertTrue(agent_delegate._process_matches_run(
                    observed["pid"], observed["process_start_token"],
                ))

                cancelled = subprocess.run([
                    sys.executable, str(script), "cancel", run_id,
                ], text=True, capture_output=True, check=True, timeout=10, env=env)
                self.assertEqual(json.loads(cancelled.stdout)["status"], "cancelled")
                final = subprocess.run([
                    sys.executable, str(script), "status", run_id, "--json",
                ], text=True, capture_output=True, check=True, timeout=5, env=env)
                self.assertEqual(json.loads(final.stdout)["status"], "cancelled")
            finally:
                subprocess.run([
                    sys.executable, str(script), "cancel", run_id,
                ], text=True, capture_output=True, timeout=5, env=env)

    def test_status_reaps_verified_child_after_manager_crash(self):
        script = Path(agent_delegate.__file__).resolve()
        fixture = script.parent / "developer" / "fake_agent_cli.py"
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["ENDMEMEX_AGENT_RUNS_DIR"] = tmp
            env.pop(agent_delegate.DEPTH_ENV, None)
            started = subprocess.run([
                sys.executable, str(script), "claude", "orphan fixture",
                "--binary", str(fixture), "--background", "--isolated",
                "--stream-progress", "--timeout", "30", "--allowed-tools", "Read",
            ], text=True, capture_output=True, check=True, timeout=5, env=env)
            run_id = json.loads(started.stdout)["run_id"]
            state = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                status = subprocess.run([
                    sys.executable, str(script), "status", run_id, "--json",
                ], text=True, capture_output=True, check=True, timeout=5, env=env)
                state = json.loads(status.stdout)
                if state.get("pid"):
                    break
                time.sleep(0.05)
            self.assertIsNotNone(state.get("pid"))
            manager_pid, child_pid = state["manager_pid"], state["pid"]
            os.kill(manager_pid, signal.SIGKILL)
            time.sleep(0.1)
            recovered = subprocess.run([
                sys.executable, str(script), "status", run_id, "--json",
            ], text=True, capture_output=True, check=True, timeout=10, env=env)
            result = json.loads(recovered.stdout)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error_kind"], "worker_died")
            self.assertFalse(agent_delegate._process_exists(child_pid))

    def test_guard_survives_target_exit_and_reaps_descendant_after_manager_crash(self):
        script = Path(agent_delegate.__file__).resolve()
        fixture = script.parent / "developer" / "fake_agent_cli.py"
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["ENDMEMEX_AGENT_RUNS_DIR"] = tmp
            env["FAKE_AGENT_MODE"] = "spawn-descendant-exit"
            env.pop(agent_delegate.DEPTH_ENV, None)
            started = subprocess.run([
                sys.executable, str(script), "claude", "descendant fixture",
                "--binary", str(fixture), "--background", "--isolated",
                "--stream-progress", "--timeout", "30", "--allowed-tools", "Read",
            ], text=True, capture_output=True, check=True, timeout=5, env=env)
            run_id = json.loads(started.stdout)["run_id"]
            descendant_pid = None
            state = None
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    status = subprocess.run([
                        sys.executable, str(script), "status", run_id, "--json",
                    ], text=True, capture_output=True, check=True, timeout=5, env=env)
                    state = json.loads(status.stdout)
                    match = re.search(r"descendant:(\d+)", state.get("progress_tail", ""))
                    if match and state.get("pid"):
                        descendant_pid = int(match.group(1))
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(descendant_pid)
                self.assertEqual(state["status"], "running")
                self.assertTrue(agent_delegate._process_exists(state["pid"]))
                self.assertTrue(agent_delegate._process_exists(descendant_pid))

                os.kill(state["manager_pid"], signal.SIGKILL)
                time.sleep(0.1)
                recovered = subprocess.run([
                    sys.executable, str(script), "status", run_id, "--json",
                ], text=True, capture_output=True, check=True, timeout=10, env=env)
                result = json.loads(recovered.stdout)
                self.assertEqual(result["status"], "failed")
                deadline = time.monotonic() + 5
                while agent_delegate._process_exists(descendant_pid) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(agent_delegate._process_exists(descendant_pid))
            finally:
                if descendant_pid and agent_delegate._process_exists(descendant_pid):
                    try:
                        os.kill(descendant_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_guard_completes_run_on_its_own_when_descendant_outlives_target(self):
        """A target that finishes and exits, leaving one lingering same-group
        descendant, must reach "completed" with the target's real exit_code on
        its own -- bounded by GUARD_DRAIN_GRACE_S -- rather than only via a
        manager crash, and never by silently burning the full --timeout and
        being misreported as timed_out (the live bug this guards against)."""
        script = Path(agent_delegate.__file__).resolve()
        fixture = script.parent / "developer" / "fake_agent_cli.py"
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["ENDMEMEX_AGENT_RUNS_DIR"] = tmp
            env["FAKE_AGENT_MODE"] = "spawn-descendant-exit"
            env.pop(agent_delegate.DEPTH_ENV, None)
            started = subprocess.run([
                sys.executable, str(script), "claude", "descendant fixture",
                "--binary", str(fixture), "--background", "--isolated",
                "--stream-progress", "--timeout", "30", "--allowed-tools", "Read",
            ], text=True, capture_output=True, check=True, timeout=5, env=env)
            run_id = json.loads(started.stdout)["run_id"]
            descendant_pid = None
            state = None
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    status = subprocess.run([
                        sys.executable, str(script), "status", run_id, "--json",
                    ], text=True, capture_output=True, check=True, timeout=5, env=env)
                    state = json.loads(status.stdout)
                    match = re.search(r"descendant:(\d+)", state.get("progress_tail", ""))
                    if match and state.get("pid"):
                        descendant_pid = int(match.group(1))
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(descendant_pid)
                self.assertTrue(agent_delegate._process_exists(descendant_pid))

                # No manager crash here: the guard itself must resolve the run
                # within GUARD_DRAIN_GRACE_S, far short of the 30s --timeout.
                deadline = time.monotonic() + agent_delegate.GUARD_DRAIN_GRACE_S + 5
                result = None
                while time.monotonic() < deadline:
                    status = subprocess.run([
                        sys.executable, str(script), "status", run_id, "--json",
                    ], text=True, capture_output=True, check=True, timeout=5, env=env)
                    result = json.loads(status.stdout)
                    if result.get("status") in agent_delegate.TERMINAL_STATUSES:
                        break
                    time.sleep(0.1)
                self.assertEqual(result.get("status"), "completed")
                self.assertEqual(result.get("exit_code"), 0)
                deadline = time.monotonic() + 5
                while agent_delegate._process_exists(descendant_pid) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(agent_delegate._process_exists(descendant_pid))
            finally:
                if descendant_pid and agent_delegate._process_exists(descendant_pid):
                    try:
                        os.kill(descendant_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_guard_translates_signal_death_to_negative_returncode(self):
        """A target killed by a signal must propagate as the standard Python
        subprocess convention (a negative signal number), not get silently
        collapsed to a positive byte value by SystemExit's POSIX exit-status
        masking (observed live: `raise SystemExit(-9)` becomes exit code 247,
        losing the fact the target died from SIGKILL)."""
        script = Path(agent_delegate.__file__).resolve()
        fixture = script.parent / "developer" / "fake_agent_cli.py"
        env = os.environ.copy()
        env["FAKE_AGENT_MODE"] = "self-sigterm"
        result = subprocess.run(
            [sys.executable, str(script), "_guard", "--", sys.executable, str(fixture)],
            env=env, timeout=10,
        )
        self.assertEqual(result.returncode, -signal.SIGTERM)

    def test_cancel_force_kills_stubborn_descendant_with_inherited_pipes(self):
        script = Path(agent_delegate.__file__).resolve()
        fixture = script.parent / "developer" / "fake_agent_cli.py"
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            env["ENDMEMEX_AGENT_RUNS_DIR"] = tmp
            env["FAKE_AGENT_MODE"] = "spawn-stubborn-descendant-exit"
            env.pop(agent_delegate.DEPTH_ENV, None)
            started = subprocess.run([
                sys.executable, str(script), "claude", "stubborn fixture",
                "--binary", str(fixture), "--background", "--isolated",
                "--stream-progress", "--timeout", "30", "--allowed-tools", "Read",
            ], text=True, capture_output=True, check=True, timeout=5, env=env)
            run_id = json.loads(started.stdout)["run_id"]
            descendant_pid = None
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    status = subprocess.run([
                        sys.executable, str(script), "status", run_id, "--json",
                    ], text=True, capture_output=True, check=True, timeout=5, env=env)
                    state = json.loads(status.stdout)
                    match = re.search(r"descendant:(\d+)", state.get("progress_tail", ""))
                    if match:
                        descendant_pid = int(match.group(1))
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(descendant_pid)
                cancelled = subprocess.run([
                    sys.executable, str(script), "cancel", run_id,
                ], text=True, capture_output=True, check=True, timeout=10, env=env)
                self.assertEqual(json.loads(cancelled.stdout)["status"], "cancelled")
                self.assertFalse(agent_delegate._process_exists(descendant_pid))
            finally:
                if descendant_pid and agent_delegate._process_exists(descendant_pid):
                    try:
                        os.kill(descendant_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


class AdvisorWorkflowTest(unittest.TestCase):
    def test_sonnet_worker_then_opus_advisor(self):
        with tempfile.TemporaryDirectory() as tmp:
            roles = []

            def fake_execute(args, run_id, directory):
                roles.append((args.role, args.model, args.prompt))
                stdout_path = directory / "stdout.attempt-1.log"
                stderr_path = directory / "stderr.attempt-1.log"
                stdout_path.write_text("candidate" if args.role == "worker" else "accept")
                stderr_path.write_text("")
                return {"run_id": run_id, "status": "completed", "exit_code": 0,
                        "stdout_path": str(stdout_path), "stderr_path": str(stderr_path)}

            with mock.patch.object(agent_delegate, "RUNS_DIR", Path(tmp)), \
                 mock.patch.object(agent_delegate, "execute_managed", fake_execute), \
                 mock.patch.object(sys, "stdout", __import__("io").StringIO()):
                code = agent_delegate.advisor_main(["audit this", "--result-format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual([(role, model) for role, model, _ in roles],
                         [("worker", "sonnet"), ("advisor", "opus")])
        self.assertIn("candidate", roles[1][2])

    def test_worker_timeout_propagates_to_group_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            def fake_execute(args, run_id, directory):
                return {"run_id": run_id, "status": "timed_out", "exit_code": 124,
                        "error_kind": "timeout", "next_action": "reduce scope"}

            stdout = __import__("io").StringIO()
            with mock.patch.object(agent_delegate, "RUNS_DIR", Path(tmp)), \
                 mock.patch.object(agent_delegate, "execute_managed", fake_execute), \
                 mock.patch.object(sys, "stdout", stdout):
                code = agent_delegate.advisor_main(["audit this", "--result-format", "json"])
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 124)
        self.assertEqual(payload["status"], "timed_out")
        self.assertIsNone(payload["advisor"])

    def test_unsafe_advisor_tools_fail_before_worker_launch(self):
        execute = mock.Mock()
        with mock.patch.object(agent_delegate, "execute_managed", execute), \
             self.assertRaises(SystemExit):
            agent_delegate.advisor_main(["audit this", "--allowed-tools", "Read", "Edit"])
        execute.assert_not_called()

    def test_truncated_worker_output_is_disclosed_to_advisor_and_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompts = []

            def fake_execute(args, run_id, directory):
                prompts.append(args.prompt)
                stdout_path = directory / "stdout.attempt-1.log"
                stderr_path = directory / "stderr.attempt-1.log"
                stdout_path.write_text("A" * 50001 if args.role == "worker" else "accept")
                stderr_path.write_text("")
                return {"run_id": run_id, "status": "completed", "exit_code": 0,
                        "stdout_path": str(stdout_path), "stderr_path": str(stderr_path)}

            stdout = __import__("io").StringIO()
            with mock.patch.object(agent_delegate, "RUNS_DIR", Path(tmp)), \
                 mock.patch.object(agent_delegate, "execute_managed", fake_execute), \
                 mock.patch.object(sys, "stdout", stdout):
                code = agent_delegate.advisor_main(["audit this", "--result-format", "json"])
        self.assertEqual(code, 0)
        self.assertIn("[truncated:", prompts[1])
        self.assertTrue(json.loads(stdout.getvalue())["worker_output_truncated"])

    def test_raw_advisor_failure_preserves_unreviewed_worker_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            def fake_execute(args, run_id, directory):
                stdout_path = directory / "stdout.attempt-1.log"
                stderr_path = directory / "stderr.attempt-1.log"
                stdout_path.write_text("candidate finding" if args.role == "worker" else "")
                stderr_path.write_text("advisor timeout" if args.role == "advisor" else "")
                if args.role == "worker":
                    return {"run_id": run_id, "status": "completed", "exit_code": 0,
                            "stdout_path": str(stdout_path), "stderr_path": str(stderr_path)}
                return {"run_id": run_id, "status": "timed_out", "exit_code": 124,
                        "error_kind": "timeout", "next_action": "increase timeout",
                        "stdout_path": str(stdout_path), "stderr_path": str(stderr_path)}

            stdout = __import__("io").StringIO()
            stderr = __import__("io").StringIO()
            with mock.patch.object(agent_delegate, "RUNS_DIR", Path(tmp)), \
                 mock.patch.object(agent_delegate, "execute_managed", fake_execute), \
                 mock.patch.object(sys, "stdout", stdout), \
                 mock.patch.object(sys, "stderr", stderr):
                code = agent_delegate.advisor_main(["audit this"])
        self.assertEqual(code, 124)
        self.assertIn("candidate finding", stdout.getvalue())
        self.assertIn("unreviewed", stderr.getvalue())


class LoggingTest(unittest.TestCase):
    def test_log_entry_written_and_truncated(self):
        entries = []
        long_prompt = "x" * 1000

        with mock.patch.object(agent_delegate, "run_child_to_files",
                               managed_child(stdout="y" * 5000)), \
             mock.patch.object(agent_delegate, "find_binary", return_value="/bin/x"), \
             mock.patch.object(agent_delegate, "_binary_version", return_value="test"), \
             mock.patch.object(agent_delegate, "append_log", entries.append):
            code = run_main(["codex", long_prompt])
        self.assertEqual(code, 0)
        entry = entries[0]
        self.assertEqual(len(entry["prompt"]), agent_delegate.LOG_PROMPT_CHARS)
        self.assertEqual(len(entry["output"]), agent_delegate.LOG_OUTPUT_CHARS)
        json.dumps(entry)  # must be serializable

    def test_failure_entry_includes_stderr_tail(self):
        entries = []

        with mock.patch.object(agent_delegate, "run_child_to_files",
                               managed_child(stderr="boom " * 200, code=1)), \
             mock.patch.object(agent_delegate, "find_binary", return_value="/bin/x"), \
             mock.patch.object(agent_delegate, "_binary_version", return_value="test"), \
             mock.patch.object(agent_delegate, "append_log", entries.append):
            code = run_main(["codex", "hi"])
        self.assertEqual(code, 1)
        self.assertIn("stderr", entries[0])
        self.assertLessEqual(len(entries[0]["stderr"]), agent_delegate.LOG_STDERR_CHARS)

    def test_missing_binary_exit_code_and_logged(self):
        with mock.patch.object(agent_delegate, "find_binary", return_value=None), \
             mock.patch.object(agent_delegate, "append_log") as log:
            code = run_main(["codex", "hi"])
        self.assertEqual(code, 3)
        log.assert_called_once()
        self.assertEqual(log.call_args[0][0]["exit_code"], 3)

    def test_raw_early_failure_is_visible_on_stderr(self):
        stderr = __import__("io").StringIO()
        with mock.patch.object(agent_delegate, "find_binary", return_value=None), \
             mock.patch.object(agent_delegate, "append_log"), \
             mock.patch.object(sys, "stderr", stderr):
            code = run_main(["claude", "hi"])
        self.assertEqual(code, 3)
        self.assertIn("cli_not_found", stderr.getvalue())

    def test_log_write_failure_does_not_raise(self):
        with mock.patch.object(Path, "open", side_effect=OSError("read-only")), \
             mock.patch.object(sys, "stderr", __import__("io").StringIO()) as stderr:
            written = agent_delegate.append_log({"message": "x"})
        self.assertFalse(written)
        self.assertIn("could not append", stderr.getvalue())


class CheckpointTest(unittest.TestCase):
    def test_checkpoint_requires_project(self):
        with self.assertRaises(SystemExit):
            run_main(["codex", "hi", "--checkpoint"])

    def test_checkpoint_invokes_db_script(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch.object(agent_delegate, "run_child_to_files", managed_child()), \
             mock.patch.object(agent_delegate.subprocess, "run", fake_run), \
             mock.patch.object(agent_delegate, "find_binary", return_value="/bin/x"), \
             mock.patch.object(agent_delegate, "_binary_version", return_value="test"), \
             mock.patch.object(agent_delegate, "append_log"):
            code = run_main(["codex", "hi", "--checkpoint", "--project", "ENDMEMEX"])
        self.assertEqual(code, 0)
        db_calls = [c for c in calls if str(agent_delegate.DB_SCRIPT) in c[0]]
        self.assertEqual(len(db_calls), 2)
        cmd, kwargs = next(call for call in db_calls if "checkpoint" in call[0])
        self.assertIn("checkpoint", cmd)

    def test_project_publishes_deduplicated_terminal_event_without_checkpoint(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

        with mock.patch.object(agent_delegate, "run_child_to_files", managed_child()), \
             mock.patch.object(agent_delegate.subprocess, "run", fake_run), \
             mock.patch.object(agent_delegate, "find_binary", return_value="/bin/x"), \
             mock.patch.object(agent_delegate, "_binary_version", return_value="test"), \
             mock.patch.object(agent_delegate, "append_log"):
            code = run_main(["codex", "hi", "--project", "ENDMEMEX"])
        self.assertEqual(code, 0)
        event_cmd = next(command for command, _ in calls if "event-add" in command)
        self.assertIn("delegation.completed", event_cmd)
        self.assertIn("--dedupe-key", event_cmd)

    def test_checkpoint_does_not_leak_its_own_stdout(self):
        # Regression: the checkpoint subprocess must not inherit our stdout
        # fd, or its own JSON output gets interleaved with/precedes the
        # actual sub-agent result on our stdout.
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(kwargs)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch.object(agent_delegate, "run_child_to_files", managed_child()), \
             mock.patch.object(agent_delegate.subprocess, "run", fake_run), \
             mock.patch.object(agent_delegate, "find_binary", return_value="/bin/x"), \
             mock.patch.object(agent_delegate, "_binary_version", return_value="test"), \
             mock.patch.object(agent_delegate, "append_log"):
            run_main(["codex", "hi", "--checkpoint", "--project", "ENDMEMEX"])
        self.assertTrue(calls[0].get("capture_output"))

    def test_checkpoint_timeout_does_not_change_main_exit_code(self):
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)

        with mock.patch.object(agent_delegate, "run_child_to_files",
                               managed_child(stdout="child output")), \
             mock.patch.object(agent_delegate.subprocess, "run", fake_run), \
             mock.patch.object(agent_delegate, "find_binary", return_value="/bin/x"), \
             mock.patch.object(agent_delegate, "_binary_version", return_value="test"), \
             mock.patch.object(agent_delegate, "append_log"):
            code = run_main(["codex", "hi", "--checkpoint", "--project", "ENDMEMEX"])
        self.assertEqual(code, 0)  # child's exit code, not masked by checkpoint failure

    def test_checkpoint_nonzero_is_warned_without_masking_child(self):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 9, stdout="", stderr="db refused")

        stderr = __import__("io").StringIO()
        with mock.patch.object(agent_delegate, "run_child_to_files",
                               managed_child(stdout="child output")), \
             mock.patch.object(agent_delegate.subprocess, "run", fake_run), \
             mock.patch.object(agent_delegate, "find_binary", return_value="/bin/x"), \
             mock.patch.object(agent_delegate, "_binary_version", return_value="test"), \
             mock.patch.object(agent_delegate, "append_log"), \
             mock.patch.object(sys, "stderr", stderr):
            code = run_main(["codex", "hi", "--checkpoint", "--project", "ENDMEMEX"])
        self.assertEqual(code, 0)
        self.assertIn("checkpoint exited 9", stderr.getvalue())


class WaitRetryOrCancelHeartbeatTest(unittest.TestCase):
    """Regression: a live manager sleeping through retry backoff must keep
    manager_heartbeat_epoch fresh, or status/cancel will misclassify it as
    worker_died after MANAGER_STALE_S and later resurrect it to running."""

    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.runs = Path(self.tmp)
        self.enterContext(mock.patch.object(agent_delegate, "RUNS_DIR", self.runs))

    def _new_run(self):
        return agent_delegate.new_run({
            "caller": "claude", "target": "claude", "model": "haiku", "role": "worker",
        })

    def test_heartbeat_advances_during_backoff(self):
        run_id, directory = self._new_run()
        stale_epoch = time.time() - 100
        agent_delegate.update_run_state(
            directory, status="retrying", manager_pid=os.getpid(),
            manager_heartbeat_epoch=stale_epoch,
        )
        with mock.patch.object(agent_delegate, "MANAGER_HEARTBEAT_S", 0.01):
            advanced = agent_delegate.wait_retry_or_cancel(directory, 0.05)
        self.assertTrue(advanced)
        state = agent_delegate._read_json(directory / "state.json")
        self.assertGreater(state["manager_heartbeat_epoch"], stale_epoch)

    def test_live_manager_is_not_misclassified_as_worker_died_during_backoff(self):
        run_id, directory = self._new_run()
        stale_epoch = time.time() - 100
        agent_delegate.update_run_state(
            directory, status="retrying", manager_pid=os.getpid(),
            manager_heartbeat_epoch=stale_epoch,
        )
        with mock.patch.object(agent_delegate, "MANAGER_HEARTBEAT_S", 0.01):
            agent_delegate.wait_retry_or_cancel(directory, 0.05)
        stdout = __import__("io").StringIO()
        with mock.patch.object(agent_delegate, "MANAGER_STALE_S", 1.0), \
             mock.patch.object(sys, "stdout", stdout):
            agent_delegate.status_main(run_id, as_json=True)
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["status"], "retrying")
        self.assertNotEqual(result.get("error_kind"), "worker_died")

    def test_concurrent_cancel_is_preserved_through_heartbeat_merge(self):
        run_id, directory = self._new_run()
        agent_delegate.update_run_state(
            directory, status="retrying", manager_pid=os.getpid(),
            manager_heartbeat_epoch=time.time(), cancel_requested=False,
        )

        def flip_cancel_soon():
            time.sleep(0.02)
            agent_delegate.update_run_state(directory, cancel_requested=True)

        flipper = threading.Thread(target=flip_cancel_soon)
        flipper.start()
        try:
            with mock.patch.object(agent_delegate, "MANAGER_HEARTBEAT_S", 0.005):
                advanced = agent_delegate.wait_retry_or_cancel(directory, 0.3)
        finally:
            flipper.join()
        self.assertFalse(advanced)
        state = agent_delegate._read_json(directory / "state.json")
        self.assertTrue(state["cancel_requested"])


class PruneOldRunsTest(unittest.TestCase):
    """Nothing bounded RUNS_DIR before: every run kept its request, state,
    logs and (large) stdout/stderr artifacts forever."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runs = Path(self.tmp.name) / "runs"
        self.runs.mkdir()
        patcher = mock.patch.object(agent_delegate, "RUNS_DIR", self.runs)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.old_epoch = time.time() - (agent_delegate.RUN_DIR_MAX_AGE_DAYS + 1) * 86_400

    def _run_dir(self, name: str, status: str | None, *, aged: bool) -> Path:
        directory = self.runs / name
        directory.mkdir()
        (directory / "stdout.log").write_text("artifact", encoding="utf-8")
        if status is not None:
            state = directory / "state.json"
            state.write_text(json.dumps({"run_id": name, "status": status}), encoding="utf-8")
            if aged:
                os.utime(state, (self.old_epoch, self.old_epoch))
        if aged:
            os.utime(directory, (self.old_epoch, self.old_epoch))
        return directory

    def test_prunes_only_old_terminal_runs(self):
        old_done = self._run_dir("a" * 32, "completed", aged=True)
        old_failed = self._run_dir("b" * 32, "failed", aged=True)
        recent_done = self._run_dir("c" * 32, "completed", aged=False)
        old_running = self._run_dir("d" * 32, "running", aged=True)
        old_queued = self._run_dir("e" * 32, "queued", aged=True)
        removed = agent_delegate.prune_old_runs()
        self.assertEqual(removed, 2)
        self.assertFalse(old_done.exists())
        self.assertFalse(old_failed.exists())
        # Reclaiming disk is never worth deleting a live run's directory, and
        # a recent finished run is still debugging material.
        self.assertTrue(recent_done.exists())
        self.assertTrue(old_running.exists())
        self.assertTrue(old_queued.exists())

    def test_unparseable_state_is_left_alone_but_missing_state_ages_out(self):
        corrupt = self._run_dir("f" * 32, None, aged=True)
        (corrupt / "state.json").write_text("{{{ not json", encoding="utf-8")
        os.utime(corrupt / "state.json", (self.old_epoch, self.old_epoch))
        died_at_creation = self._run_dir("g" * 32, None, aged=True)
        removed = agent_delegate.prune_old_runs()
        self.assertEqual(removed, 1)
        self.assertTrue(corrupt.exists())          # cannot prove it is finished
        self.assertFalse(died_at_creation.exists())  # no state at all, long cold

    def test_prune_failure_can_never_block_a_new_run(self):
        self._run_dir("h" * 32, "completed", aged=True)
        with mock.patch.object(agent_delegate.shutil, "rmtree", side_effect=PermissionError("nope")):
            run_id, directory = agent_delegate.new_run({"target": "codex", "caller": "claude"})
        self.assertTrue((directory / "state.json").exists())
        self.assertEqual(agent_delegate._read_json(directory / "state.json")["status"], "queued")

    def test_new_run_prunes(self):
        old_done = self._run_dir("i" * 32, "completed", aged=True)
        agent_delegate.new_run({"target": "codex", "caller": "claude"})
        self.assertFalse(old_done.exists())


if __name__ == "__main__":
    unittest.main()
