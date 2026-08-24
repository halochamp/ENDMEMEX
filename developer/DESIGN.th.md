# ENDEAVOR Memory — การออกแบบโดยละเอียด

สถานะ: การออกแบบที่นำไปใช้งานแล้ว, schema version 12
กลุ่มผู้อ่าน: ผู้ดูแล `ENDMEMEX`, Codex และ Claude Code
จุดเริ่มต้นขณะรัน: `endeavor_db.py`

## 1. วัตถุประสงค์

ENDEAVOR Memory คือชั้นจัดเก็บข้อมูลภายในเครื่องที่ใช้ร่วมกันระหว่าง Codex และ Claude Code ใน workspace ของ ENDMEMEX โดยมี memory สามประเภทที่เกี่ยวข้องกัน แต่แยกจากกันโดยเจตนา:

1. ความรู้ที่ค้นหาได้จากไฟล์ Markdown ที่ผ่านการตรวจทานแล้ว
2. ระเบียน audit, fix, verification, decision และ knowledge ที่อยู่ใน SQLite โดยตรง พร้อมการอ้างอิงภายในที่คงที่
3. เซสชันของ agent ที่กลับมาทำต่อได้ และ checkpoint ที่มีขอบเขต เพื่อส่งต่องานระหว่าง agent

ระบบใช้ SQLite เป็นหลักสำหรับการทำดัชนีและความต่อเนื่อง แต่ไม่ได้ถือว่า SQLite เป็นแหล่งอ้างอิงหลักของข้อมูลทุกประเภท เอกสารโครงการที่มนุษย์อ่านได้ยังเป็นแหล่งอ้างอิงหลักของความรู้ที่มาจาก Markdown ส่วน `memory_records` ที่อยู่ใน SQLite โดยตรงเป็นข้อยกเว้นอย่างชัดเจน: ID ที่คงที่และความสัมพันธ์แบบมีชนิดของรายการเหล่านี้เป็นแหล่งอ้างอิงหลักภายในฐานข้อมูล

## 2. เป้าหมายการออกแบบ

- รักษาที่มาของผลลัพธ์ Markdown ทุกรายการกลับไปยังไฟล์ต้นทาง หัวข้อ และช่วงบรรทัด
- ทำให้ lexical search เป็นเส้นทางการดึงข้อมูลที่สมบูรณ์และพร้อมใช้งานเสมอ
- ถือว่า semantic search เป็นการเพิ่มคุณภาพแบบเลือกใช้ได้ ไม่ใช่ข้อกำหนดของการมีฐานข้อมูล
- ทำให้การนำเข้าซ้ำมีลักษณะ idempotent และปลอดภัย
- รองรับกระบวนการ Codex และ Claude ที่ทำงานพร้อมกันบน Mac เครื่องเดียว
- เก็บประวัติ audit ไว้ พร้อมคลี่คลายข้ออ้างเก่าไปสู่ความจริงปัจจุบัน
- จำกัดขอบเขตของ checkpoint และการไล่ความสัมพันธ์เมื่อฐานข้อมูลเติบโต
- ปิดการทำงานอย่างปลอดภัยเมื่อ lifecycle relationship ไม่ถูกต้อง มี reference ลอย มี cycle หรือมี successor ที่กำกวม
- ทำให้คำสั่งแบบอ่านอย่างเดียวไม่แก้ไขฐานข้อมูลหรือขอ write lock
- แสดง diagnostics เพียงพอเพื่อไม่ให้ agent สับสนระหว่าง sandbox policy, Python package ที่หายไป และ companion process ที่หยุดทำงาน

## 3. สิ่งที่ไม่อยู่ในเป้าหมาย

- ENDEAVOR Memory ไม่ใช่ฐานข้อมูลแบบกระจาย
- ไม่รองรับการเขียนพร้อมกันจากหลาย host ผ่านบริการ sync ไฟล์อย่างปลอดภัย
- ไม่ใช่ filesystem watcher; SQLite trigger ไม่ได้อ่านไฟล์ Markdown
- ไม่ได้แทนที่ Git history หรือเอกสารโครงการที่มนุษย์อ่านได้
- ไม่ต้องใช้ MiniLM, Torch, FastAPI หรือ network สำหรับ lexical storage และ search
- ไม่จัดเก็บทุก query โดยอัตโนมัติ
- ไม่มี approximate-nearest-neighbor vector index; semantic retrieval ใช้ exact scan ตราบเท่าที่ยังมีขอบเขตเหมาะสม จากนั้นจึงเป็น reranker บน lexical candidates

