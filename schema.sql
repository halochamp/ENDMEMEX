-- Foreign-key enforcement is connection-local; endeavor_db.connect() enables it.

CREATE TABLE IF NOT EXISTS database_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    source_path TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    project TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    index_version TEXT NOT NULL DEFAULT '2',
    source_mtime REAL,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    project TEXT NOT NULL,
    category TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    metadata TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT '',
    bug_id TEXT NOT NULL DEFAULT '',
    module TEXT NOT NULL DEFAULT '',
    session_label TEXT NOT NULL DEFAULT '',
    parent_heading TEXT NOT NULL DEFAULT '',
    embedding BLOB,
    embedding_hash TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL,
    source_heading TEXT,
    source_line_start INTEGER,
    source_line_end INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_knowledge_project ON knowledge(project);
CREATE INDEX IF NOT EXISTS idx_knowledge_category ON knowledge(category);
CREATE INDEX IF NOT EXISTS idx_knowledge_document ON knowledge(document_id);

CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    title,
    content,
    tags,
    content='knowledge',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2 categories ''L* N* Co Mn'''
);

CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts_porter USING fts5(
    title,
    content,
    tags,
    content='knowledge',
    content_rowid='id',
    tokenize='porter unicode61 remove_diacritics 2 categories ''L* N* Co Mn'''
);

CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts_trigram USING fts5(
    title,
    content,
    tags,
    content='knowledge',
    content_rowid='id',
    tokenize='trigram case_sensitive 0'
);

-- fts5vocab exposes the real inverted-index document IDs. Reading rowids
-- directly from an external-content FTS table can mask a stale/missing index
-- because SQLite falls back to the content table.
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts_vocab
    USING fts5vocab(knowledge_fts, 'instance');
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts_porter_vocab
    USING fts5vocab(knowledge_fts_porter, 'instance');
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts_trigram_vocab
    USING fts5vocab(knowledge_fts_trigram, 'instance');
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts_terms
    USING fts5vocab(knowledge_fts, 'row');

CREATE TRIGGER IF NOT EXISTS knowledge_ai AFTER INSERT ON knowledge BEGIN
    INSERT INTO knowledge_fts(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, new.tags);
    INSERT INTO knowledge_fts_porter(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, new.tags);
    INSERT INTO knowledge_fts_trigram(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS knowledge_ad AFTER DELETE ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, tags)
    VALUES ('delete', old.id, old.title, old.content, old.tags);
    INSERT INTO knowledge_fts_porter(knowledge_fts_porter, rowid, title, content, tags)
    VALUES ('delete', old.id, old.title, old.content, old.tags);
    INSERT INTO knowledge_fts_trigram(knowledge_fts_trigram, rowid, title, content, tags)
    VALUES ('delete', old.id, old.title, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS knowledge_au AFTER UPDATE OF title, content, tags ON knowledge BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, title, content, tags)
    VALUES ('delete', old.id, old.title, old.content, old.tags);
    INSERT INTO knowledge_fts_porter(knowledge_fts_porter, rowid, title, content, tags)
    VALUES ('delete', old.id, old.title, old.content, old.tags);
    INSERT INTO knowledge_fts_trigram(knowledge_fts_trigram, rowid, title, content, tags)
    VALUES ('delete', old.id, old.title, old.content, old.tags);
    INSERT INTO knowledge_fts(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, new.tags);
    INSERT INTO knowledge_fts_porter(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, new.tags);
    INSERT INTO knowledge_fts_trigram(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, new.tags);
END;

