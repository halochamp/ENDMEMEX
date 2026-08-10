#!/usr/bin/env python3
"""Optional HNSW sidecar for zero-keyword-overlap semantic retrieval.

This helper owns the optional numpy/hnswlib imports so endeavor_db.py remains
stdlib-only. Each Mac builds its own sidecar from its local SQLite snapshot;
the files are never shared writers. A stale/missing/incompatible sidecar
returns no candidates and the caller keeps its lexical fallback.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sqlite3
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_DB = HERE / "endeavor_memory.sqlite3"
BASE_DIR = HERE / ".ann"
EMBED_DIM = 384
SIDECAR_SCHEMA = 1


def sidecar_dir() -> Path:
    machine = re.sub(r"[^A-Za-z0-9_.-]", "_", socket.gethostname())
    return BASE_DIR / machine


def paths(directory: Path | None = None) -> tuple[Path, Path]:
    root = directory or sidecar_dir()
    return root / "vectors.hnsw", root / "metadata.json"


def optional_modules():
    try:
        import hnswlib  # type: ignore
        import numpy  # type: ignore
    except ImportError as exc:
        raise RuntimeError("optional ANN requires numpy and hnswlib") from exc
    return hnswlib, numpy


def connect_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    knowledge = conn.execute(
        """SELECT COUNT(*) AS count, COALESCE(MAX(updated_at), '') AS updated_at
           FROM knowledge WHERE length(embedding) = ?""", (EMBED_DIM * 2,),
    ).fetchone()
    native = conn.execute(
        """SELECT COUNT(*) AS count, COALESCE(MAX(e.updated_at), '') AS updated_at
           FROM memory_record_embeddings e WHERE length(e.embedding) = ?""", (EMBED_DIM * 2,),
    ).fetchone() if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_record_embeddings'"
    ).fetchone() else {"count": 0, "updated_at": ""}
    generation = "0"
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='database_meta'"
    ).fetchone():
        row = conn.execute(
            "SELECT value FROM database_meta WHERE key = 'ann_generation'"
        ).fetchone()
        if row is not None:
            generation = str(row["value"])
    return {
        "knowledge_count": knowledge["count"], "knowledge_updated_at": knowledge["updated_at"],
        "native_count": native["count"], "native_updated_at": native["updated_at"],
        "ann_generation": generation,
    }


def vector_rows(conn: sqlite3.Connection):
    for row in conn.execute(
        "SELECT id, embedding FROM knowledge WHERE length(embedding) = ? ORDER BY id",
        (EMBED_DIM * 2,),
    ):
        yield {"source": "knowledge", "id": row["id"]}, row["embedding"]
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_record_embeddings'"
    ).fetchone():
        for row in conn.execute(
            """SELECT record_id, chunk_index, embedding FROM memory_record_embeddings
               WHERE length(embedding) = ? ORDER BY record_id, chunk_index""",
            (EMBED_DIM * 2,),
        ):
            yield {
                "source": "native", "id": row["record_id"], "chunk_index": row["chunk_index"],
            }, row["embedding"]


def build(db_path: Path, directory: Path | None = None) -> dict[str, Any]:
    hnswlib, numpy = optional_modules()
    target_index, target_meta = paths(directory)
    target_index.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_read_only(db_path)
    try:
        before = snapshot(conn)
        mapping: list[dict[str, Any]] = []
        vectors = []
        for item, blob in vector_rows(conn):
            mapping.append(item)
            vectors.append(struct.unpack(f"<{EMBED_DIM}e", blob))
        after = snapshot(conn)
        if before != after:
            raise RuntimeError("embedding snapshot changed during ANN build; retry")
    finally:
        conn.close()
    if not vectors:
        raise RuntimeError("no valid embeddings are available to index")
    matrix = numpy.asarray(vectors, dtype=numpy.float32)
    index = hnswlib.Index(space="cosine", dim=EMBED_DIM)
    index.init_index(max_elements=len(mapping), ef_construction=200, M=16)
    index.add_items(matrix, numpy.arange(len(mapping)))
    index.set_ef(min(max(100, 20), len(mapping)))
    with tempfile.TemporaryDirectory(dir=target_index.parent) as temp_dir:
        temp_index = Path(temp_dir) / "vectors.hnsw"
        temp_meta = Path(temp_dir) / "metadata.json"
        index.save_index(str(temp_index))
        metadata = {
            "schema_version": SIDECAR_SCHEMA, "dimension": EMBED_DIM,
            "database": str(db_path.resolve()), "snapshot": before,
            "count": len(mapping), "mapping": mapping,
        }
        temp_meta.write_text(json.dumps(metadata, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temp_index, target_index)
        os.replace(temp_meta, target_meta)
    return {"status": "ready", "index": str(target_index), "count": len(mapping), "snapshot": before}


def status(db_path: Path, directory: Path | None = None) -> dict[str, Any]:
    index_path, meta_path = paths(directory)
    dependency = True
    try:
        optional_modules()
    except RuntimeError:
        dependency = False
    if not index_path.is_file() or not meta_path.is_file():
        return {"status": "missing", "available": dependency, "fresh": False, "index": str(index_path)}
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        conn = connect_read_only(db_path)
        try:
            current = snapshot(conn)
        finally:
            conn.close()
        compatible = metadata.get("schema_version") == SIDECAR_SCHEMA and metadata.get("dimension") == EMBED_DIM
        fresh = compatible and metadata.get("snapshot") == current
        return {
            "status": "ready" if dependency and fresh else ("stale" if compatible else "incompatible"),
            "available": dependency, "fresh": fresh, "index": str(index_path),
            "count": metadata.get("count", 0), "built_snapshot": metadata.get("snapshot"),
            "current_snapshot": current,
        }
    except (OSError, json.JSONDecodeError, sqlite3.Error) as exc:
        return {"status": "invalid", "available": dependency, "fresh": False, "error": str(exc)}


def query(
    db_path: Path, vector: list[float], source: str, limit: int,
    directory: Path | None = None,
) -> list[Any]:
    state = status(db_path, directory)
    if not state.get("available") or not state.get("fresh"):
        return []
    if source not in {"knowledge", "native"} or len(vector) != EMBED_DIM:
        raise ValueError("invalid ANN source or vector dimension")
    hnswlib, numpy = optional_modules()
    index_path, meta_path = paths(directory)
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    mapping = metadata["mapping"]
    index = hnswlib.Index(space="cosine", dim=EMBED_DIM)
    index.load_index(str(index_path), max_elements=len(mapping))
    candidate_count = min(len(mapping), max(limit * 10, 100))
    index.set_ef(candidate_count)
    labels, _ = index.knn_query(numpy.asarray([vector], dtype=numpy.float32), k=candidate_count)
    results: list[Any] = []
    seen = set()
    for label in labels[0]:
        item = mapping[int(label)]
        if item["source"] != source or item["id"] in seen:
            continue
        seen.add(item["id"])
        results.append(item["id"])
        if len(results) >= limit:
            break
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build")
    sub.add_parser("status")
    query_parser = sub.add_parser("query")
    query_parser.add_argument("--source", required=True, choices=("knowledge", "native"))
    query_parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    path = Path(args.db)
    try:
        if args.command == "build":
            result = build(path)
        elif args.command == "status":
            result = status(path)
        else:
            vector = json.loads(sys.stdin.read())
            result = {"ids": query(path, vector, args.source, max(1, min(args.limit, 1000)))}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "unavailable", "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
