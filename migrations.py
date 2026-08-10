"""Schema migration primitives: FTS rebuild/upgrade and pre-v4 lifecycle backfill.

None of these are monkeypatch seams (confirmed by grep against
test_endeavor_db.py: zero mock.patch.object or direct db.<name> = ...
references), so most are re-exported directly. The three that issue DDL take
`execute_sql_script` as an injected `execute_sql_script_fn` parameter rather
than importing db_connection/endeavor_db, mirroring db_connection.py's own
`table_exists_fn=`/`table_exists_fn=` pattern -- endeavor_db.py keeps thin
wrappers supplying its own (patchable) execute_sql_script.

These run raw DDL against the live production database; only the file
location moves here, not the SQL or execution order. Every statement below
is byte-identical to the pre-extraction body (verified by a mechanical
git show + ast.get_source_segment + difflib diff, not just re-reading).
"""
from __future__ import annotations

import sqlite3
from typing import Callable


def dedupe_symmetric_relations(conn: sqlite3.Connection) -> None:
    groups = conn.execute("""
        SELECT relation,
               CASE WHEN source_id < target_id THEN source_id ELSE target_id END AS a,
               CASE WHEN source_id < target_id THEN target_id ELSE source_id END AS b
        FROM memory_relations
        WHERE relation IN ('contradicts', 'duplicates')
        GROUP BY relation, a, b HAVING COUNT(*) > 1
    """).fetchall()
    for group in groups:
        rows = conn.execute("""
            SELECT id, note FROM memory_relations
            WHERE relation = ? AND (
                (source_id = ? AND target_id = ?)
                OR (source_id = ? AND target_id = ?)
            ) ORDER BY id
        """, (group["relation"], group["a"], group["b"], group["b"], group["a"])).fetchall()
        keep_id = rows[0]["id"]
        notes = list(dict.fromkeys(row["note"].strip() for row in rows if row["note"].strip()))
        conn.execute("UPDATE memory_relations SET note = ? WHERE id = ?", (" | ".join(notes), keep_id))
        conn.executemany(
            "DELETE FROM memory_relations WHERE id = ?",
            [(row["id"],) for row in rows[1:]],
        )


def ensure_memory_components(conn: sqlite3.Connection) -> None:
    """Backfill the materialized lifecycle index for pre-v4 native records.

    The production database had no native records before v4, so this is
    normally a no-op. The deterministic replay also makes partially deployed
    v3 databases fail loudly instead of returning an invented current head.
    """
    record_count = conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0]
    mapping_count = conn.execute("SELECT COUNT(*) FROM memory_record_components").fetchone()[0]
    if mapping_count == record_count:
        return
    record_ids = [row["id"] for row in conn.execute("SELECT id FROM memory_records ORDER BY id")]
    conn.execute("DELETE FROM memory_record_components")
    conn.execute("UPDATE memory_components SET parent_id = NULL")
    conn.execute("DELETE FROM memory_components")
    mapping: dict[str, int] = {}
    parent: dict[int, int | None] = {}
    current: dict[int, str] = {}
    size: dict[int, int] = {}
    for record_id in record_ids:
        cursor = conn.execute(
            "INSERT INTO memory_components(parent_id, current_id, size) VALUES(NULL, ?, 1)",
            (record_id,),
        )
        component_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO memory_record_components(record_id, component_id) VALUES(?, ?)",
            (record_id, component_id),
        )
        mapping[record_id] = component_id
        parent[component_id] = None
        current[component_id] = record_id
        size[component_id] = 1

    def root(component_id: int) -> int:
        trail = []
        while parent[component_id] is not None:
            trail.append(component_id)
            component_id = int(parent[component_id])
            if component_id in trail:
                raise ValueError("pre-v4 memory lifecycle contains a component cycle")
        for child in trail:
            parent[child] = component_id
        return component_id

    for relation in conn.execute(
        """SELECT source_id, target_id, relation FROM memory_relations
           WHERE relation IN ('supersedes', 'resolves') ORDER BY id"""
    ):
        source_root = root(mapping[relation["source_id"]])
        target_root = root(mapping[relation["target_id"]])
        if source_root == target_root:
            raise ValueError("pre-v4 memory lifecycle contains a cycle")
        if current[source_root] != relation["source_id"] or current[target_root] != relation["target_id"]:
            raise ValueError("pre-v4 memory lifecycle branches; resolve it before migrating")
        if size[source_root] >= size[target_root]:
            parent[target_root] = source_root
            size[source_root] += size[target_root]
            current[source_root] = relation["source_id"]
        else:
            parent[source_root] = target_root
            size[target_root] += size[source_root]
            current[target_root] = relation["source_id"]

    conn.executemany(
        "UPDATE memory_components SET parent_id = ?, current_id = ?, size = ? WHERE id = ?",
        [(parent[cid], current[cid], size[cid], cid) for cid in parent],
    )


