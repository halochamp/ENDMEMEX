"""Memory-record lifecycle: relation graph, dict assembly, lexical search,
context traversal, and integrity health for the memory_records/memory_relations
tables.

Scoped by an AST call-graph check against test_endeavor_db.py's monkeypatch
surface: create_memory_record, update_memory_record,
semantic_memory_records, and search_all each reach an outward-patched name
(_refresh_memory_record_embedding_chunks / embed_texts / ensure_embed_server)
and stay in endeavor_db.py for the same reason the embeddings.py companion
chain does. Everything here has zero monkeypatch references of its own and no
outward edge into that chain.

annotate_staleness takes `root` as an explicit keyword rather than importing
config.ROOT directly: db.ROOT is an active monkeypatch target for an unrelated
function (auto_detect_changed_files) that stays in endeavor_db.py, and the
`documents.py`/MAX_CHUNK_CHARS parameter-injection convention is how this
codebase keeps a facade patch reaching a moved function.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from typing import Any, Iterable
from pathlib import Path

from config import MAX_MEMORY_CONTEXT_RECORDS, MEMORY_RELATIONS, SQL_BATCH_SIZE
from db_connection import table_exists
from primitives import batched, json_text, normalize_memory_id, now_utc
from retrieval import fts_expression, query_terms, typo_corrected_terms


def _current_ids_map(conn: sqlite3.Connection, record_ids: Iterable[str]) -> dict[str, list[str]]:
    unique_ids = list(dict.fromkeys(record_ids))
    result: dict[str, list[str]] = {record_id: [] for record_id in unique_ids}
    for batch in batched(unique_ids):
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""WITH RECURSIVE roots(record_id, component_id, parent_id, current_id) AS (
                    SELECT mapping.record_id, component.id, component.parent_id, component.current_id
                    FROM memory_record_components mapping
                    JOIN memory_components component ON component.id = mapping.component_id
                    WHERE mapping.record_id IN ({placeholders})
                    UNION ALL
                    SELECT roots.record_id, parent.id, parent.parent_id, parent.current_id
                    FROM roots JOIN memory_components parent ON parent.id = roots.parent_id
                )
                SELECT record_id, current_id FROM roots WHERE parent_id IS NULL""",
            batch,
        ).fetchall()
        for row in rows:
            result[row["record_id"]].append(row["current_id"])
    return {record_id: sorted(set(heads)) for record_id, heads in result.items()}


def _insert_memory_relation(
    conn: sqlite3.Connection,
    source_id: str,
    relation: str,
    target_id: str,
    note: str,
    agent: str,
) -> None:
    source_id = normalize_memory_id(source_id)
    target_id = normalize_memory_id(target_id)
    relation = relation.strip().lower()
    if relation not in MEMORY_RELATIONS:
        raise ValueError(f"unsupported relation: {relation}")
    if source_id == target_id:
        raise ValueError("a memory record cannot refer to itself")
    if relation in ("contradicts", "duplicates") and source_id > target_id:
        source_id, target_id = target_id, source_id
    records = {
        row["id"]: row for row in conn.execute(
            "SELECT id, record_type FROM memory_records WHERE id IN (?, ?)", (source_id, target_id)
        )
    }
    found = set(records)
    missing = sorted({source_id, target_id} - found)
    if missing:
        raise ValueError(f"relation endpoint does not exist in SQLite: {', '.join(missing)}")
    source_type = records[source_id]["record_type"]
    target_type = records[target_id]["record_type"]
    if relation == "resolves" and (source_type, target_type) != ("fix", "audit"):
        raise ValueError("resolves requires fix -> audit")
    if relation == "verifies" and (source_type, target_type) != ("verification", "fix"):
        raise ValueError("verifies requires verification -> fix")
    if relation in ("supersedes", "duplicates") and source_type != target_type:
        raise ValueError(f"{relation} requires matching record types")
    if relation in ("contradicts", "duplicates"):
        existing = conn.execute(
            """SELECT id FROM memory_relations WHERE relation = ? AND (
                   (source_id = ? AND target_id = ?)
                   OR (source_id = ? AND target_id = ?)
               )""",
            (relation, source_id, target_id, target_id, source_id),
        ).fetchone()
        if existing is not None:
            conn.execute("UPDATE memory_relations SET note = ? WHERE id = ?", (note, existing["id"]))
            return
    else:
        existing = conn.execute(
            """SELECT 1 FROM memory_relations
               WHERE source_id = ? AND target_id = ? AND relation = ?""",
            (source_id, target_id, relation),
        ).fetchone()
    if relation in ("supersedes", "resolves") and existing is None:
        heads = _current_ids_map(conn, (source_id, target_id))
        if heads.get(source_id) != [source_id]:
            raise ValueError(f"lifecycle source is not current: {source_id}")
        if heads.get(target_id) != [target_id]:
            current = ", ".join(heads.get(target_id) or []) or "unknown"
            raise ValueError(f"lifecycle target is not current: {target_id}; current={current}")
    conn.execute(
        """INSERT INTO memory_relations(source_id, target_id, relation, note, created_by, created_at)
           VALUES(?, ?, ?, ?, ?, ?)
           ON CONFLICT(source_id, target_id, relation) DO UPDATE SET
               note=excluded.note""",
        (source_id, target_id, relation, note, agent, now_utc()),
    )