## 4. โครงสร้างไดเรกทอรี

```text
ENDMEMEX/
├── README.md                    quick start สำหรับผู้ปฏิบัติงานและรายการคำสั่ง
├── endeavor_db.py               CLI, storage, retrieval และ lifecycle ที่ใช้ stdlib เท่านั้น
├── schema.sql                   runtime schema, indexes และ SQLite triggers
├── sync_tracked.py              การซิงก์ Markdown ที่ Git ติดตามแบบคัดเลือก
├── embed_config.py              embedding contract ร่วมแบบเบา
├── embed_server.py              FastAPI MiniLM companion ที่เป็นตัวเลือก
├── endeavor_memory.sqlite3      runtime database ภายในเครื่อง; Git-ignored
└── developer/
    ├── DESIGN.md                เอกสารการออกแบบหลักสำหรับนักพัฒนา
    ├── eval_queries.json        กรณี retrieval regression
    └── test_endeavor_db.py      fast, hermetic unit/regression suite
```

`graphify-out/`, SQLite sidecars, logs, locks และ `__pycache__` เป็น artifact ที่สร้างระหว่าง runtime/development ไม่ใช่ไฟล์ design ที่เป็นแหล่งอ้างอิงหลัก

## 5. สถาปัตยกรรมระดับสูง

```text
Reviewed Git-tracked Markdown
          │
          ▼
 sync_tracked.py / ingest
          │ hash + chunk + metadata
          ▼
 documents ──1:N── knowledge ──SQLite triggers──► 3 knowledge FTS indexes
                          │
                          └──best effort──► float16 MiniLM embeddings

Direct SQLite-native writes
          │
          ▼
 memory_records ──typed edges──► memory_relations
          │                          │
          ├──► memory_records_fts    └──► materialized lifecycle components
          └──────────────────────────────► current truth resolution

Agent continuity
          │
          ▼
 sessions ──1:N── checkpoints ──► handoff

query
  ├──► multi-pass Markdown retrieval
  ├──► optional semantic pass
  └──► current SQLite-native truth
              │
              ▼
          ranked results พร้อม provenance และเหตุผลที่ match
```

## 6. ขอบเขตของกระบวนการ

### 6.1 Main CLI process

`endeavor_db.py` ตั้งใจใช้เฉพาะ Python standard library และรับผิดชอบ:

- SQLite connection และ migration management
- การแบ่ง Markdown เป็น chunk และการดึง metadata
- Lexical retrieval, RRF fusion, filtering และ reranking
- การทำงานกับ session/checkpoint
- การทำงานกับ SQLite-native record และ relation
- การ serialize embedding vector และ HTTP client สำหรับ companion
- health, integrity, FTS identity, lifecycle และ embedding diagnostics

ขอบเขตนี้ทำให้ lexical operation แบบธรรมดาทำงานได้ แม้ `python3` ที่ใช้งานอยู่จะไม่มี ML package

### 6.2 Embedding companion process

`embed_server.py` เป็น FastAPI/Uvicorn process แยกต่างหากที่ bind อยู่กับ `127.0.0.1:8770` โดย import `sentence_transformers` และเป็นเจ้าของ MiniLM model ส่วน CLI หลักสื่อสารผ่าน HTTP และไม่ import Torch หรือ SentenceTransformers เอง

Companion process เป็นขอบเขตของ warm state: model ที่โหลดแล้วมีอยู่เฉพาะขณะที่ process นั้นยังทำงาน

### 6.3 Synchronization process

`sync_tracked.py` ค้นหา Markdown ที่ผ่านการตรวจทาน เขียนโดยมนุษย์ และ Git ติดตาม แล้วเรียกเส้นทาง ingest ของ CLI ด้วย `--no-embed` ความรับผิดชอบของมันคือ lexical freshness ส่วน embedding freshness จัดการแยกต่างหากด้วย `embed-backfill`

## 7. การเปิดฐานข้อมูลและ migration

ฐานข้อมูลเริ่มต้นคือ `endeavor_memory.sqlite3` และเปลี่ยนได้ด้วย `--db` หรือ `ENDEAVOR_DB_PATH`

Writable connection ตั้งค่า:

- `PRAGMA foreign_keys = ON`
- `PRAGMA journal_mode = WAL`
- `PRAGMA synchronous = NORMAL`
- `PRAGMA busy_timeout = 15000`
- SQLite connection timeout 15 วินาที

คำสั่งแบบอ่านอย่างเดียวเปิด `file:<path>?mode=ro`, ตั้ง `query_only` และไม่รัน schema initialization กลุ่ม read-only ได้แก่ query, stats, doctor, evaluate, embed-status, การอ่าน/ค้นหา record และ handoff

