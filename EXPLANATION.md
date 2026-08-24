# ENDMEMEX — อธิบายการทำงาน

**ENDMEMEX** คือระบบ **ความจำร่วม (Shared Memory)** แบบ SQLite สำหรับ AI agents เช่น **Claude Code** และ **Codex** ในโปรเจคของคุณ

---

## 🎯 วัตถุประสงค์หลัก

ระบบนี้ทำหน้าที่เป็น **ฐานความจำกลาง** ที่ทำให้ AI Agent ทั้งสองตัว:

- จำสิ่งที่ทำไว้ได้ (checkpoint / session)
- ค้นหาความรู้จากเอกสาร Markdown ที่ผ่านการตรวจสอบแล้ว
- บันทึก audit trail, fix record, verification record แบบถาวร
- ส่งต่องานระหว่างกันได้อย่างต่อเนื่อง (handoff)

---

## 🏗️ สถาปัตยกรรม

```
เอกสาร Markdown (Git-tracked)
        │
        ▼  sync_tracked.py
   chunk + hash + metadata
        ▼
   SQLite Database
   ├── documents        ← แหล่ง Markdown แต่ละไฟล์
   ├── knowledge        ← chunk ที่แยกตาม heading
   ├── knowledge_fts ×3 ← full-text search (3 แบบ)
   ├── memory_records   ← audit/fix/verification/decision
   ├── memory_relations ← ความสัมพันธ์แบบ lifecycle
   ├── sessions         ← session ของงาน
   ├── checkpoints      ← checkpoint ที่ resume ได้
   └── agent_presence   ← ใครกำลังทำงานอะไร
```

---

## ⚙️ ส่วนประกอบหลัก

| ไฟล์ | หน้าที่ |
|---|---|
| `endeavor_db.py` | **CLI หลัก** — ใช้ stdlib เท่านั้น ไม่มี dependency ภายนอก |
| `embed_server.py` | **Embedding Server** (optional) — FastAPI + MiniLM model สำหรับ semantic search |
| `sync_tracked.py` | **Sync** — ค้น Markdown ที่เปลี่ยนแล้ว ingest เข้า SQLite |
| `schema.sql` | **Schema** — ตาราง, index, trigger ทั้งหมดของ SQLite |
| `record_lifecycle.py` | **Record lifecycle** — จัดการ audit/fix/verification/decision/knowledge records |
| `retrieval.py` | **Search** — Lexical search (BM25 + RRF fusion) + optional semantic |
| `sessions.py` | **Session/Checkpoint** — จัด session, checkpoint, handoff |
| `embeddings.py` | **Embedding** — serialize/deserialize vector, HTTP client ไป embed_server |
| `config.py` | **Configuration** — ค่า default, retention caps, schema version |
| `db_connection.py` | **DB Connection** — เปิด connection, migrate schema, WAL mode |
| `activity.py` | **Activity Log** — บันทึก write events (ingest, checkpoint, record) |
| `primitives.py` | **Primitives** — data classes, utility functions |
| `errors.py` | **Error types** — custom exception classes |
| `mcp_server.py` | **MCP Server** — expose ENDMEMEX tools ให้ Claude/Codex เรียกผ่าน MCP |
| `agent_mcp_server.py` | **Agent MCP Server** — start/status/cancel managed agent runs |
| `embed_config.py` | **Embedding config** — shared embedding contract |
| `cli_parser.py` | **CLI parser** — argument parsing สำหรับ endeavor_db.py |
| `delegate_*.py` | **Delegation** — cross-agent delegation fallback |
| `watch_computer_use_handoff.py` | **Handoff** — computer use handoff between agents |

---

## 🔍 ระบบค้นหา (2 แบบ)

### 1. Lexical Search (มีเสมอ)

ใช้ 3 FTS5 index พร้อม tokenizer ต่างกัน:

| Index | Tokenizer | หน้าที่ |
|---|---|---|
| `knowledge_fts` | Unicode61 | normal exact/prefix/all-term matching |
| `knowledge_fts_porter` | Porter + Unicode61 | English stemming |
| `knowledge_fts_trigram` | Trigram (case-insensitive) | substring / identifier fragments |

**RRF (Reciprocal Rank Fusion)** — รวมผลจากทุก pass ด้วย weighted reciprocal rank:

```
pass contribution = pass_weight / (60 + rank_in_pass)
candidate RRF score = sum(all pass contributions)
```

**Deterministic boost:**
- Exact normalized query ใน title: +0.45
- ทุก query term อยู่ใน title+content: +0.25
- Training-method intent/category match: +0.50
- Bug identifier term match: +0.35

### 2. Semantic Search (optional)

