from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ann_index
import endeavor_db as db


class AnnIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "memory.sqlite3"
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            CREATE TABLE knowledge(id INTEGER PRIMARY KEY, embedding BLOB, updated_at TEXT);
            CREATE TABLE memory_record_embeddings(
                record_id TEXT, chunk_index INTEGER, embedding BLOB, updated_at TEXT
            );
        """)
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_sidecar_is_a_safe_unavailable_state(self):
        with mock.patch.object(ann_index, "optional_modules", side_effect=RuntimeError("missing")):
            state = ann_index.status(self.db, self.root / "ann")
        self.assertEqual(state["status"], "missing")
        self.assertFalse(state["available"])
        self.assertFalse(state["fresh"])

    def test_snapshot_change_marks_existing_sidecar_stale(self):
        directory = self.root / "ann"
        index_path, metadata_path = ann_index.paths(directory)
        directory.mkdir()
        index_path.write_bytes(b"index")
        metadata_path.write_text(json.dumps({
            "schema_version": ann_index.SIDECAR_SCHEMA,
            "dimension": ann_index.EMBED_DIM,
            "snapshot": {
                "knowledge_count": 1, "knowledge_updated_at": "old",
                "native_count": 0, "native_updated_at": "",
            },
            "count": 1, "mapping": [{"source": "knowledge", "id": 1}],
        }), encoding="utf-8")
        with mock.patch.object(ann_index, "optional_modules", return_value=(mock.Mock(), mock.Mock())):
            state = ann_index.status(self.db, directory)
        self.assertEqual(state["status"], "stale")
        self.assertFalse(state["fresh"])

    def test_query_never_loads_index_when_sidecar_is_stale(self):
        with mock.patch.object(ann_index, "status", return_value={"available": True, "fresh": False}), \
             mock.patch.object(ann_index, "optional_modules") as modules:
            self.assertEqual(
                ann_index.query(self.db, [0.0] * ann_index.EMBED_DIM, "knowledge", 20, self.root),
                [],
            )
        modules.assert_not_called()

    def test_embedding_generation_marks_same_timestamp_vector_change_stale(self):
        """A count/max-timestamp snapshot misses a same-second vector rewrite."""
        source = self.root / "source.md"
        source.write_text("# Source\n\ncontent", encoding="utf-8")
        full_db = self.root / "full-memory.sqlite3"
        conn = db.connect(full_db)
        try:
            db.initialize(conn)
            db.ingest_markdown(conn, source, "demo", "project_memory", embed=False)
            row_id = conn.execute("SELECT id FROM knowledge").fetchone()[0]
            timestamp = "2026-08-09T00:00:00+00:00"
            conn.execute(
                "UPDATE knowledge SET embedding = ?, updated_at = ? WHERE id = ?",
                (b"a" * (ann_index.EMBED_DIM * 2), timestamp, row_id),
            )
            conn.commit()
            before = ann_index.snapshot(conn)
            conn.execute(
                "UPDATE knowledge SET embedding = ?, updated_at = ? WHERE id = ?",
                (b"b" * (ann_index.EMBED_DIM * 2), timestamp, row_id),
            )
            conn.commit()
        finally:
            conn.close()

        directory = self.root / "ann"
        index_path, metadata_path = ann_index.paths(directory)
        directory.mkdir()
        index_path.write_bytes(b"index")
        metadata_path.write_text(json.dumps({
            "schema_version": ann_index.SIDECAR_SCHEMA,
            "dimension": ann_index.EMBED_DIM,
            "snapshot": before,
        }), encoding="utf-8")
        with mock.patch.object(ann_index, "optional_modules", return_value=(mock.Mock(), mock.Mock())):
            state = ann_index.status(full_db, directory)
        self.assertFalse(state["fresh"])
        self.assertEqual(state["status"], "stale")

    def test_native_embedding_generation_marks_same_timestamp_vector_change_stale(self):
        full_db = self.root / "native-memory.sqlite3"
        conn = db.connect(full_db)
        try:
            db.initialize(conn)
            record_id = db.create_memory_record(
                conn, "AUDIT-ANN-NATIVE", "demo", "audit", "Native ANN", "content", "open", "codex",
            )
            timestamp = "2026-08-09T00:00:00+00:00"
            conn.execute(
                "UPDATE memory_record_embeddings SET embedding = ?, updated_at = ? WHERE record_id = ?",
                (b"a" * (ann_index.EMBED_DIM * 2), timestamp, record_id),
            )
            conn.commit()
            before = ann_index.snapshot(conn)
            conn.execute(
                "UPDATE memory_record_embeddings SET embedding = ?, updated_at = ? WHERE record_id = ?",
                (b"b" * (ann_index.EMBED_DIM * 2), timestamp, record_id),
            )
            conn.commit()
        finally:
            conn.close()

        directory = self.root / "native-ann"
        index_path, metadata_path = ann_index.paths(directory)
        directory.mkdir()
        index_path.write_bytes(b"index")
        metadata_path.write_text(json.dumps({
            "schema_version": ann_index.SIDECAR_SCHEMA,
            "dimension": ann_index.EMBED_DIM,
            "snapshot": before,
        }), encoding="utf-8")
        with mock.patch.object(ann_index, "optional_modules", return_value=(mock.Mock(), mock.Mock())):
            state = ann_index.status(full_db, directory)
        self.assertFalse(state["fresh"])
        self.assertEqual(state["status"], "stale")


if __name__ == "__main__":
    unittest.main()