def rebuild_fts(
    conn: sqlite3.Connection, *, execute_sql_script_fn: Callable[[sqlite3.Connection, str], None],
) -> None:
    """Upgrade v1 databases without losing knowledge, sessions, or checkpoints."""
    execute_sql_script_fn(conn, """
        DROP TRIGGER IF EXISTS knowledge_ai;
        DROP TRIGGER IF EXISTS knowledge_ad;
        DROP TRIGGER IF EXISTS knowledge_au;
        CREATE TRIGGER knowledge_ai AFTER INSERT ON knowledge BEGIN
            INSERT INTO knowledge_fts(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
            INSERT INTO knowledge_fts_porter(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
            INSERT INTO knowledge_fts_trigram(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
        END;
        CREATE TRIGGER knowledge_ad AFTER DELETE ON knowledge BEGIN
            INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, tags) VALUES ('delete', old.id, old.title, old.content, old.tags);
            INSERT INTO knowledge_fts_porter(knowledge_fts_porter, rowid, title, content, tags) VALUES ('delete', old.id, old.title, old.content, old.tags);
            INSERT INTO knowledge_fts_trigram(knowledge_fts_trigram, rowid, title, content, tags) VALUES ('delete', old.id, old.title, old.content, old.tags);
        END;
        CREATE TRIGGER knowledge_au AFTER UPDATE OF title, content, tags ON knowledge BEGIN
            INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, tags) VALUES ('delete', old.id, old.title, old.content, old.tags);
            INSERT INTO knowledge_fts_porter(knowledge_fts_porter, rowid, title, content, tags) VALUES ('delete', old.id, old.title, old.content, old.tags);
            INSERT INTO knowledge_fts_trigram(knowledge_fts_trigram, rowid, title, content, tags) VALUES ('delete', old.id, old.title, old.content, old.tags);
            INSERT INTO knowledge_fts(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
            INSERT INTO knowledge_fts_porter(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
            INSERT INTO knowledge_fts_trigram(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
        END;
    """)
    for table in ("knowledge_fts", "knowledge_fts_porter", "knowledge_fts_trigram"):
        conn.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")