- **Model:** `paraphrase-multilingual-MiniLM-L12-v2` (384-dim, float16)
- **Process:** แยกเป็น FastAPI server บน port 8770
- **CLI:** ไม่ import Torch/SentenceTransformers — communicate ผ่าน HTTP
- **Query mode:** `auto` (ใช้ถ้า ready แล้ว), `on` (spawn ถ้าจำเป็น), `off` (lexical only)
- **Backfill:** `embed-backfill` หา rows ที่ไม่มี vector หรือ stale hash → embed ใหม่
- **Scale:** exact scan capped ที่ 20,000 rows แล้วใช้ reranker over lexical candidates

---

## 📝 Markdown Knowledge Write Path

```
Agent แก้ไข .md → commit
        │
        ▼
sync_tracked.py (หรือ pre-commit hook)
        │
        ▼
compute SHA-256 hash → idempotent check
        │
        ▼
chunk ตาม heading → extract metadata (project, category, bug_id, module)
        │
        ▼
atomic transaction: upsert document → delete old chunks → insert new chunks
        │
        ▼
FTS triggers update 3 indexes (ใน transaction เดียวกัน)
        │
        ▼
best-effort embedding (หลัง commit แล้ว — ไม่ rollback lexical)
```

**Idempotency:** ถ้า content hash, index version, project, kind เหมือนเดิม → ไม่ rechunk

---

## 🔗 SQLite-Native Record Lifecycle

ระบบมี **typed lifecycle relations** ที่ enforce ใน SQLite trigger:

```
AUDIT (open)
  ▲
  └── FIX resolves AUDIT
        ▲
        └── VERIFICATION verifies FIX

NEW DECISION supersedes OLD DECISION
```

| Relation | Shape | Effect |
|---|---|---|
| `references` | any → any | ไม่มี effect |
| `resolves` | fix → audit | advance current truth |
| `verifies` | verification → fix | evidence only |
| `supersedes` | same type → same type | advance current truth |
| `contradicts` | any → any | conflict evidence |
| `duplicates` | same type → same type | duplicate evidence |

**Union-by-size materialized tree** (`memory_components`) — depth O(log N) แทน recursive transitive closure

---

## 🔄 Session & Checkpoint

```
session-start → สร้าง session (project + goal + status)
        │
        ▼
checkpoint → บันทึก summary, work_done, next_steps, blockers, files_changed
        │
        ▼
handoff → ส่งต่อ session ให้ agent อื่น resume ได้
```

- **Checkpoint retention:** 500/pinned session, 10,000 global
- **`BEGIN IMMEDIATE`** ป้องกัน race condition เมื่อ 2 agent เขียนพร้อมกัน
- **Timeline query:** parameterized join, limit 500, deterministic ordering
- **Session states:** active, paused, completed, blocked

---

## 🔒 กฎความปลอดภัยสำคัญ

1. **ฐานข้อมูลที่เขียนได้ต้องอยู่ใน host เดียว**
2. **ห้าม sync ไฟล์ SQLite ที่เขียนได้ผ่าน iCloud/Dropbox** — WAL ใช้ได้กับ
   process ที่ใช้ฐานข้อมูล local เดียวกันเท่านั้น
3. **การเขียนข้าม host ใช้ authenticated write gateway** ไม่ใช้ distributed SQLite
4. **ห้ามเก็บ secret** (token, password, private key) ใน checkpoint
5. **Checkpoint หลังทุก phase** — ไม่รอจนจบงาน
6. **Pre-commit hook** = advisory เท่านั้น — ต้อง run `sync_tracked.py` explicitly
7. **Presence writes** = opt-in เท่านั้น — ใช้เมื่อ user ขอให้ออกประกาศหรือประสานงาน

---

## 📊 Quick Reference Commands

```bash
# Initialize database
python3 endeavor_db.py init

# Bootstrap project
python3 endeavor_db.py bootstrap --project <PROJECT> --json

# Query knowledge
python3 endeavor_db.py query "<question>" --project <PROJECT> --json --compact

# Sync tracked docs
python3 sync_tracked.py <path>...

# Checkpoint
python3 endeavor_db.py checkpoint --project <PROJECT> --goal "..."

# Pending work
python3 endeavor_db.py pending --all-projects --json
python3 endeavor_db.py handoff --all-paused --json

# Embedding diagnose
python3 endeavor_db.py embed-diagnose

# Record operations
python3 endeavor_db.py record-add --type audit --project <PROJECT> --title "..." --content "..."
python3 endeavor_db.py record-show <RECORD_ID>
python3 endeavor_db.py record-search --current-only --project <PROJECT>
```

---

## 📁 ไฟล์เพิ่มเติม

- [`README.md`](README.md) — คู่มือเริ่มต้นและ command reference
- [`ENDMEMEX_USER_MANUAL.md`](ENDMEMEX_USER_MANUAL.md) — คู่มือฉบับเต็ม
- [`developer/DESIGN.md`](developer/DESIGN.md) — design document ฉบับละเอียด
- [`developer/test_endeavor_db.py`](developer/test_endeavor_db.py) — test suite
- [`developer/eval_queries.json`](developer/eval_queries.json) — retrieval regression cases