def add_memory_relation(
    conn: sqlite3.Connection, source_id: str, relation: str, target_id: str, note: str, agent: str
) -> None:
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute("SAVEPOINT add_memory_relation")
    try:
        _insert_memory_relation(conn, source_id, relation, target_id, note, agent)
        project = conn.execute(
            "SELECT project FROM memory_records WHERE id = ?", (normalize_memory_id(source_id),)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO activity_log(agent, action, project, detail, created_at) VALUES(?, ?, ?, ?, ?)",
            (
                agent, "memory_relation_add", project,
                json_text({"source": source_id, "relation": relation, "target": target_id}), now_utc(),
            ),
        )
        if owns_transaction:
            conn.commit()
        else:
            conn.execute("RELEASE SAVEPOINT add_memory_relation")
    except Exception:
        if owns_transaction:
            conn.rollback()
        else:
            conn.execute("ROLLBACK TO SAVEPOINT add_memory_relation")
            conn.execute("RELEASE SAVEPOINT add_memory_relation")
        raise


def _terminal_current_ids(conn: sqlite3.Connection, record_id: str) -> list[str]:
    return _current_ids_map(conn, (record_id,)).get(record_id) or [record_id]


def _memory_records_dicts(
    conn: sqlite3.Connection, rows: Iterable[sqlite3.Row]
) -> dict[str, dict[str, Any]]:
    data_by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        data = dict(row)
        try:
            data["metadata"] = json.loads(data["metadata"])
        except (TypeError, json.JSONDecodeError):
            pass
        data_by_id[data["id"]] = data
    if not data_by_id:
        return {}

    ids = list(data_by_id)
    current_ids = _current_ids_map(conn, ids)
    incoming: dict[str, set[str]] = {record_id: set() for record_id in ids}
    conflicts: dict[str, set[str]] = {record_id: set() for record_id in ids}
    conflict_other_ids: set[str] = set()
    for batch in batched(ids, max(1, SQL_BATCH_SIZE // 2)):
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"""SELECT target_id, relation FROM memory_relations
                WHERE target_id IN ({placeholders})
                  AND relation IN ('supersedes', 'resolves')""",
            batch,
        ):
            incoming[row["target_id"]].add(row["relation"])
        for row in conn.execute(
            f"""SELECT source_id, target_id FROM memory_relations
                WHERE relation = 'contradicts'
                  AND (source_id IN ({placeholders}) OR target_id IN ({placeholders}))""",
            [*batch, *batch],
        ):
            if row["source_id"] in conflicts:
                conflicts[row["source_id"]].add(row["target_id"])
                conflict_other_ids.add(row["target_id"])
            if row["target_id"] in conflicts:
                conflicts[row["target_id"]].add(row["source_id"])
                conflict_other_ids.add(row["source_id"])
    other_heads = _current_ids_map(conn, conflict_other_ids)

    for record_id, data in data_by_id.items():
        heads = current_ids.get(record_id) or [record_id]
        data["current_record_ids"] = heads
        data["is_current"] = heads == [record_id]
        data["has_ambiguous_current"] = len(heads) > 1
        relations = incoming[record_id]
        data["effective_status"] = (
            "superseded" if "supersedes" in relations
            else "resolved" if "resolves" in relations
            else data["status"]
        )
        data["conflicts_with"] = sorted(conflicts[record_id])
        data["has_unresolved_conflict"] = data["is_current"] and any(
            other_heads.get(other_id) == [other_id] for other_id in conflicts[record_id]
        )
    return data_by_id


def _memory_record_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    return _memory_records_dicts(conn, (row,))[row["id"]]


def memory_record_context(
    conn: sqlite3.Connection, record_id: str, depth: int = 2,
    max_records: int = MAX_MEMORY_CONTEXT_RECORDS,
) -> dict[str, Any]:
    record_id = normalize_memory_id(record_id)
    root = conn.execute("SELECT * FROM memory_records WHERE id = ?", (record_id,)).fetchone()
    if root is None:
        raise ValueError(f"memory record not found: {record_id}")
    depth = max(0, min(depth, 10))
    max_records = max(1, min(max_records, MAX_MEMORY_CONTEXT_RECORDS))
    seen = {record_id}
    frontier = {record_id}
    relation_rows: dict[int, sqlite3.Row] = {}
    truncated = False
    for _ in range(depth):
        if not frontier:
            break
        next_frontier: set[str] = set()
        for batch in batched(sorted(frontier), max(1, SQL_BATCH_SIZE // 2)):
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"SELECT * FROM memory_relations "
                f"WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})",
                [*batch, *batch],
            ).fetchall()
            for row in rows:
                relation_rows[row["id"]] = row
                for endpoint in (row["source_id"], row["target_id"]):
                    if endpoint not in seen:
                        next_frontier.add(endpoint)
        remaining = max(0, max_records - len(seen))
        if len(next_frontier) > remaining:
            next_frontier = set(sorted(next_frontier)[:remaining])
            truncated = True
        seen.update(next_frontier)
        frontier = next_frontier
        if len(seen) >= max_records:
            truncated = True
            break
    record_rows: list[sqlite3.Row] = []
    for batch in batched(sorted(seen)):
        placeholders = ",".join("?" for _ in batch)
        record_rows.extend(conn.execute(
            f"SELECT * FROM memory_records WHERE id IN ({placeholders}) ORDER BY id", batch
        ).fetchall())
    records = _memory_records_dicts(conn, record_rows)
    inverse = {
        "references": "referenced_by", "resolves": "resolved_by", "verifies": "verified_by",
        "supersedes": "superseded_by", "contradicts": "contradicted_by", "duplicates": "duplicated_by",
    }
    relations = []
    for row in sorted(relation_rows.values(), key=lambda item: item["id"]):
        if row["source_id"] not in seen or row["target_id"] not in seen:
            continue
        relation = dict(row)
        relation["inverse_relation"] = inverse[relation["relation"]]
        relations.append(relation)
    return {
        "record": records[record_id], "records": records, "relations": relations,
        "truncated": truncated,
    }


def search_memory_records(
    conn: sqlite3.Connection, query: str, project: str | None = None,
    record_type: str | None = None, limit: int = 10, current_only: bool = False,
) -> list[dict[str, Any]]:
    if not table_exists(conn, "memory_records_fts"):
        return []
    limit = max(1, min(limit, 50))

    def run(table: str, expression: str, fetch_limit: int) -> list[sqlite3.Row]:
        if not expression:
            return []
        source_filters = [f"{table} MATCH ?"]
        source_params: list[Any] = [expression]
        if project:
            source_filters.append("matched.project = ?")
            source_params.append(project)
        if record_type:
            source_filters.append("matched.record_type = ?")
            source_params.append(record_type)

        if not current_only:
            return conn.execute(
                f"""SELECT matched.*,
                           bm25({table}, 4.0, 1.0, 0.4, 0.4) AS rank,
                           matched.id AS matched_via_record_id
                    FROM {table}
                    JOIN memory_records matched
                      ON matched.fts_rowid = {table}.rowid
                    WHERE {' AND '.join(source_filters)}
                    ORDER BY rank, matched.id LIMIT ?""",
                [*source_params, fetch_limit],
            ).fetchall()

        current_filters = ["ranked.choice = 1"]
        current_params: list[Any] = []
        if project:
            current_filters.append("current.project = ?")
            current_params.append(project)
        if record_type:
            current_filters.append("current.record_type = ?")
            current_params.append(record_type)
        # Resolve and deduplicate inside one query. This avoids candidate-cap
        # starvation when many historical matches point to the same current
        # record and keeps project/type scope on both the match and its head.
        return conn.execute(
            f"""WITH RECURSIVE
                    matches(origin_id, rank) AS (
                        SELECT matched.id,
                               bm25({table}, 4.0, 1.0, 0.4, 0.4)
                        FROM {table}
                        JOIN memory_records matched
                          ON matched.fts_rowid = {table}.rowid
                        WHERE {' AND '.join(source_filters)}
                    ),
                    roots(origin_id, component_id, parent_id, current_id, rank) AS (
                        SELECT matches.origin_id, component.id, component.parent_id,
                               component.current_id, matches.rank
                        FROM matches
                        JOIN memory_record_components mapping
                          ON mapping.record_id = matches.origin_id
                        JOIN memory_components component
                          ON component.id = mapping.component_id
                        UNION ALL
                        SELECT roots.origin_id, parent.id, parent.parent_id,
                               parent.current_id, roots.rank
                        FROM roots JOIN memory_components parent
                          ON parent.id = roots.parent_id
                    ),
                    ranked AS (
                        SELECT origin_id, current_id, rank,
                               row_number() OVER (
                                   PARTITION BY current_id ORDER BY rank, origin_id
                               ) AS choice
                        FROM roots WHERE parent_id IS NULL
                    )
                    SELECT current.*, ranked.rank,
                           ranked.origin_id AS matched_via_record_id
                    FROM ranked JOIN memory_records current
                      ON current.id = ranked.current_id
                    WHERE {' AND '.join(current_filters)}
                    ORDER BY ranked.rank, current.id LIMIT ?""",
            [*source_params, *current_params, fetch_limit],
        ).fetchall()

    strict = fts_expression(query, "AND")
    broad = fts_expression(query, "OR")
    if not strict:
        return []
    selected_rows = run("memory_records_fts", strict, limit)
    seen = {row["id"] for row in selected_rows}
    if not seen:
        corrected_terms, typo_reasons = typo_corrected_terms(
            conn, query_terms(query), "memory_records_fts_terms", table_exists_fn=table_exists,
        )
        if typo_reasons:
            corrected = fts_expression(" ".join(corrected_terms), "AND")
            for row in run("memory_records_fts", corrected, limit):
                if row["id"] not in seen:
                    selected_rows.append(row)
                    seen.add(row["id"])
                    if len(seen) >= limit:
                        break
    if len(seen) < limit and broad and broad != strict:
        # Strict matches keep precedence; broad OR is a genuine fallback.
        for row in run("memory_records_fts", broad, limit + len(seen)):
            if row["id"] not in seen:
                selected_rows.append(row)
                seen.add(row["id"])
                if len(seen) >= limit:
                    break
    if len(seen) < limit:
        for row in run("memory_records_fts_porter", strict, limit + len(seen)):
            if row["id"] not in seen:
                selected_rows.append(row)
                seen.add(row["id"])
                if len(seen) >= limit:
                    break
    trigram_text = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", query).casefold().strip()).replace('"', "")
    if len(seen) < min(limit, 3) and len(trigram_text.replace(" ", "")) >= 3:
        for row in run("memory_records_fts_trigram", f'"{trigram_text}"', limit + len(seen)):
            if row["id"] not in seen:
                selected_rows.append(row)
                seen.add(row["id"])
                if len(seen) >= limit:
                    break
    records = _memory_records_dicts(conn, selected_rows)
    results: list[dict[str, Any]] = []
    for row in selected_rows:
        if row["id"] not in records or any(item["id"] == row["id"] for item in results):
            continue
        item = records[row["id"]]
        if item.get("matched_via_record_id") == item["id"]:
            item.pop("matched_via_record_id", None)
        results.append(item)
    return results[:limit]


def memory_relation_health(conn: sqlite3.Connection) -> dict[str, Any]:
    if not table_exists(conn, "memory_records"):
        return {
            "available": False, "records": 0, "relations": 0, "fts_rows": 0,
            "fts_missing": 0, "fts_extra": 0, "lifecycle_cycles": 0,
            "unresolved_conflicts": 0, "ambiguous_current_records": 0,
            "component_errors": 0, "invalid_typed_relations": 0,
            "symmetric_duplicates": 0, "ok": True,
        }
    record_count = conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0]
    relation_count = conn.execute("SELECT COUNT(*) FROM memory_relations").fetchone()[0]
    record_rows = conn.execute("SELECT fts_rowid, id, record_type FROM memory_records").fetchall()
    record_ids = {row["id"] for row in record_rows}
    record_types = {row["id"]: row["record_type"] for row in record_rows}
    rowids = {row["fts_rowid"] for row in record_rows}
    native_fts_identity: dict[str, dict[str, int]] = {}
    for table in ("memory_records_fts", "memory_records_fts_porter", "memory_records_fts_trigram"):
        if not table_exists(conn, table):
            native_fts_identity[table] = {"indexed": 0, "missing": len(rowids), "extra": 0}
            continue
        indexed_rowids = {
            row["rowid"] for row in conn.execute(
                f"SELECT rowid FROM {table} WHERE {table} MATCH ?",
                ("{record_type}: (audit OR fix OR verification OR decision OR knowledge)",),
            )
        }
        native_fts_identity[table] = {
            "indexed": len(indexed_rowids),
            "missing": len(rowids - indexed_rowids),
            "extra": len(indexed_rowids - rowids),
        }
    primary_identity = native_fts_identity["memory_records_fts"]
    fts_count = primary_identity["indexed"]
    fts_missing = max(item["missing"] for item in native_fts_identity.values())
    fts_extra = max(item["extra"] for item in native_fts_identity.values())

    components = {
        row["id"]: {"parent": row["parent_id"], "current": row["current_id"], "size": row["size"]}
        for row in conn.execute("SELECT id, parent_id, current_id, size FROM memory_components")
    }
    mappings = {
        row["record_id"]: row["component_id"]
        for row in conn.execute("SELECT record_id, component_id FROM memory_record_components")
    }
    roots: dict[int, int | None] = {}
    component_cycle_nodes: set[int] = set()
    missing_parent_links = 0
    for component_id in components:
        if component_id in roots:
            continue
        trail: list[int] = []
        positions: dict[int, int] = {}
        node: int | None = component_id
        resolved_root: int | None = None
        while node is not None:
            if node in roots:
                resolved_root = roots[node]
                break
            if node in positions:
                component_cycle_nodes.update(trail[positions[node]:])
                break
            component = components.get(node)
            if component is None:
                missing_parent_links += 1
                break
            positions[node] = len(trail)
            trail.append(node)
            parent = component["parent"]
            if parent is None:
                resolved_root = node
                break
            node = int(parent)
        for child in trail:
            roots[child] = resolved_root

    mapping_errors = len(record_ids - set(mappings)) + len(set(mappings) - record_ids)
    mapping_errors += sum(component_id not in components for component_id in mappings.values())
    members_by_root: dict[int, set[str]] = {}
    current_by_record: dict[str, str | None] = {}
    for record_id, component_id in mappings.items():
        root_id = roots.get(component_id)
        if root_id is None:
            current_by_record[record_id] = None
            continue
        members_by_root.setdefault(root_id, set()).add(record_id)
        current_by_record[record_id] = components[root_id]["current"]

    component_size_errors = 0
    component_current_errors = 0
    for root_id, members in members_by_root.items():
        component = components[root_id]
        if component["size"] != len(members):
            component_size_errors += 1
        if component["current"] not in members:
            component_current_errors += 1

    relations = [dict(row) for row in conn.execute(
        "SELECT source_id, target_id, relation FROM memory_relations"
    )]
    invalid_typed_relations = 0
    target_counts: dict[str, int] = {}
    outgoing: dict[str, list[str]] = {}
    indegree = {record_id: 0 for record_id in record_ids}
    lifecycle_component_errors = 0
    for row in relations:
        source_type = record_types.get(row["source_id"])
        target_type = record_types.get(row["target_id"])
        relation = row["relation"]
        if (
            (relation == "resolves" and (source_type, target_type) != ("fix", "audit"))
            or (relation == "verifies" and (source_type, target_type) != ("verification", "fix"))
            or (relation in ("supersedes", "duplicates") and source_type != target_type)
        ):
            invalid_typed_relations += 1
        if relation in ("supersedes", "resolves"):
            target_counts[row["target_id"]] = target_counts.get(row["target_id"], 0) + 1
            outgoing.setdefault(row["source_id"], []).append(row["target_id"])
            if row["target_id"] in indegree:
                indegree[row["target_id"]] += 1
            if roots.get(mappings.get(row["source_id"])) != roots.get(mappings.get(row["target_id"])):
                lifecycle_component_errors += 1

    # Kahn's algorithm is O(N+E), unlike enumerating a transitive closure.
    queue = [record_id for record_id, degree in indegree.items() if degree == 0]
    visited = 0
    while queue:
        source = queue.pop()
        visited += 1
        for target in outgoing.get(source, ()):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    cycle_count = len(record_ids) - visited
    branch_targets = sum(count > 1 for count in target_counts.values())

    heads_by_root: dict[int, list[str]] = {}
    targeted = set(target_counts)
    for root_id, members in members_by_root.items():
        heads_by_root[root_id] = [record_id for record_id in members if record_id not in targeted]
    ambiguous_current_records = sum(len(heads) != 1 for heads in heads_by_root.values())
    component_current_errors += sum(
        len(heads) == 1 and components[root_id]["current"] != heads[0]
        for root_id, heads in heads_by_root.items()
    )

    unresolved_conflicts = sum(
        row["relation"] == "contradicts"
        and current_by_record.get(row["source_id"]) == row["source_id"]
        and current_by_record.get(row["target_id"]) == row["target_id"]
        for row in relations
    )
    symmetric_duplicates = conn.execute(
        """SELECT COUNT(*) FROM (
               SELECT relation,
                      CASE WHEN source_id < target_id THEN source_id ELSE target_id END AS a,
                      CASE WHEN source_id < target_id THEN target_id ELSE source_id END AS b
               FROM memory_relations
               WHERE relation IN ('contradicts', 'duplicates')
               GROUP BY relation, a, b HAVING COUNT(*) > 1
           )"""
    ).fetchone()[0]
    component_errors = (
        mapping_errors + missing_parent_links + len(component_cycle_nodes)
        + component_size_errors + component_current_errors + lifecycle_component_errors
    )
    return {
        "available": True,
        "records": record_count,
        "relations": relation_count,
        "fts_rows": fts_count,
        "fts_identity": native_fts_identity,
        "fts_missing": fts_missing,
        "fts_extra": fts_extra,
        "lifecycle_cycles": cycle_count,
        "lifecycle_branch_targets": branch_targets,
        "unresolved_conflicts": unresolved_conflicts,
        "ambiguous_current_records": ambiguous_current_records,
        "component_errors": component_errors,
        "invalid_typed_relations": invalid_typed_relations,
        "symmetric_duplicates": symmetric_duplicates,
        "ok": not any((
            fts_missing, fts_extra, cycle_count, branch_targets, component_errors,
            invalid_typed_relations, symmetric_duplicates,
        )),
    }


_COMPACT_RESULT_FIELDS = (
    "id", "title", "project", "category", "excerpt", "match_reasons", "rank", "source_kind",
)


def compact_result(item: dict[str, Any]) -> dict[str, Any]:
    """Trim a search result to the fields an agent actually reads when
    browsing (~29 keys down to ~8). `record-show`/opening `source_path`
    still gives the full record when a result is worth acting on."""
    location = item.get("source_path", "") or ""
    if item.get("line_start") is not None:
        location += f":{item['line_start']}-{item['line_end']}"
    elif item.get("source_heading"):
        location += f":{item['source_heading']}"
    compact = {key: item[key] for key in _COMPACT_RESULT_FIELDS if key in item}
    compact["location"] = location
    for key in ("bug_id", "status", "stale"):
        if item.get(key):
            compact[key] = item[key]
    return compact


def annotate_staleness(conn: sqlite3.Connection, results: list[dict[str, Any]], *, root: Path) -> None:
    """Flag markdown-sourced results whose indexed hash no longer matches the
    live source file, so an agent doesn't trust an excerpt that has drifted
    from disk since the last sync_tracked.py run. SQLite-native records are
    always current by construction and are left alone. Opt-in (--check-stale)
    since it reads every distinct source file once per query."""
    paths = {
        item["source_path"] for item in results
        if item.get("source_kind") == "markdown" and item.get("source_path")
    }
    if not paths:
        return
    placeholders = ",".join("?" for _ in paths)
    stored_hashes = {
        row["source_path"]: row["content_hash"]
        for row in conn.execute(
            f"SELECT source_path, content_hash FROM documents WHERE source_path IN ({placeholders})",
            list(paths),
        )
    }
    stale_cache: dict[str, bool] = {}
    for item in results:
        if item.get("source_kind") != "markdown":
            continue
        source_path = item.get("source_path")
        stored_hash = stored_hashes.get(source_path)
        if stored_hash is None:
            continue
        if source_path not in stale_cache:
            try:
                current_hash = hashlib.sha256((root / source_path).read_bytes()).hexdigest()
                stale_cache[source_path] = current_hash != stored_hash
            except OSError:
                stale_cache[source_path] = True
        item["stale"] = stale_cache[source_path]