def migrate_v5_fts(
    conn: sqlite3.Connection, *, execute_sql_script_fn: Callable[[sqlite3.Connection, str], None],
) -> None:
    """Make native-record FTS independent of SQLite's implicit rowid."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(memory_records)")}
    if "fts_rowid" not in columns:
        conn.execute("ALTER TABLE memory_records ADD COLUMN fts_rowid INTEGER")
        conn.execute("UPDATE memory_records SET fts_rowid = rowid WHERE fts_rowid IS NULL")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_records_fts_rowid ON memory_records(fts_rowid)")
    execute_sql_script_fn(conn, """
        DROP TRIGGER IF EXISTS knowledge_au;
        CREATE TRIGGER knowledge_au AFTER UPDATE OF title, content, tags ON knowledge BEGIN
            INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, tags) VALUES ('delete', old.id, old.title, old.content, old.tags);
            INSERT INTO knowledge_fts_porter(knowledge_fts_porter, rowid, title, content, tags) VALUES ('delete', old.id, old.title, old.content, old.tags);
            INSERT INTO knowledge_fts_trigram(knowledge_fts_trigram, rowid, title, content, tags) VALUES ('delete', old.id, old.title, old.content, old.tags);
            INSERT INTO knowledge_fts(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
            INSERT INTO knowledge_fts_porter(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
            INSERT INTO knowledge_fts_trigram(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
        END;
        DROP TRIGGER IF EXISTS memory_records_ai;
        DROP TRIGGER IF EXISTS memory_records_ad;
        DROP TRIGGER IF EXISTS memory_records_au;
        DROP TABLE IF EXISTS memory_records_fts_vocab;
        DROP TABLE IF EXISTS memory_records_fts;
        CREATE VIRTUAL TABLE memory_records_fts USING fts5(title, content, project, record_type, content='memory_records', content_rowid='fts_rowid', tokenize='unicode61 remove_diacritics 2');
        CREATE VIRTUAL TABLE memory_records_fts_vocab USING fts5vocab(memory_records_fts, 'instance');
        CREATE TRIGGER memory_records_ai AFTER INSERT ON memory_records BEGIN
            INSERT INTO memory_records_fts(rowid, title, content, project, record_type) VALUES (new.fts_rowid, new.title, new.content, new.project, new.record_type);
        END;
        CREATE TRIGGER memory_records_ad AFTER DELETE ON memory_records BEGIN
            INSERT INTO memory_records_fts(memory_records_fts, rowid, title, content, project, record_type) VALUES ('delete', old.fts_rowid, old.title, old.content, old.project, old.record_type);
        END;
        CREATE TRIGGER memory_records_au AFTER UPDATE OF title, content, project, record_type ON memory_records BEGIN
            INSERT INTO memory_records_fts(memory_records_fts, rowid, title, content, project, record_type) VALUES ('delete', old.fts_rowid, old.title, old.content, old.project, old.record_type);
            INSERT INTO memory_records_fts(rowid, title, content, project, record_type) VALUES (new.fts_rowid, new.title, new.content, new.project, new.record_type);
        END;
    """)
    conn.execute("INSERT INTO memory_records_fts(memory_records_fts) VALUES('rebuild')")


def migrate_v9_fts(
    conn: sqlite3.Connection, *, execute_sql_script_fn: Callable[[sqlite3.Connection, str], None],
) -> None:
    """Atomically rebuild FTS with Thai-safe tokens and native parity.

    ``unicode61`` otherwise drops Thai combining marks, turning visually
    identical words into different index tokens. All external-content tables
    and their triggers are replaced in this transaction before their rebuild,
    so a failed migration rolls back without leaving stale FTS shadow tables.
    """
    execute_sql_script_fn(conn, """
        DROP TRIGGER IF EXISTS knowledge_ai;
        DROP TRIGGER IF EXISTS knowledge_ad;
        DROP TRIGGER IF EXISTS knowledge_au;
        DROP TRIGGER IF EXISTS memory_records_ai;
        DROP TRIGGER IF EXISTS memory_records_ad;
        DROP TRIGGER IF EXISTS memory_records_au;
        DROP TABLE IF EXISTS knowledge_fts_vocab;
        DROP TABLE IF EXISTS knowledge_fts_porter_vocab;
        DROP TABLE IF EXISTS knowledge_fts_trigram_vocab;
        DROP TABLE IF EXISTS knowledge_fts_terms;
        DROP TABLE IF EXISTS knowledge_fts;
        DROP TABLE IF EXISTS knowledge_fts_porter;
        DROP TABLE IF EXISTS knowledge_fts_trigram;
        DROP TABLE IF EXISTS memory_records_fts_vocab;
        DROP TABLE IF EXISTS memory_records_fts_porter_vocab;
        DROP TABLE IF EXISTS memory_records_fts_trigram_vocab;
        DROP TABLE IF EXISTS memory_records_fts_terms;
        DROP TABLE IF EXISTS memory_records_fts;
        DROP TABLE IF EXISTS memory_records_fts_porter;
        DROP TABLE IF EXISTS memory_records_fts_trigram;

        CREATE VIRTUAL TABLE knowledge_fts USING fts5(
            title, content, tags, content='knowledge', content_rowid='id',
            tokenize='unicode61 remove_diacritics 2 categories ''L* N* Co Mn'''
        );
        CREATE VIRTUAL TABLE knowledge_fts_porter USING fts5(
            title, content, tags, content='knowledge', content_rowid='id',
            tokenize='porter unicode61 remove_diacritics 2 categories ''L* N* Co Mn'''
        );
        CREATE VIRTUAL TABLE knowledge_fts_trigram USING fts5(
            title, content, tags, content='knowledge', content_rowid='id',
            tokenize='trigram case_sensitive 0'
        );
        CREATE VIRTUAL TABLE knowledge_fts_vocab USING fts5vocab(knowledge_fts, 'instance');
        CREATE VIRTUAL TABLE knowledge_fts_porter_vocab USING fts5vocab(knowledge_fts_porter, 'instance');
        CREATE VIRTUAL TABLE knowledge_fts_trigram_vocab USING fts5vocab(knowledge_fts_trigram, 'instance');
        CREATE VIRTUAL TABLE knowledge_fts_terms USING fts5vocab(knowledge_fts, 'row');

        CREATE VIRTUAL TABLE memory_records_fts USING fts5(
            title, content, project, record_type, content='memory_records', content_rowid='fts_rowid',
            tokenize='unicode61 remove_diacritics 2 categories ''L* N* Co Mn'''
        );
        CREATE VIRTUAL TABLE memory_records_fts_porter USING fts5(
            title, content, project, record_type, content='memory_records', content_rowid='fts_rowid',
            tokenize='porter unicode61 remove_diacritics 2 categories ''L* N* Co Mn'''
        );
        CREATE VIRTUAL TABLE memory_records_fts_trigram USING fts5(
            title, content, project, record_type, content='memory_records', content_rowid='fts_rowid',
            tokenize='trigram case_sensitive 0'
        );
        CREATE VIRTUAL TABLE memory_records_fts_vocab USING fts5vocab(memory_records_fts, 'instance');
        CREATE VIRTUAL TABLE memory_records_fts_porter_vocab USING fts5vocab(memory_records_fts_porter, 'instance');
        CREATE VIRTUAL TABLE memory_records_fts_trigram_vocab USING fts5vocab(memory_records_fts_trigram, 'instance');
        CREATE VIRTUAL TABLE memory_records_fts_terms USING fts5vocab(memory_records_fts, 'row');

        CREATE TRIGGER knowledge_ai AFTER INSERT ON knowledge BEGIN
            INSERT INTO knowledge_fts(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
            INSERT INTO knowledge_fts_porter(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
            INSERT INTO knowledge_fts_trigram(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
        END;
        CREATE TRIGGER knowledge_ad AFTER DELETE ON knowledge BEGIN
            INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, tags) VALUES ('delete', old.id, old.title, old.content, old.tags);
            INSERT INTO knowledge_fts_porter(knowledge_fts_porter, rowid, title, content, tags) VALUES ('delete', old.id, old.title, old.content, old.tags);
            INSERT INTO knowledge_fts_trigram(knowledge_fts_trigram, rowid, title, content, tags) VALUES ('delete', old.id, old.title, old.content, old.tags);
        END;
        CREATE TRIGGER knowledge_au AFTER UPDATE OF title, content, tags ON knowledge BEGIN
            INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, tags) VALUES ('delete', old.id, old.title, old.content, old.tags);
            INSERT INTO knowledge_fts_porter(knowledge_fts_porter, rowid, title, content, tags) VALUES ('delete', old.id, old.title, old.content, old.tags);
            INSERT INTO knowledge_fts_trigram(knowledge_fts_trigram, rowid, title, content, tags) VALUES ('delete', old.id, old.title, old.content, old.tags);
            INSERT INTO knowledge_fts(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
            INSERT INTO knowledge_fts_porter(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
            INSERT INTO knowledge_fts_trigram(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
        END;
        CREATE TRIGGER memory_records_ai AFTER INSERT ON memory_records BEGIN
            INSERT INTO memory_records_fts(rowid, title, content, project, record_type) VALUES (new.fts_rowid, new.title, new.content, new.project, new.record_type);
            INSERT INTO memory_records_fts_porter(rowid, title, content, project, record_type) VALUES (new.fts_rowid, new.title, new.content, new.project, new.record_type);
            INSERT INTO memory_records_fts_trigram(rowid, title, content, project, record_type) VALUES (new.fts_rowid, new.title, new.content, new.project, new.record_type);
        END;
        CREATE TRIGGER memory_records_ad AFTER DELETE ON memory_records BEGIN
            INSERT INTO memory_records_fts(memory_records_fts, rowid, title, content, project, record_type) VALUES ('delete', old.fts_rowid, old.title, old.content, old.project, old.record_type);
            INSERT INTO memory_records_fts_porter(memory_records_fts_porter, rowid, title, content, project, record_type) VALUES ('delete', old.fts_rowid, old.title, old.content, old.project, old.record_type);
            INSERT INTO memory_records_fts_trigram(memory_records_fts_trigram, rowid, title, content, project, record_type) VALUES ('delete', old.fts_rowid, old.title, old.content, old.project, old.record_type);
        END;
        CREATE TRIGGER memory_records_au AFTER UPDATE OF title, content, project, record_type ON memory_records BEGIN
            INSERT INTO memory_records_fts(memory_records_fts, rowid, title, content, project, record_type) VALUES ('delete', old.fts_rowid, old.title, old.content, old.project, old.record_type);
            INSERT INTO memory_records_fts_porter(memory_records_fts_porter, rowid, title, content, project, record_type) VALUES ('delete', old.fts_rowid, old.title, old.content, old.project, old.record_type);
            INSERT INTO memory_records_fts_trigram(memory_records_fts_trigram, rowid, title, content, project, record_type) VALUES ('delete', old.fts_rowid, old.title, old.content, old.project, old.record_type);
            INSERT INTO memory_records_fts(rowid, title, content, project, record_type) VALUES (new.fts_rowid, new.title, new.content, new.project, new.record_type);
            INSERT INTO memory_records_fts_porter(rowid, title, content, project, record_type) VALUES (new.fts_rowid, new.title, new.content, new.project, new.record_type);
            INSERT INTO memory_records_fts_trigram(rowid, title, content, project, record_type) VALUES (new.fts_rowid, new.title, new.content, new.project, new.record_type);
        END;
    """)
    for table in (
        "knowledge_fts", "knowledge_fts_porter", "knowledge_fts_trigram",
        "memory_records_fts", "memory_records_fts_porter", "memory_records_fts_trigram",
    ):
        conn.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