คำสั่งเขียนเรียก `initialize()` ก่อนทำงาน Migration ใช้ `BEGIN IMMEDIATE`, ประมวลผล `schema.sql` โดยไม่ใช้พฤติกรรม implicit commit ของ `executescript`, สร้าง derived structures ใหม่เมื่อจำเป็น เขียน schema version เป็นขั้นตอนสุดท้าย และ rollback migration ทั้งหมดเมื่อเกิดข้อผิดพลาด

การ return เร็วเมื่อ `database_meta.schema_version` เท่ากับ `SCHEMA_VERSION` ของโค้ดแล้ว ทำให้การ initialize ซ้ำใช้เวลาแทบคงที่

## 8. โมเดลการจัดเก็บ

### 8.1 `database_meta`

เก็บ version ของ schema/index contract และเวลาที่อัปเดต ตัวบอก version จะถูกเขียนหลัง migration สำเร็จอย่างสมบูรณ์เท่านั้น

### 8.2 `documents`

หนึ่งแถวแทนไฟล์ Markdown ต้นทางหนึ่งไฟล์:

- ความไม่ซ้ำที่คงที่: `source_path`
- Idempotency: SHA-256 `content_hash` ร่วมกับ `index_version`
- การจัดเส้นทาง: `project` และ `kind`
- Provenance: title, source mtime และ import time

หาก source เปลี่ยน ingestion จะอัปเดตแถวนี้และแทนที่ `knowledge` rows ที่สร้างจากเอกสารนั้นทั้งหมดใน transaction เดียว

### 8.3 `knowledge`

หนึ่งแถวแทน Markdown chunk ที่ตระหนักถึง heading หนึ่งรายการ โดยเก็บ:

- Search text: title, content และ tags
- Routing metadata: project, category, status, bug ID, module และ session
- JSON metadata ที่มี module/bug identifier ที่ดึงได้ทั้งหมด
- Parent heading เพื่อความหลากหลายของผลลัพธ์
- source path, heading และ line range ที่ตรงจริง
- float16 embedding BLOB และ content/model hash ที่เป็นตัวเลือก

`knowledge.id` เปลี่ยนได้ เพราะการ ingest ซ้ำจะลบและสร้าง chunk ใหม่ ห้ามใช้เป็น relation target แบบถาวร

### 8.4 Knowledge FTS indexes

External-content FTS5 index สามชุดครอบคลุม retrieval failure ที่ต่างกัน:

| Index | Tokenizer | หน้าที่ |
|---|---|---|
| `knowledge_fts` | Unicode61 | exact/prefix/all-term และ broad matching ตามปกติ |
| `knowledge_fts_porter` | Porter + Unicode61 | การทำ stemming ภาษาอังกฤษ |
| `knowledge_fts_trigram` | case-insensitive trigram | substring และ identifier fragment |

Insert/update/delete trigger ทำให้ทั้งสาม index สอดคล้องกับ `knowledge` ส่วนตาราง `fts5vocab(..., 'instance')` แยกต่างหากแสดง document identity จริงของ inverted index ให้ `doctor`; การอ่าน external-content FTS table โดยตรงอาจซ่อน stale index เพราะอาจ fallback ไปยัง content table

### 8.5 `query_feedback`

เก็บ feedback ด้าน usefulness ที่ระบุชัด, result identifier ที่เลือก, notes และ agent identity Feedback เป็นหลักฐานสำหรับการปรับ ranking ที่ผ่านการตรวจทานภายหลัง ไม่แก้ ranking โดยอัตโนมัติ

### 8.6 `sessions`, `checkpoints` และ `activity_log`

` sessions` เก็บ goal ที่ใช้ร่วมกัน, state, agent ที่เริ่ม, agent ล่าสุด และ timestamps สถานะที่ถูกต้องคือ active, paused, completed และ blocked

`checkpoints` เก็บ state ที่กลับมาทำต่อได้ ได้แก่ summary, งานที่เสร็จแล้ว, state ปัจจุบัน, next steps, blockers, changed files, commands และ verification ระบบเก็บ unpinned checkpoint 500 รายการต่อ session และ 10,000 รายการทั่วโลก โดยยกเว้น checkpoint ล่าสุดของทุก session ที่ยังไม่ completed และ checkpoint ที่ pin ไว้อย่างชัดเจน

`activity_log` บันทึก write event แบบเบา เช่น ingest, checkpoint, record creation และ relation creation ไม่ใช่ raw query log

