from __future__ import annotations

import inspect
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ENDMEMEX = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENDMEMEX))

import db_connection  # noqa: E402
import endeavor_db  # noqa: E402


class DbConnectionExtractionTest(unittest.TestCase):
    def test_table_count_forwards_facade_table_exists(self):
        conn = sqlite3.connect(":memory:")
        try:
            with mock.patch.object(endeavor_db, "table_exists", return_value=False) as exists:
                self.assertEqual(endeavor_db.table_count(conn, "missing"), 0)
            exists.assert_called_once_with(conn, "missing")
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)
        finally:
            conn.close()

    def test_sql_script_forwards_facade_complete_statement_and_preserves_order(self):
        conn = sqlite3.connect(":memory:")
        executed = []
        original_complete_statement = sqlite3.complete_statement
        conn.set_trace_callback(executed.append)
        with mock.patch.object(endeavor_db.sqlite3, "complete_statement", wraps=original_complete_statement) as complete:
            endeavor_db.execute_sql_script(
                conn,
                "  CREATE TABLE sample (value TEXT);\n\n"
                "INSERT INTO sample VALUES ('first');\n"
                "  INSERT INTO sample VALUES ('second');\n",
            )
        try:
            self.assertGreaterEqual(len(executed), 3)
            script_statements = [statement for statement in executed if statement.strip() != "BEGIN"][:3]
            self.assertIn("CREATE TABLE sample", script_statements[0])
            self.assertIn("first", script_statements[1])
            self.assertIn("second", script_statements[2])
            self.assertEqual(
                conn.execute("SELECT value FROM sample ORDER BY rowid").fetchall(),
                [("first",), ("second",)],
            )
            self.assertGreater(complete.call_count, 0)
        finally:
            conn.close()

    def test_sql_script_incomplete_error_and_transaction_are_preserved(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE TABLE sample (value TEXT)")
            conn.commit()
            conn.execute("BEGIN")
            conn.execute("INSERT INTO sample VALUES ('before')")
            endeavor_db.execute_sql_script(conn, "INSERT INTO sample VALUES ('during');\n")
            self.assertTrue(conn.in_transaction)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 2)
            with self.assertRaisesRegex(sqlite3.OperationalError, "incomplete SQL statement in schema"):
                endeavor_db.execute_sql_script(conn, "INSERT INTO sample VALUES ('unfinished')")
            self.assertTrue(conn.in_transaction)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 2)
            conn.rollback()
        finally:
            conn.close()

    def test_sql_script_trailing_comment_does_not_raise(self):
        # A trailing SQL comment after the last real statement is not an
        # incomplete statement -- sqlite3.complete_statement("-- comment")
        # returns False (correctly, it's not a statement at all), but the old
        # code treated any non-empty leftover as incomplete SQL and raised.
        conn = sqlite3.connect(":memory:")
        try:
            endeavor_db.execute_sql_script(
                conn,
                "CREATE TABLE sample (value TEXT);\n-- trailing line comment, no statement after this\n",
            )
            self.assertTrue(
                conn.execute("SELECT 1 FROM sqlite_master WHERE name='sample'").fetchone()
            )
            endeavor_db.execute_sql_script(
                conn,
                "CREATE TABLE sample2 (value TEXT);\n/* trailing block comment */\n",
            )
            self.assertTrue(
                conn.execute("SELECT 1 FROM sqlite_master WHERE name='sample2'").fetchone()
            )
        finally:
            conn.close()

    def test_sql_script_incomplete_statement_before_trailing_comment_still_raises(self):
        # A trailing comment must not mask a genuinely incomplete statement
        # that precedes it -- only pure leftover comment/whitespace is safe.
        conn = sqlite3.connect(":memory:")
        try:
            with self.assertRaisesRegex(sqlite3.OperationalError, "incomplete SQL statement in schema"):
                endeavor_db.execute_sql_script(
                    conn,
                    "CREATE TABLE sample (value TEXT\n-- forgot the closing paren and semicolon\n",
                )
        finally:
            conn.close()

    def test_sql_script_propagates_connection_exceptions_unchanged(self):
        error = RuntimeError("sentinel")
        conn = mock.Mock()
        conn.execute.side_effect = error
        with self.assertRaises(RuntimeError) as raised:
            endeavor_db.execute_sql_script(conn, "SELECT 1;")
        self.assertIs(raised.exception, error)

    def test_sql_script_forwards_exact_facade_dependency(self):
        conn = mock.sentinel.connection
        with mock.patch.object(endeavor_db, "_execute_sql_script") as execute:
            endeavor_db.execute_sql_script(conn, "SELECT 1;")
        execute.assert_called_once_with(
            conn,
            "SELECT 1;",
            complete_statement_fn=endeavor_db.sqlite3.complete_statement,
        )

    def test_facade_connect_forwards_to_low_level_with_facade_connector(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "endeavor.sqlite3"
            with mock.patch.object(endeavor_db, "open_connection", return_value="connection") as opened:
                result = endeavor_db.connect(path, read_only=True)
            self.assertEqual(result, "connection")
            opened.assert_called_once_with(
                path, read_only=True, connect_fn=endeavor_db.sqlite3.connect
            )

    def test_facade_wrappers_keep_signatures_and_module_identity(self):
        self.assertEqual(
            str(inspect.signature(endeavor_db.connect)),
            "(path: 'Path', *, read_only: 'bool' = False) -> 'sqlite3.Connection'",
        )
        self.assertEqual(endeavor_db.connect.__module__, "endeavor_db")
        self.assertEqual(endeavor_db.table_exists.__module__, "endeavor_db")
        self.assertEqual(
            str(inspect.signature(endeavor_db.table_count)),
            "(conn: 'sqlite3.Connection', name: 'str') -> 'int'",
        )
        self.assertEqual(endeavor_db.table_count.__module__, "endeavor_db")
        self.assertEqual(
            str(inspect.signature(endeavor_db.execute_sql_script)),
            "(conn: 'sqlite3.Connection', script: 'str') -> 'None'",
        )
        self.assertEqual(endeavor_db.execute_sql_script.__module__, "endeavor_db")
        self.assertEqual(endeavor_db.database_schema_version.__module__, "endeavor_db")
        self.assertEqual(db_connection.open_connection.__module__, "db_connection")

    def test_facade_sqlite_connect_controls_low_level_open(self):
        original_connect = sqlite3.connect
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "memory.sqlite3"
            with mock.patch.object(endeavor_db.sqlite3, "connect", wraps=original_connect) as connect:
                conn = endeavor_db.connect(path)
            try:
                self.assertTrue(path.parent.is_dir())
                connect.assert_called_once_with(path, timeout=15)
                self.assertIs(conn.row_factory, sqlite3.Row)
            finally:
                conn.close()

    def test_writable_and_read_only_connections_preserve_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.sqlite3"
            writable = endeavor_db.connect(path)
            writable.execute("CREATE TABLE sample (value TEXT)")
            writable.commit()
            self.assertEqual(writable.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(writable.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            writable.close()

            read_only = endeavor_db.connect(path, read_only=True)
            try:
                self.assertIs(read_only.row_factory, sqlite3.Row)
                self.assertEqual(read_only.execute("PRAGMA query_only").fetchone()[0], 1)
                with self.assertRaises(sqlite3.OperationalError):
                    read_only.execute("CREATE TABLE rejected (value TEXT)")
            finally:
                read_only.close()

    def test_read_only_connection_percent_encodes_special_characters_in_database_path(self):
        # SQLite URI delimiters must remain part of the filename, not become
        # URI syntax that redirects a supposedly read-only open elsewhere.
        for filename in ("memory?copy.sqlite3", "memory#copy.sqlite3", "memory%copy.sqlite3", "memory copy.sqlite3"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / filename
                writable = endeavor_db.connect(path)
                writable.execute("CREATE TABLE marker (value TEXT)")
                writable.execute("INSERT INTO marker VALUES ('expected')")
                writable.commit()
                writable.close()

                read_only = endeavor_db.connect(path, read_only=True)
                try:
                    self.assertEqual(read_only.execute("SELECT value FROM marker").fetchone()[0], "expected")
                finally:
                    read_only.close()
                self.assertFalse((root / "memory").exists())

    def test_schema_version_wrapper_uses_facade_table_exists(self):
        conn = sqlite3.connect(":memory:")
        try:
            with mock.patch.object(endeavor_db, "table_exists", return_value=False) as exists:
                self.assertIsNone(endeavor_db.database_schema_version(conn))
            exists.assert_called_once_with(conn, "database_meta")
        finally:
            conn.close()

    def test_imports_do_not_write_a_database(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", "import db_connection; import endeavor_db"],
                cwd=directory,
                env={"PYTHONPATH": str(ENDMEMEX)},
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, "")
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