CREATE TABLE IF NOT EXISTS query_feedback (
    id INTEGER PRIMARY KEY,
    agent TEXT NOT NULL,
    query_text TEXT NOT NULL,
    selected_ids TEXT NOT NULL DEFAULT '[]',
    useful INTEGER,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_query_feedback_created_at
    ON query_feedback(created_at DESC);

-- Durable host-consumable events. Producers use a stable dedupe_key, hosts
-- poll/stream by increasing id, and acknowledgement is explicit so a process
-- restart cannot silently lose a completion notification.
CREATE TABLE IF NOT EXISTS durable_events (
    id INTEGER PRIMARY KEY,
    event_type TEXT NOT NULL,
    project TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    dedupe_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    acknowledged_at TEXT,
    acknowledged_by TEXT
);

CREATE INDEX IF NOT EXISTS idx_durable_events_delivery
    ON durable_events(acknowledged_at, id);
CREATE INDEX IF NOT EXISTS idx_durable_events_project
    ON durable_events(project, id);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'paused', 'completed', 'blocked')),
    started_by TEXT NOT NULL,
    last_agent TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_project_status
    ON sessions(project, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    agent TEXT NOT NULL,
    summary TEXT NOT NULL,
    work_done TEXT NOT NULL DEFAULT '',
    current_state TEXT NOT NULL DEFAULT '',
    next_steps TEXT NOT NULL DEFAULT '',
    blockers TEXT NOT NULL DEFAULT '',
    files_changed TEXT NOT NULL DEFAULT '[]',
    commands_run TEXT NOT NULL DEFAULT '[]',
    verification TEXT NOT NULL DEFAULT '[]',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    UNIQUE(session_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_checkpoints_session
    ON checkpoints(session_id, sequence DESC);

-- idx_checkpoints_pinned is created in _finish_initialize (endeavor_db.py),
-- not here: on an existing database this script's CREATE TABLE is a no-op
-- (table already exists without the pinned column), and _finish_initialize
-- adds that column via ALTER TABLE before creating any index on it. Creating
-- the index here too would run before that ALTER TABLE on migration and fail
-- with "no such column: pinned".

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY,
    agent TEXT NOT NULL,
    action TEXT NOT NULL,
    project TEXT,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

-- Same-machine live "who is working on what" board. WAL mode makes this
-- real-time for two local processes. Cross-machine visibility is handled
-- OUTSIDE this table (see presence sidecar JSON files in endeavor_db.py) --
-- this row set is only ever read/written by processes on `machine`.
--
-- Identity is (machine, agent, project, instance), NOT pid: every CLI call
-- (and every MCP write, which shells out to the CLI) is its own short-lived
-- subprocess with a fresh pid, so pid cannot be a stable key across a
-- start -> heartbeat -> stop sequence. `pid` is kept only as an informational
-- "last process that touched this row" column. `instance` defaults to '' and
-- only needs to be set when two concurrent instances of the same agent
-- legitimately work the same project on the same machine at once.
CREATE TABLE IF NOT EXISTS agent_presence (
    id INTEGER PRIMARY KEY,
    machine TEXT NOT NULL,
    agent TEXT NOT NULL,
    project TEXT NOT NULL,
    instance TEXT NOT NULL DEFAULT '',
    pid INTEGER,
    task TEXT NOT NULL DEFAULT '',
    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'stopped')),
    started_at TEXT NOT NULL,
    last_heartbeat TEXT NOT NULL,
    UNIQUE(machine, agent, project, instance)
);

CREATE INDEX IF NOT EXISTS idx_agent_presence_project
    ON agent_presence(project, last_heartbeat DESC);