### 8.7 `memory_records`

SQLite-native record ใช้ ID ตัวพิมพ์ใหญ่คั่นด้วยขีดกลางที่คงที่ เช่น `AUDIT-ENDEAVOR-MEMORY-001` ชนิดที่รองรับคือ:

- audit
- fix
- verification
- decision
- knowledge

สถานะที่ระบุได้คือ open, current, resolved หรือ accepted สถานะ superseded และ resolved ใน lifecycle อาจคำนวณจาก incoming relation แทนการเขียนลง historical record ด้วยตนเอง

ต่างจาก knowledge ที่มาจาก Markdown, record เหล่านี้เป็นแหล่งอ้างอิงหลักภายใน SQLite และเป็น endpoint ของ durable relation ได้อย่างปลอดภัย

### 8.8 `memory_relations`

Relation มีทิศทางจาก record ที่กำลังยืนยัน/ใหม่กว่าไปยัง target:

| Relation | รูปแบบที่ต้องการ | ผลต่อ lifecycle |
|---|---|---|
| `references` | record ที่มีอยู่ใด ๆ ไปยัง record อื่น | ไม่มี |
| `resolves` | fix → audit | ทำให้ current truth เดินหน้า |
| `verifies` | verification → fix | เป็นหลักฐานเท่านั้น |
| `supersedes` | record ชนิดเดียวกัน → record ชนิดเดียวกัน | ทำให้ current truth เดินหน้า |
| `contradicts` | record ที่มีอยู่สองรายการใด ๆ | หลักฐานความขัดแย้ง |
| `duplicates` | record ชนิดที่ตรงกัน | หลักฐานรายการซ้ำ |

Foreign key ปฏิเสธ endpoint ที่ไม่มีอยู่ SQLite trigger บังคับใช้รูปแบบ edge ตามชนิด, current-source/current-target requirement, ห้าม lifecycle cycle และห้ามมี parallel successor `contradicts` และ `duplicates` เป็นความสัมพันธ์สมมาตรและจัดเก็บในทิศทางมาตรฐานด้วย unique symmetric index

### 8.9 Materialized lifecycle components

`memory_components` และ `memory_record_components` ทำให้ประวัติ `resolves`/`supersedes` ที่เชื่อมต่อกันอยู่ในรูป materialized component ทุก record ใหม่เริ่มใน component ที่มี record เดียว การเพิ่ม lifecycle edge จะรวม component สองชุดตามขนาด และตั้ง `current_id` ของ root เป็น source ที่ใหม่กว่า/กำลังยืนยัน

Union-by-size จำกัดความลึกของ parent ไว้ที่ O(log N) โดยปกติศูนย์ถึงสองระดับ แทนการทำ recursive transitive closure ทุกครั้งที่ query อ่านสามารถ resolve record ID จำนวนมากไปยัง current head ได้เป็น batch

### 8.10 `memory_records_fts`

ให้ full-text retrieval สำหรับ native record ID, project, type, title, content และ status โดย trigger จะ sync กับ `memory_records`

## 9. เส้นทางการเขียนความรู้จาก Markdown

Markdown ไม่ถูกดึงเข้า SQLite ด้วย trigger เส้นทางที่ต้องใช้คือ:

```text
agent แก้ไข .md ที่ผ่านการตรวจทาน
        │
        ├── explicit: python3 sync_tracked.py
        └── advisory: local Git pre-commit hook เรียก sync เดียวกัน
                         │
                         ▼
                   ingest_markdown()
```

### 9.1 การค้นหา source

`sync_tracked.py` ขอรายชื่อไฟล์ `*.md` ที่ Git ติดตาม ยกเว้น generated directory, virtual environment, third-party notice, source library/translation ที่เลือกไว้ และ prompt-baseline snapshot ขนาดใหญ่ `project` และ `kind` ถูกสร้างจาก path ที่สัมพันธ์กับ repository แบบกำหนดแน่นอน

Local pre-commit hook เป็น advisory และไม่ได้อยู่ในการติดตามของ Git การแก้ไข source ที่ยังไม่ commit จึงไม่ถูก sync โดยอัตโนมัติ agent ต้องเรียก `sync_tracked.py` อย่างชัดเจน

### 9.2 การตัดสินใจด้าน idempotency

Ingestion อ่าน raw bytes, decode เป็น UTF-8 และคำนวณ SHA-256 source จะถือว่าไม่เปลี่ยนก็ต่อเมื่อ content hash, index version, project และ kind ตรงกันทั้งหมด เอกสารที่ไม่เปลี่ยนจะไม่ถูก rechunk แต่ embedding ที่หายหรือ stale อาจได้รับการซ่อมเมื่อเปิดใช้ embedding

