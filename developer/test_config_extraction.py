from __future__ import annotations

import inspect
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
import endeavor_db

_CONFIG_EXPORTS = tuple(
    sorted(
        name for name in vars(config)
        if not name.startswith("_")
        and name != "Path"  # imported for path-building, not a config constant
        and not inspect.ismodule(getattr(config, name))
    )
)


class ConfigExtractionTest(unittest.TestCase):
    def test_workspace_root_supports_monorepo_and_standalone_layouts(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            monorepo = base / "monorepo"
            nested = monorepo / "ENDMEMEX"
            nested.mkdir(parents=True)
            (monorepo / ".git").mkdir()
            self.assertEqual(config._workspace_root(nested), monorepo)

            standalone = base / "standalone"
            standalone.mkdir()
            (standalone / ".git").mkdir()
            self.assertEqual(config._workspace_root(standalone), standalone)

            unpacked = base / "unpacked" / "ENDMEMEX"
            unpacked.mkdir(parents=True)
            self.assertEqual(config._workspace_root(unpacked), unpacked.parent)

    def test_config_is_the_source_of_facade_constants(self):
        # Every non-module public name in config.py, not a hand-picked subset --
        # a slice whose entire purpose is moving constants should pin all of
        # them, so a transcription error in an unpinned one fails loudly.
        for name in _CONFIG_EXPORTS:
            with self.subTest(name=name):
                self.assertIs(getattr(endeavor_db, name), getattr(config, name))

    def test_config_has_only_named_production_importers_and_no_callables(self):
        # The monkeypatch seam (test below) only holds for endeavor_db.py
        # because nothing reads an unpatched config.X at runtime through it.
        # primitives.py, record_lifecycle.py, sessions.py, cli_parser.py, and
        # the standalone launchers are deliberate additional importers:
        # primitives.py binds
        # config.MEMORY_RELATIONS and config.SQL_BATCH_SIZE; record_lifecycle.py
        # binds those two plus config.MAX_MEMORY_CONTEXT_RECORDS; sessions.py
        # binds config.PRESENCE_STALE_SEC, config.PRESENCE_ROW_MAX_AGE_DAYS, and
        # config.SIDECAR_TEMP_MAX_AGE_SEC; cli_parser.py binds
        # config.EMBED_BATCH_SIZE, config.HERE, config.MAX_MEMORY_CONTEXT_RECORDS,
        # config.MEMORY_RECORD_STATUSES, config.MEMORY_RECORD_TYPES,
        # config.MEMORY_RELATIONS, and config.PACK_DEFAULT_BUDGET_CHARS. All are
        # only safe because none is ever a monkeypatch target in
        # test_endeavor_db.py (see the tripwire test below) -- unlike
        # MAX_CHUNK_CHARS, which documents.py receives as an explicit parameter
        # instead of importing directly, because it IS patched. A new importer
        # of config.py must be added here explicitly, not picked up silently --
        # growth of this set is a design decision.
        endmemex = Path(__file__).resolve().parent.parent
        importer_pattern = re.compile(r"^\s*(?:from|import)\s+config\b", re.MULTILINE)
        importers = sorted(
            path.relative_to(endmemex).as_posix()
            for path in endmemex.glob("*.py")
            if path.name != "config.py" and importer_pattern.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(
            importers,
            [
                "agent_delegate.py", "agent_mcp_server.py", "cli_parser.py", "endeavor_db.py",
                "mcp_server.py", "primitives.py", "record_lifecycle.py", "sessions.py",
                "watch_computer_use_handoff.py", "write_gateway.py",
            ],
        )

        callables = [name for name in _CONFIG_EXPORTS if callable(getattr(config, name))]
        self.assertEqual(callables, [])

    def test_config_names_imported_directly_by_primitives_are_never_monkeypatched(self):
        # primitives.py, record_lifecycle.py, sessions.py, and cli_parser.py
        # import these names directly rather than receiving them as
        # parameters, which is only safe as long as no test controls their
        # value through endeavor_db's facade. If a future test needs to patch
        # any of these, the importing module must switch to parameter
        # injection (the documents.py/MAX_CHUNK_CHARS pattern) in the same
        # change, not silently keep a direct import that a patch on
        # endeavor_db can no longer see.
        endmemex = Path(__file__).resolve().parent.parent
        test_source = (endmemex / "developer" / "test_endeavor_db.py").read_text(encoding="utf-8")
        for name in (
            "MEMORY_RELATIONS", "SQL_BATCH_SIZE", "MAX_MEMORY_CONTEXT_RECORDS",
            "PRESENCE_STALE_SEC", "PRESENCE_ROW_MAX_AGE_DAYS", "SIDECAR_TEMP_MAX_AGE_SEC",
            "EMBED_BATCH_SIZE", "HERE", "MEMORY_RECORD_STATUSES", "MEMORY_RECORD_TYPES",
            "PACK_DEFAULT_BUDGET_CHARS",
        ):
            with self.subTest(name=name):
                self.assertNotIn(f'"{name}"', test_source)
                self.assertNotIn(f"db.{name} = ", test_source)

    def test_facade_patches_control_real_chunk_and_presence_paths(self):
        with mock.patch.object(endeavor_db, "MAX_CHUNK_CHARS", 20):
            chunks = endeavor_db.markdown_chunks("# Heading\n\n" + "x" * 80, "source")
            self.assertTrue(all(len(chunk.content) <= 20 for chunk in chunks))

        with tempfile.TemporaryDirectory() as directory:
            presence_dir = Path(directory) / "presence"
            conn = endeavor_db.connect(Path(directory) / "memory.sqlite3")
            endeavor_db.initialize(conn)
            with mock.patch.object(endeavor_db, "PRESENCE_DIR", presence_dir), mock.patch.object(
                endeavor_db, "local_machine", return_value="config-test"
            ):
                endeavor_db.presence_start(conn, "config-test", "codex", "DEMO", "test")
            self.assertTrue((presence_dir / "config-test.json").exists())
            conn.close()

    def test_imports_are_one_way_and_do_not_write_runtime_files(self):
        endmemex = Path(__file__).resolve().parent.parent
        # Explicit raise, not a bare `assert` -- assertions are stripped under
        # -O/PYTHONOPTIMIZE, which would silently turn this into a no-op check.
        script = (
            "import config\n"
            "if 'endeavor_db' in __import__('sys').modules:\n"
            "    raise SystemExit('endeavor_db imported as a side effect of importing config')\n"
            "import endeavor_db\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(endmemex))
            before = sorted(path.relative_to(endmemex).as_posix() for path in endmemex.glob("**/*") if path.is_file())
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=directory, env=env,
                capture_output=True, text=True, check=False,
            )
            after = sorted(path.relative_to(endmemex).as_posix() for path in endmemex.glob("**/*") if path.is_file())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