-- SQLite-native durable knowledge. Unlike `knowledge`, these records are not
-- regenerated from Markdown and their stable text IDs are safe relation
-- targets across re-indexes.
CREATE TABLE IF NOT EXISTS memory_records (
    id TEXT PRIMARY KEY,
    fts_rowid INTEGER UNIQUE NOT NULL,
    project TEXT NOT NULL,
    record_type TEXT NOT NULL CHECK(record_type IN (
        'audit', 'fix', 'verification', 'decision', 'knowledge'
    )),
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'current' CHECK(status IN (
        'open', 'current', 'resolved', 'accepted', 'superseded'
    )),
    action_state TEXT NOT NULL DEFAULT 'nonactionable' CHECK(action_state IN (
        'actionable', 'deferred', 'blocked', 'nonactionable', 'done'
    )),
    metadata TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memory_records_project
    ON memory_records(project, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_records_type_status
    ON memory_records(record_type, status, updated_at DESC);
-- idx_memory_records_action_state is created by _finish_initialize() after
-- legacy databases have received their action_state column migration.

CREATE TABLE IF NOT EXISTS memory_record_embeddings (
    record_id TEXT NOT NULL REFERENCES memory_records(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    embedding BLOB,
    embedding_hash TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(record_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_memory_record_embeddings_hash
    ON memory_record_embeddings(embedding_hash);

-- ANN freshness must change with the actual vector set, not merely with a row
-- count or a coarse timestamp. The sidecar stores this monotonic generation
-- and compares it in O(1) before it is queried.
CREATE TRIGGER IF NOT EXISTS knowledge_ann_generation_ai
AFTER INSERT ON knowledge
WHEN length(new.embedding) = 768
BEGIN
    INSERT INTO database_meta(key, value, updated_at)
    VALUES('ann_generation', '1', strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'))
    ON CONFLICT(key) DO UPDATE SET
        value = CAST(database_meta.value AS INTEGER) + 1,
        updated_at = excluded.updated_at;
END;

CREATE TRIGGER IF NOT EXISTS knowledge_ann_generation_au
AFTER UPDATE OF embedding ON knowledge
WHEN old.embedding IS NOT new.embedding
BEGIN
    INSERT INTO database_meta(key, value, updated_at)
    VALUES('ann_generation', '1', strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'))
    ON CONFLICT(key) DO UPDATE SET
        value = CAST(database_meta.value AS INTEGER) + 1,
        updated_at = excluded.updated_at;
END;

CREATE TRIGGER IF NOT EXISTS knowledge_ann_generation_ad
AFTER DELETE ON knowledge
WHEN length(old.embedding) = 768
BEGIN
    INSERT INTO database_meta(key, value, updated_at)
    VALUES('ann_generation', '1', strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'))
    ON CONFLICT(key) DO UPDATE SET
        value = CAST(database_meta.value AS INTEGER) + 1,
        updated_at = excluded.updated_at;
END;

CREATE TRIGGER IF NOT EXISTS memory_record_embeddings_ann_generation_ai
AFTER INSERT ON memory_record_embeddings
WHEN length(new.embedding) = 768
BEGIN
    INSERT INTO database_meta(key, value, updated_at)
    VALUES('ann_generation', '1', strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'))
    ON CONFLICT(key) DO UPDATE SET
        value = CAST(database_meta.value AS INTEGER) + 1,
        updated_at = excluded.updated_at;
END;

CREATE TRIGGER IF NOT EXISTS memory_record_embeddings_ann_generation_au
AFTER UPDATE OF embedding ON memory_record_embeddings
WHEN old.embedding IS NOT new.embedding
BEGIN
    INSERT INTO database_meta(key, value, updated_at)
    VALUES('ann_generation', '1', strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'))
    ON CONFLICT(key) DO UPDATE SET
        value = CAST(database_meta.value AS INTEGER) + 1,
        updated_at = excluded.updated_at;
END;

CREATE TRIGGER IF NOT EXISTS memory_record_embeddings_ann_generation_ad
AFTER DELETE ON memory_record_embeddings
WHEN length(old.embedding) = 768
BEGIN
    INSERT INTO database_meta(key, value, updated_at)
    VALUES('ann_generation', '1', strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'))
    ON CONFLICT(key) DO UPDATE SET
        value = CAST(database_meta.value AS INTEGER) + 1,
        updated_at = excluded.updated_at;
END;

CREATE TABLE IF NOT EXISTS memory_relations (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES memory_records(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES memory_records(id) ON DELETE RESTRICT,
    relation TEXT NOT NULL CHECK(relation IN (
        'references', 'resolves', 'verifies', 'supersedes',
        'contradicts', 'duplicates'
    )),
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(source_id <> target_id),
    UNIQUE(source_id, target_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_memory_relations_source
    ON memory_relations(source_id, relation);
CREATE INDEX IF NOT EXISTS idx_memory_relations_target
    ON memory_relations(target_id, relation);

-- A union tree materializes lifecycle components. Resolving the current
-- record follows component parent pointers (normally depth 0-2) instead of
-- traversing every historical edge on every query.
CREATE TABLE IF NOT EXISTS memory_components (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES memory_components(id) ON DELETE RESTRICT,
    current_id TEXT NOT NULL REFERENCES memory_records(id) ON DELETE RESTRICT,
    size INTEGER NOT NULL DEFAULT 1 CHECK(size > 0),
    CHECK(parent_id IS NULL OR parent_id <> id)
);

CREATE INDEX IF NOT EXISTS idx_memory_components_parent
    ON memory_components(parent_id);

CREATE TABLE IF NOT EXISTS memory_record_components (
    record_id TEXT PRIMARY KEY REFERENCES memory_records(id) ON DELETE CASCADE,
    component_id INTEGER NOT NULL REFERENCES memory_components(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_memory_record_components_component
    ON memory_record_components(component_id);

CREATE TRIGGER IF NOT EXISTS memory_records_component_ai
AFTER INSERT ON memory_records BEGIN
    INSERT INTO memory_components(parent_id, current_id, size)
    VALUES(NULL, new.id, 1);
    INSERT INTO memory_record_components(record_id, component_id)
    VALUES(new.id, last_insert_rowid());
END;

-- Typed-edge validation is enforced inside SQLite as well as in the CLI so
-- DB Browser/manual SQL cannot silently invert the lifecycle.
CREATE TRIGGER IF NOT EXISTS memory_relations_validate_bi
BEFORE INSERT ON memory_relations
WHEN NOT EXISTS (
    SELECT 1 FROM memory_relations existing
    WHERE existing.source_id = new.source_id
      AND existing.target_id = new.target_id
      AND existing.relation = new.relation
)
BEGIN
    SELECT CASE
        WHEN new.relation = 'resolves' AND NOT EXISTS (
            SELECT 1 FROM memory_records source, memory_records target
            WHERE source.id = new.source_id AND source.record_type = 'fix'
              AND target.id = new.target_id AND target.record_type = 'audit'
        ) THEN RAISE(ABORT, 'resolves requires fix -> audit')
    END;
    SELECT CASE
        WHEN new.relation = 'verifies' AND NOT EXISTS (
            SELECT 1 FROM memory_records source, memory_records target
            WHERE source.id = new.source_id AND source.record_type = 'verification'
              AND target.id = new.target_id AND target.record_type = 'fix'
        ) THEN RAISE(ABORT, 'verifies requires verification -> fix')
    END;
    SELECT CASE
        WHEN new.relation IN ('supersedes', 'duplicates') AND NOT EXISTS (
            SELECT 1 FROM memory_records source, memory_records target
            WHERE source.id = new.source_id AND target.id = new.target_id
              AND source.record_type = target.record_type
        ) THEN RAISE(ABORT, 'relation requires matching record types')
    END;
    SELECT CASE
        WHEN new.relation IN ('supersedes', 'resolves') AND (
            WITH RECURSIVE source_root(id, parent_id, current_id) AS (
                SELECT component.id, component.parent_id, component.current_id
                FROM memory_record_components mapping
                JOIN memory_components component ON component.id = mapping.component_id
                WHERE mapping.record_id = new.source_id
                UNION ALL
                SELECT parent.id, parent.parent_id, parent.current_id
                FROM source_root child
                JOIN memory_components parent ON parent.id = child.parent_id
            )
            SELECT current_id <> new.source_id FROM source_root WHERE parent_id IS NULL
        ) THEN RAISE(ABORT, 'lifecycle source is not current')
    END;
    SELECT CASE
        WHEN new.relation IN ('supersedes', 'resolves') AND (
            WITH RECURSIVE target_root(id, parent_id, current_id) AS (
                SELECT component.id, component.parent_id, component.current_id
                FROM memory_record_components mapping
                JOIN memory_components component ON component.id = mapping.component_id
                WHERE mapping.record_id = new.target_id
                UNION ALL
                SELECT parent.id, parent.parent_id, parent.current_id
                FROM target_root child
                JOIN memory_components parent ON parent.id = child.parent_id
            )
            SELECT current_id <> new.target_id FROM target_root WHERE parent_id IS NULL
        ) THEN RAISE(ABORT, 'lifecycle target is not current')
    END;
    SELECT CASE
        WHEN new.relation IN ('supersedes', 'resolves') AND (
            (WITH RECURSIVE source_root(id, parent_id) AS (
                SELECT component.id, component.parent_id
                FROM memory_record_components mapping
                JOIN memory_components component ON component.id = mapping.component_id
                WHERE mapping.record_id = new.source_id
                UNION ALL
                SELECT parent.id, parent.parent_id FROM source_root child
                JOIN memory_components parent ON parent.id = child.parent_id
             ) SELECT id FROM source_root WHERE parent_id IS NULL)
            =
            (WITH RECURSIVE target_root(id, parent_id) AS (
                SELECT component.id, component.parent_id
                FROM memory_record_components mapping
                JOIN memory_components component ON component.id = mapping.component_id
                WHERE mapping.record_id = new.target_id
                UNION ALL
                SELECT parent.id, parent.parent_id FROM target_root child
                JOIN memory_components parent ON parent.id = child.parent_id
             ) SELECT id FROM target_root WHERE parent_id IS NULL)
        ) THEN RAISE(ABORT, 'lifecycle relation would create a cycle')
    END;
END;

DROP TRIGGER IF EXISTS memory_relations_lifecycle_ai;
CREATE TRIGGER memory_relations_lifecycle_ai
AFTER INSERT ON memory_relations
WHEN new.relation IN ('supersedes', 'resolves')
BEGIN
    -- Union by component size bounds parent depth to O(log N). First update
    -- the winning root, then attach the smaller root below it. `current_id`
    -- is lifecycle truth and is independent of which root wins the union.
    UPDATE memory_components
    SET current_id = new.source_id,
        size = size + (
            SELECT size FROM memory_components WHERE id = (
                WITH RECURSIVE target_root(id, parent_id) AS (
                    SELECT component.id, component.parent_id
                    FROM memory_record_components mapping
                    JOIN memory_components component ON component.id = mapping.component_id
                    WHERE mapping.record_id = new.target_id
                    UNION ALL
                    SELECT parent.id, parent.parent_id FROM target_root child
                    JOIN memory_components parent ON parent.id = child.parent_id
                ) SELECT id FROM target_root WHERE parent_id IS NULL
            )
        )
    WHERE id = (
        WITH RECURSIVE source_root(id, parent_id, size) AS (
            SELECT component.id, component.parent_id, component.size
            FROM memory_record_components mapping
            JOIN memory_components component ON component.id = mapping.component_id
            WHERE mapping.record_id = new.source_id
            UNION ALL
            SELECT parent.id, parent.parent_id, parent.size FROM source_root child
            JOIN memory_components parent ON parent.id = child.parent_id
        ) SELECT id FROM source_root WHERE parent_id IS NULL
          AND size >= (
              WITH RECURSIVE target_root(id, parent_id, size) AS (
                  SELECT component.id, component.parent_id, component.size
                  FROM memory_record_components mapping
                  JOIN memory_components component ON component.id = mapping.component_id
                  WHERE mapping.record_id = new.target_id
                  UNION ALL
                  SELECT parent.id, parent.parent_id, parent.size FROM target_root child
                  JOIN memory_components parent ON parent.id = child.parent_id
              ) SELECT size FROM target_root WHERE parent_id IS NULL
          )
    );

    UPDATE memory_components
    SET current_id = new.source_id,
        size = size + (
            SELECT size FROM memory_components WHERE id = (
                WITH RECURSIVE source_root(id, parent_id) AS (
                    SELECT component.id, component.parent_id
                    FROM memory_record_components mapping
                    JOIN memory_components component ON component.id = mapping.component_id
                    WHERE mapping.record_id = new.source_id
                    UNION ALL
                    SELECT parent.id, parent.parent_id FROM source_root child
                    JOIN memory_components parent ON parent.id = child.parent_id
                ) SELECT id FROM source_root WHERE parent_id IS NULL
            )
        )
    WHERE id = (
        WITH RECURSIVE target_root(id, parent_id, size) AS (
            SELECT component.id, component.parent_id, component.size
            FROM memory_record_components mapping
            JOIN memory_components component ON component.id = mapping.component_id
            WHERE mapping.record_id = new.target_id
            UNION ALL
            SELECT parent.id, parent.parent_id, parent.size FROM target_root child
            JOIN memory_components parent ON parent.id = child.parent_id
        ) SELECT id FROM target_root WHERE parent_id IS NULL
          AND size > (
              WITH RECURSIVE source_root(id, parent_id, size) AS (
                  SELECT component.id, component.parent_id, component.size
                  FROM memory_record_components mapping
                  JOIN memory_components component ON component.id = mapping.component_id
                  WHERE mapping.record_id = new.source_id
                  UNION ALL
                  SELECT parent.id, parent.parent_id, parent.size FROM source_root child
                  JOIN memory_components parent ON parent.id = child.parent_id
              ) SELECT size FROM source_root WHERE parent_id IS NULL
          )
    );

    UPDATE memory_components
    SET parent_id = (
        WITH RECURSIVE source_root(id, parent_id) AS (
            SELECT component.id, component.parent_id
            FROM memory_record_components mapping
            JOIN memory_components component ON component.id = mapping.component_id
            WHERE mapping.record_id = new.source_id
            UNION ALL
            SELECT parent.id, parent.parent_id FROM source_root child
            JOIN memory_components parent ON parent.id = child.parent_id
        ) SELECT id FROM source_root WHERE parent_id IS NULL
    )
    WHERE id = (
        WITH RECURSIVE target_root(id, parent_id, size) AS (
            SELECT component.id, component.parent_id, component.size
            FROM memory_record_components mapping
            JOIN memory_components component ON component.id = mapping.component_id
            WHERE mapping.record_id = new.target_id
            UNION ALL
            SELECT parent.id, parent.parent_id, parent.size FROM target_root child
            JOIN memory_components parent ON parent.id = child.parent_id
        ) SELECT id FROM target_root WHERE parent_id IS NULL
          AND size < (
              WITH RECURSIVE source_root(id, parent_id, size) AS (
                  SELECT component.id, component.parent_id, component.size
                  FROM memory_record_components mapping
                  JOIN memory_components component ON component.id = mapping.component_id
                  WHERE mapping.record_id = new.source_id
                  UNION ALL
                  SELECT parent.id, parent.parent_id, parent.size FROM source_root child
                  JOIN memory_components parent ON parent.id = child.parent_id
              ) SELECT size FROM source_root WHERE parent_id IS NULL
          )
    )
    AND parent_id IS NULL;

    UPDATE memory_components
    SET parent_id = (
        WITH RECURSIVE target_root(id, parent_id) AS (
            SELECT component.id, component.parent_id
            FROM memory_record_components mapping
            JOIN memory_components component ON component.id = mapping.component_id
            WHERE mapping.record_id = new.target_id
            UNION ALL
            SELECT parent.id, parent.parent_id FROM target_root child
            JOIN memory_components parent ON parent.id = child.parent_id
        ) SELECT id FROM target_root WHERE parent_id IS NULL
    )
    WHERE id = (
        WITH RECURSIVE source_root(id, parent_id, size) AS (
            SELECT component.id, component.parent_id, component.size
            FROM memory_record_components mapping
            JOIN memory_components component ON component.id = mapping.component_id
            WHERE mapping.record_id = new.source_id
            UNION ALL
            SELECT parent.id, parent.parent_id, parent.size FROM source_root child
            JOIN memory_components parent ON parent.id = child.parent_id
        ) SELECT id FROM source_root WHERE parent_id IS NULL
          AND size < (
              WITH RECURSIVE target_root(id, parent_id, size) AS (
                  SELECT component.id, component.parent_id, component.size
                  FROM memory_record_components mapping
                  JOIN memory_components component ON component.id = mapping.component_id
                  WHERE mapping.record_id = new.target_id
                  UNION ALL
                  SELECT parent.id, parent.parent_id, parent.size FROM target_root child
                  JOIN memory_components parent ON parent.id = child.parent_id
              ) SELECT size FROM target_root WHERE parent_id IS NULL
          )
    )
    AND parent_id IS NULL;
END;

CREATE TRIGGER IF NOT EXISTS memory_relations_lifecycle_bd
BEFORE DELETE ON memory_relations
WHEN old.relation IN ('supersedes', 'resolves')
BEGIN
    SELECT RAISE(ABORT, 'lifecycle relations are immutable');
END;

CREATE TRIGGER IF NOT EXISTS memory_relations_endpoints_bu
BEFORE UPDATE OF source_id, target_id, relation ON memory_relations
BEGIN
    SELECT RAISE(ABORT, 'relation endpoints are immutable');
END;

CREATE TRIGGER IF NOT EXISTS memory_records_related_type_bu
BEFORE UPDATE OF record_type ON memory_records
WHEN new.record_type <> old.record_type AND EXISTS (
    SELECT 1 FROM memory_relations
    WHERE source_id = old.id OR target_id = old.id
)
BEGIN
    SELECT RAISE(ABORT, 'record type is immutable after relations exist');
END;

CREATE VIRTUAL TABLE IF NOT EXISTS memory_records_fts USING fts5(
    title,
    content,
    project,
    record_type,
    content='memory_records',
    content_rowid='fts_rowid',
    tokenize='unicode61 remove_diacritics 2 categories ''L* N* Co Mn'''
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_records_fts_vocab
    USING fts5vocab(memory_records_fts, 'instance');
CREATE VIRTUAL TABLE IF NOT EXISTS memory_records_fts_porter USING fts5(
    title,
    content,
    project,
    record_type,
    content='memory_records',
    content_rowid='fts_rowid',
    tokenize='porter unicode61 remove_diacritics 2 categories ''L* N* Co Mn'''
);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_records_fts_trigram USING fts5(
    title,
    content,
    project,
    record_type,
    content='memory_records',
    content_rowid='fts_rowid',
    tokenize='trigram case_sensitive 0'
);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_records_fts_porter_vocab
    USING fts5vocab(memory_records_fts_porter, 'instance');
CREATE VIRTUAL TABLE IF NOT EXISTS memory_records_fts_trigram_vocab
    USING fts5vocab(memory_records_fts_trigram, 'instance');
CREATE VIRTUAL TABLE IF NOT EXISTS memory_records_fts_terms
    USING fts5vocab(memory_records_fts, 'row');

CREATE TRIGGER IF NOT EXISTS memory_records_ai AFTER INSERT ON memory_records BEGIN
    INSERT INTO memory_records_fts(rowid, title, content, project, record_type)
    VALUES (new.fts_rowid, new.title, new.content, new.project, new.record_type);
    INSERT INTO memory_records_fts_porter(rowid, title, content, project, record_type)
    VALUES (new.fts_rowid, new.title, new.content, new.project, new.record_type);
    INSERT INTO memory_records_fts_trigram(rowid, title, content, project, record_type)
    VALUES (new.fts_rowid, new.title, new.content, new.project, new.record_type);
END;

CREATE TRIGGER IF NOT EXISTS memory_records_ad AFTER DELETE ON memory_records BEGIN
    INSERT INTO memory_records_fts(memory_records_fts, rowid, title, content, project, record_type)
    VALUES ('delete', old.fts_rowid, old.title, old.content, old.project, old.record_type);
    INSERT INTO memory_records_fts_porter(memory_records_fts_porter, rowid, title, content, project, record_type)
    VALUES ('delete', old.fts_rowid, old.title, old.content, old.project, old.record_type);
    INSERT INTO memory_records_fts_trigram(memory_records_fts_trigram, rowid, title, content, project, record_type)
    VALUES ('delete', old.fts_rowid, old.title, old.content, old.project, old.record_type);
END;

CREATE TRIGGER IF NOT EXISTS memory_records_au AFTER UPDATE OF title, content, project, record_type ON memory_records BEGIN
    INSERT INTO memory_records_fts(memory_records_fts, rowid, title, content, project, record_type)
    VALUES ('delete', old.fts_rowid, old.title, old.content, old.project, old.record_type);
    INSERT INTO memory_records_fts_porter(memory_records_fts_porter, rowid, title, content, project, record_type)
    VALUES ('delete', old.fts_rowid, old.title, old.content, old.project, old.record_type);
    INSERT INTO memory_records_fts_trigram(memory_records_fts_trigram, rowid, title, content, project, record_type)
    VALUES ('delete', old.fts_rowid, old.title, old.content, old.project, old.record_type);
    INSERT INTO memory_records_fts(rowid, title, content, project, record_type)
    VALUES (new.fts_rowid, new.title, new.content, new.project, new.record_type);
    INSERT INTO memory_records_fts_porter(rowid, title, content, project, record_type)
    VALUES (new.fts_rowid, new.title, new.content, new.project, new.record_type);
    INSERT INTO memory_records_fts_trigram(rowid, title, content, project, record_type)
    VALUES (new.fts_rowid, new.title, new.content, new.project, new.record_type);
END;