### 9.3 Chunking และ metadata

ตัวแยก Markdown สร้าง chunk ที่รู้จัก heading และเก็บช่วงบรรทัดของ source ไว้ ย่อหน้าที่ใหญ่เกินไปจะถูก hard-split ที่ขอบเขต grapheme ที่ปลอดภัย เพื่อให้ chunk ไม่เกิน 500 ตัวอักษรโดยไม่ทิ้ง combining mark ภาษาไทยไว้ต้น chunk ถัดไป การจำแนกและการดึง metadata จะหา category, status, bug identifier, module name, session label, parent heading และ tag

### 9.4 การแทนที่ lexical แบบ atomic

Write transaction จะ upsert แถว `documents`, ลบ chunk เก่าของเอกสาร, แทรก chunk ใหม่ทั้งหมด และเขียน activity event FTS trigger จะอัปเดต lexical index ทั้งสามภายใน transaction เดียว หากล้มเหลว lexical replacement ทั้งหมดจะ rollback

### 9.5 Embedding แบบ best-effort หลัง commit

Embedding เกิดขึ้นหลัง lexical ingestion commit เท่านั้น ดังนั้น companion/package/model failure จะไม่สามารถลบหรือ rollback ความรู้ lexical ที่ค้นหาได้

## 10. เส้นทางการเขียน SQLite-native record

Audit/fix/verification record ที่ต้องมี reference ถาวรเขียนโดยตรงผ่าน `record-add`, `record-update` และ `record-link`

`record-add --link ...` สร้าง record และ relation ที่ร้องขอทั้งหมดใน transaction เดียว หาก target หรือ relation ใดไม่ถูกต้อง record ใหม่และ link ทั้งหมดจะ rollback พร้อมกัน

เมื่อ truth ในอดีตเปลี่ยน จะไม่เขียนทับ historical truth เส้นทางที่แนะนำคือ:

```text
AUDIT (open)
  ▲
  └── FIX resolves AUDIT
        ▲
        └── VERIFICATION verifies FIX

NEW DECISION supersedes OLD DECISION
```

`record-update` สงวนไว้สำหรับแก้ไข/เติมข้อมูล assertion เดิม ส่วน `supersedes` ใช้แทน assertion ที่เปลี่ยนไป

## 11. เส้นทางการเขียน session และ checkpoint

`session-start` สร้าง goal ที่มีขอบเขตตาม project การค้นด้วย project อย่างเดียวสำเร็จได้เมื่อมี session ที่ active/paused/blocked ตรงกันเพียงหนึ่งรายการเท่านั้น หากมีหลายรายการ caller ต้องส่ง session ID ที่ผู้ใช้เลือกผ่าน handoff, bootstrap, pack และ checkpoint จากนั้น checkpoint จึงเข้า `BEGIN IMMEDIATE` ก่อนจัดสรร sequence ถัดไป วิธีนี้ป้องกันไม่ให้ agent สองตัวอ่าน `MAX(sequence)` เดียวกันและพยายามใช้ sequence ถัดไปเดียวกันพร้อมกัน

การ insert checkpoint, cleanup retention ทั่วโลก, การอัปเดต session state/last-agent และ activity event จะ commit แบบ atomic session ที่ completed แล้วไม่สามารถถูกเปิดใหม่ด้วย checkpoint ภายหลังได้ `handoff` คืน session ที่เลือกและ checkpoint ล่าสุดโดยไม่เขียนข้อมูล

## 12. Lexical retrieval pipeline

เส้นทาง query จะ normalize whitespace, ดึง non-stopword term ได้ไม่เกิน 12 คำ เติม reviewed Thai/English alias และสร้าง FTS expression คำ ASCII ที่มีความยาวอย่างน้อย 4 ตัวอักษรใช้ prefix matching ส่วนคำไทยยังใช้ exact matching

แต่ละ FTS pass คืน candidate ได้ไม่เกิน 30 รายการ BM25 ใช้น้ำหนัก field 4.0 สำหรับ title, 1.0 สำหรับ content และ 0.8 สำหรับ tags

| Pass | น้ำหนัก | วัตถุประสงค์ |
|---|---:|---|
| all terms, Unicode FTS | 1.6 | lexical match ที่แม่นยำ |
| any term, Unicode FTS | 0.8 | เพิ่ม broad recall |
| all terms, Porter FTS | 1.2 | morphology ภาษาอังกฤษ |
| trigram phrase | 0.9 | กู้คืน substring/identifier |
| metadata rescue | 1.5 | กู้คืน bug/module/session ID ที่มี punctuation มาก |

แต่ละ pass เพิ่ม weighted reciprocal rank:

```text
pass contribution = pass_weight / (60 + rank_in_pass)
candidate RRF score = sum(all pass contributions)
```

การจัดอันดับด้วย rank แทนค่าดิบของ BM25/cosine ทำให้ retrieval strategy ที่เป็นอิสระต่อกันนำมาเปรียบเทียบได้

หลัง fusion จะใช้ deterministic boost:

- query ที่ normalize แล้วอยู่ใน title แบบตรงตัว: +0.45
- query ทุกคำปรากฏใน title+content: +0.25
- ตรงกับ training-method intent/category: +0.50
- ตรงกับ intent/category อื่นที่รู้จัก: +0.22
- resolved knowledge: +0.12
- accepted historical knowledge: -0.05
- ตรงกับ bug identifier term: +0.35

การเลือกสุดท้ายลบคู่ title/content ที่ซ้ำ และให้ผลลัพธ์จาก parent heading เดียวกันได้ไม่เกินสองรายการ หากคะแนนเท่ากันจะใช้ row ID ที่เรียงคงที่

ผลลัพธ์ทุกรายการแสดง `match_reasons`; rank เป็นเพียงหลักฐานว่าค้นพบ ไม่ใช่หลักฐานว่าเนื้อหาถูกต้อง

## 13. Semantic retrieval pipeline

### 13.1 Vector contract

- Model: `paraphrase-multilingual-MiniLM-L12-v2`
- Dimension: 384
- รูปแบบจัดเก็บ: normalized float16 little-endian BLOB ขนาด 768 bytes ต่อแถว
- Content identity: SHA-256 ของ `model_name + ':' + content`
- HTTP batch size: ข้อความ 256 รายการ

BLOB ที่ผิดรูปแบบ หรือ content/model hash ที่เก่าจะถือว่า pending และไม่นำไปจัดอันดับ

### 13.2 วงจรชีวิตของ companion

`ensure_embed_server()` จะนำ service ที่พร้อมและเข้ากันได้กลับมาใช้ก่อน การเริ่มต้นถูก serialize ด้วย `.embed_start.lock` เพื่อไม่ให้ agent หลายตัวแย่ง bind port 8770 launcher จะ probe interpreter แยกจาก `sys.executable` ของ CLI และ `ENDEAVOR_EMBED_PYTHON` เป็น override ที่มีอำนาจสูงสุด

Server โหลด model ด้วย `local_files_only=True` และไม่เปลี่ยน query ให้กลายเป็นการ download โดยนัย process จะออกหลังไม่มี active request เป็นเวลาหนึ่งชั่วโมง เพื่อคืน model RAM

### 13.3 โหมด query

- `auto`: ใช้ semantic เฉพาะเมื่อ companion พร้อมอยู่แล้ว; ไม่ spawn
- `on`: spawn/wait เมื่อจำเป็น
- `off`: lexical เท่านั้น
- `ready` ภายใน: evaluation ได้ทำ warm probe ครั้งเดียวแล้ว

เมื่อ embedded row ที่อยู่ใน scope มีไม่เกิน 20,000 รายการ ระบบจะเปรียบเทียบ query vector กับ embedding ที่ valid ทุกตัว เพื่อรักษา recall แม้ไม่มีคำซ้ำกันเลย หากเกิน 20,000 รายการ semantic scoring จะจำกัดอยู่ที่ lexical candidate ID และไม่เกิน 20 embedding chunks ต่อ candidate record ซึ่งทำให้ query ที่มี 100k row หรือ record ขนาดใหญ่มากมีขอบเขตโดยไม่ต้องใช้ ANN dependency
สำหรับ SQLite-native record คะแนนที่ match ถ้อยคำของ historical member จะถูกส่งไปยัง current head ที่ materialize ไว้และ deduplicate ตาม head ก่อนจัดอันดับ จึงยังค้นคำเก่าได้โดยไม่คืน stale truth

Cosine similarity เป็น dot product เพราะ stored และ query vector ถูก normalize แล้ว semantic row 30 อันดับแรกเพิ่ม `1.0 / (60 + semantic_rank)` เข้า RRF score เดียวกับ lexical pass

### 13.4 Backfill

`embed-backfill` ค้นหา row ที่ไม่มี vector, BLOB length ไม่ถูกต้อง หรือ embedding hash เก่า แล้ว embed เป็น batch ที่มีขอบเขตและอัปเดตเฉพาะ row เหล่านั้น การเปลี่ยน model name จะ invalidate hash เก่าโดยอัตโนมัติ และทำให้ backfill ครั้งถัดไปซ่อมแซมได้เอง

### 13.5 Diagnostics hard gate

`embed-diagnose` รายงาน:

- CLI Python และ version
- companion Python candidate ทุกตัว
- ความพร้อมของ required module ในแต่ละ candidate
- companion interpreter ที่ถูกเลือก
- raw health error type, errno และ message
- model identity และ cache-only policy
- diagnosis ที่จัดประเภทแล้วและ next action ที่แน่นอน

`companion_warm=false` เพียงอย่างเดียวไม่เคยพิสูจน์ว่า package หายหรือ server หยุดทำงาน `localhost_permission_denied` ต้องใช้ probe เดิมนอก sandbox หรือพร้อม localhost permission ห้ามติดตั้ง package, เปลี่ยน interpreter หรือ restart จาก diagnosis นี้

## 14. Unified search ใน knowledge store ทั้งสองแบบ

`query` ปกติจะค้นทั้ง `knowledge` ที่มาจาก Markdown และ current head ของ SQLite-native `memory_records` เว้นแต่ filter จะเจาะจง Markdown

แต่ละ store สร้าง ranked list ของตนเองก่อน จากนั้น RRF ชั้นที่สองจะเพิ่ม `1.0 / (60 + rank)` ต่อ store SQLite-native current truth ชนะเมื่อ tie แบบตรงกันพอดี จากนั้นใช้ stable identifier เพื่อจัดลำดับแบบ deterministic

Native result มีเครื่องหมาย:

- `source_path = SQLite:memory_records`
- stable record ID เป็น source heading
- `match_reasons = [sqlite_native, current_truth]`

Markdown result ยังคง source file และ line provenance ไว้ ผู้ใช้ต้องไม่ถือว่า ID ทั้งสอง domain ใช้แทนกันได้

## 15. ความหมายของการอ่านตาม lifecycle

การอ่าน native record ใด ๆ จะ resolve materialized component root และคืนค่า:

- `current_record_ids`
- `is_current`
- `effective_status`
- `has_ambiguous_current`
- `conflicts_with`
- `has_unresolved_conflict`

`record-show` ขยาย relation แบบ breadth-first ได้ถึง depth ที่ร้องขอ แต่มี hard cap 1,000 record SQL `IN` clause ถูกแบ่ง batch (โดยปกติ 500 parameter) เพื่อให้ relation context ขนาดใหญ่อยู่ต่ำกว่าขีดจำกัดเชิงปฏิบัติของ SQLite

`record-search --current-only` อาจ match historical record ด้วยคำเก่า resolve ไปยัง current head และคืน head แม้ content ใหม่จะไม่ใช้ถ้อยคำเดิมซ้ำ

`doctor` ตรวจสอบ lifecycle graph ใน O(records + relations) ด้วยอัลกอริทึมของ Kahn แทนการ enumerate transitive closure

## 16. หน้าที่ของ trigger และ hook

ห้ามสับสนกลไกเหล่านี้:

| กลไก | ทำงานที่ไหน | หน้าที่ |
|---|---|---|
| Git pre-commit hook | นอก SQLite | เรียก sync แบบ advisory สำหรับ Markdown ที่ Git ติดตามและเปลี่ยนแปลง |
| `sync_tracked.py` | Python process | ค้นหาและ ingest Markdown ที่ตรวจทานแล้วอย่างชัดเจน |
| knowledge FTS triggers | SQLite | mirror chunk ที่ insert/update/delete ไปยัง FTS index สามชุด |
| native FTS triggers | SQLite | mirror native record ไปยัง `memory_records_fts` |
| relation validation trigger | SQLite | บังคับ typed edge, current endpoint และห้าม cycle |
| lifecycle union triggers | SQLite | ดูแล materialized current-head component |

ไม่มี SQLite trigger ตัวใด monitor filesystem การแก้ Markdown ที่ยังไม่ commit ต้อง sync อย่างชัดเจน

## 17. Concurrency และ transaction boundary

รองรับ concurrency ของหลาย local process บน Mac เครื่องเดียว:

- WAL อนุญาตให้ reader ทำงานขณะที่ writer active
- Busy timeout รองรับ writer ที่ทับซ้อนกันช่วงสั้น ๆ
- Foreign key ปกป้องความถูกต้องของ endpoint
- `BEGIN IMMEDIATE` serialize migration, relation write และ checkpoint sequence allocation ในจุดที่ deferred transaction จะเกิด race
- Startup lock serialize MiniLM companion ภายนอก

ห้ามเขียนฐานข้อมูลพร้อมกันจากหลาย host ผ่านบริการ sync ไฟล์ WAL เป็นกลไก concurrency ภายในเครื่อง ไม่ใช่ distributed coordination ให้เก็บฐานข้อมูลที่เขียนได้ไว้บน host เดียว และใช้ authenticated write gateway สำหรับ mutation จากระยะไกล

## 18. กลยุทธ์รองรับขนาด

การออกแบบปัจจุบันตั้งใจให้ยังตอบสนองได้เมื่อมี record อย่างน้อยหลายแสนรายการ:

- FTS5 ให้ lexical retrieval ผ่าน inverted index
- project/category/status/bug/module index จำกัดงานตาม scope
- จำกัด candidate list ก่อน Python reranking
- SQL operation แบ่ง batch สำหรับ ID จำนวนมาก
- materialize lifecycle current head ด้วย union-by-size
- relation health เป็น O(N+E)
- read-only command หลีกเลี่ยง migration/write-lock
- exact semantic scan หยุดเมื่อมี embedded row ใน scope เกิน 20,000
- เหนือ threshold งาน semantic ถูกจำกัดด้วย lexical candidate
- สถิติ embedding ดึงเฉพาะ BLOB length ไม่ดึง vector bytes ทั้งหมด

Lexical FTS ยังคงเป็น retrieval path ที่สมบูรณ์ในทุกขนาด Semantic retrieval ตั้งใจยอมเสีย recall แบบไม่มี keyword overlap เมื่อเกิน threshold แทนการให้ query memory/time แปรผันตาม vector ที่เก็บทั้งหมด

## 19. Failure และ degradation model

| Failure | พฤติกรรม |
|---|---|
| MiniLM package หรือ cache ไม่มี | lexical ingest/search ทำงานต่อ |
| companion ใช้งานไม่ได้ | ข้าม semantic pass และคืน structured reason |
| sandbox ปฏิเสธ localhost | วินิจฉัย permission boundary; ไม่เปลี่ยน environment |
| service ที่ไม่เข้ากันอยู่บน port 8770 | ปฏิเสธ identity; ไม่ spawn ทับ |
| embedding stale/malformed | ข้าม vector และรายงาน pending |
| Markdown เปลี่ยน | แทนที่ chunk ของเอกสารนั้นแบบ atomic |
| Markdown ไม่เปลี่ยน | ไม่มี chunk ซ้ำ |
| relation/branch/cycle ไม่ถูกต้อง | transaction abort |
| checkpoint writer ชนกัน | writer ตัวที่สองรอที่ `BEGIN IMMEDIATE` |
| FTS drift | `doctor` รายงาน identity ที่หาย/เกินจริงจาก inverted index |
| migration ล้มเหลว | rollback ทั้งหมด; schema version เก่ายังคงเป็นแหล่งอ้างอิงหลัก |
| เขียนพร้อมกันข้าม host ผ่าน shared folder | ไม่รองรับ; เสี่ยง conflict/corruption |

## 20. ขั้นตอนการทำงานของ agent

เมื่อเริ่มงานที่ไม่ใช่งานเล็กน้อย:

1. รัน `handoff --project <PROJECT> --json`
2. ทำ session ที่เกี่ยวข้องต่อ หรือสร้าง session ใหม่
3. รัน `embed-backfill` หนึ่งครั้ง; หากล้มเหลวไม่ถือเป็นตัวขวางงาน
4. หาก embedding ล้มเหลว ให้รัน `embed-diagnose` ก่อนสรุปเรื่อง environment
5. Query memory ก่อนค้นคว้างานเดิมใหม่
6. เปิด source line ของ Markdown ที่อ้างถึงก่อนตัดสินใจความเสี่ยงสูง

ระหว่างทำงาน:

1. เขียนความรู้ของโครงการลงใน Markdown ที่อ่านได้โดยมนุษย์ให้เหมาะสม
2. รัน `sync_tracked.py` หลังแก้เอกสารความรู้ที่ Git ติดตาม
3. เขียน native audit/fix/verification record โดยตรงเมื่อจำเป็นต้องมี stable internal reference
4. ทำ checkpoint หลังแต่ละช่วงสำคัญ และก่อน compaction/agent switch

หลังทำงาน:

1. เพิ่ม verification ใน native lifecycle เมื่อ audit ได้รับการแก้ไข
2. รัน targeted tests และ `doctor`
