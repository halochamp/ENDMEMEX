from __future__ import annotations

import argparse
import json
import io
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import endeavor_db as db
import sync_tracked


class EndeavorDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.database = self.root / "test.sqlite3"
        self.conn = db.connect(self.database)
        db.initialize(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_pending_work_procedure_has_one_readme_source_of_truth(self):
        agents = (db.ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertTrue((db.HERE / "AGENTS.md").is_file())
        readme = (db.HERE / "README.md").read_text(encoding="utf-8")
        self.assertIn("Inspecting all pending work", agents)
        self.assertNotIn("pending --all-projects --json", agents)
        self.assertIn("### Inspecting all pending work — mandatory procedure", readme)
        self.assertLess(
            readme.index("pending --all-projects --json"),
            readme.index("handoff --all-paused --json"),
        )
        self.assertIn("one or more paused sessions", readme)

    def test_initialize_safely_replays_against_production_database_copy(self):
        source = db.DEFAULT_DB
        if not source.exists():
            self.skipTest("production ENDMEMEX database is unavailable")
        copied = self.root / "production-copy.sqlite3"
        shutil.copy2(source, copied)
        conn = db.connect(copied)
        try:
            before = {name: db.table_count(conn, name) for name in ("sessions", "checkpoints", "knowledge")}
            db.initialize(conn, force=True)
            after = {name: db.table_count(conn, name) for name in before}
            self.assertEqual(db.database_schema_version(conn), db.SCHEMA_VERSION)
            self.assertEqual(after, before)
        finally:
            conn.close()

    def test_ingest_is_idempotent_and_search_has_provenance(self):
        source = self.root / "memory.md"
        source.write_text("# Project\n\n## Bug Fix\n\nFix cache race with one lock.\n", encoding="utf-8")
        first = db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        second = db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        self.assertEqual(first["status"], "imported")
        self.assertEqual(second["status"], "unchanged")
        results = db.search(self.conn, "cache race", "demo", None, 5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source_path"], str(source.resolve()))
        self.assertGreaterEqual(results[0]["line_start"], 1)

    def test_discover_knowledge_docs_keeps_audits_and_includes_reviewed_prototypes(self):
        active_audit = self.root / "ENDEAVOR_VOX" / "developer" / "vox_audit.md"
        active_audit.parent.mkdir(parents=True)
        active_audit.write_text("# Audit\n\nFixed an issue.\n", encoding="utf-8")
        bug_report = self.root / "ENDEAVOR_LOCAL_AGENT_MAX" / "developer" / "bug_report.md"
        bug_report.parent.mkdir(parents=True)
        bug_report.write_text("# Bug\n\nRoot cause.\n", encoding="utf-8")
        archived = self.root / "PROTOTYPE" / "ENDEAVOR_CORE" / "bug_report.md"
        archived.parent.mkdir(parents=True)
        archived.write_text("# Old bug\n", encoding="utf-8")
        baseline = self.root / "ENDEAVOR_VOX" / "developer" / "prompt_baseline_react_full.md"
        baseline.write_text("# Snapshot\n", encoding="utf-8")
        library = self.root / "ENDEAVOR_VOX" / "library" / "book.md"
        library.parent.mkdir(parents=True)
        library.write_text("# Book\n", encoding="utf-8")
        memory_design = self.root / "ENDMEMEX" / "developer" / "DESIGN.md"
        memory_design.parent.mkdir(parents=True)
        memory_design.write_text("# ENDEAVOR Memory Design\n", encoding="utf-8")
        rag_api_memory = self.root / "ENDEAVOR_AGENT_API_MAX" / "ENDEAVOR_RAG_API" / "PROJECT_MEMORY.md"
        rag_api_memory.parent.mkdir(parents=True)
        rag_api_memory.write_text("# RAG API Memory\n", encoding="utf-8")

        docs = sync_tracked.discover_knowledge_docs(
            self.root,
            tracked_paths=[
                "ENDEAVOR_VOX/developer/vox_audit.md",
                "ENDEAVOR_LOCAL_AGENT_MAX/developer/bug_report.md",
                "PROTOTYPE/ENDEAVOR_CORE/bug_report.md",
                "ENDEAVOR_VOX/developer/prompt_baseline_react_full.md",
                "ENDEAVOR_VOX/library/book.md",
                "ENDMEMEX/developer/DESIGN.md",
                "ENDEAVOR_AGENT_API_MAX/ENDEAVOR_RAG_API/PROJECT_MEMORY.md",
            ],
        )

        self.assertEqual(docs["ENDEAVOR_VOX/developer/vox_audit.md"], ("ENDEAVOR_VOX", "audit"))
        self.assertEqual(docs["ENDEAVOR_LOCAL_AGENT_MAX/developer/bug_report.md"], ("ENDEAVOR_LOCAL_AGENT_MAX", "audit"))
        self.assertEqual(
            docs["PROTOTYPE/ENDEAVOR_CORE/bug_report.md"],
            ("PROTOTYPE_ENDEAVOR_CORE", "audit"),
        )
        self.assertNotIn("ENDEAVOR_VOX/developer/prompt_baseline_react_full.md", docs)
        self.assertNotIn("ENDEAVOR_VOX/library/book.md", docs)
        self.assertEqual(
            docs["ENDMEMEX/developer/DESIGN.md"],
            ("ENDMEMEX", "project_memory"),
        )
        self.assertEqual(
            docs["ENDEAVOR_AGENT_API_MAX/ENDEAVOR_RAG_API/PROJECT_MEMORY.md"],
            ("ENDEAVOR_RAG_API", "project_memory"),
        )

    def test_discover_knowledge_docs_includes_explicit_active_project_memory(self):
        awake_memory = self.root / "AWAKE" / "PROJECT_MEMORY.md"
        awake_memory.parent.mkdir(parents=True)
        awake_memory.write_text("# AWAKE Memory\n", encoding="utf-8")

        with mock.patch.object(sync_tracked, "_git_tracked_markdown", return_value=[]):
            docs = sync_tracked.discover_knowledge_docs(self.root)

        self.assertEqual(docs["AWAKE/PROJECT_MEMORY.md"], ("AWAKE", "project_memory"))

    def test_sync_tracked_labels_standalone_public_markdown_as_endmemex(self):
        self.assertEqual(
            sync_tracked._project_for("developer/audit.md", standalone=True), "ENDMEMEX"
        )

    def test_prune_documents_only_removes_sources_outside_manifest(self):
        kept = self.root / "kept.md"
        stale = self.root / "stale.md"
        kept.write_text("# Kept\n\nKnowledge.\n", encoding="utf-8")
        stale.write_text("# Stale\n\nOld knowledge.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, kept, "demo", "project_memory", embed=False)
        db.ingest_markdown(self.conn, stale, "demo", "project_memory", embed=False)
        self.assertEqual(db.prune_documents(self.conn, {str(kept.resolve())}), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)
        self.assertTrue(db.search(self.conn, "Knowledge", "demo", None, 5))
        self.assertFalse(db.search(self.conn, "Stale", "demo", None, 5))

    def test_freshness_report_detects_stale_missing_metadata_and_orphaned_sources(self):
        tracked = self.root / "tracked.md"
        orphan = self.root / "orphan.md"
        tracked.write_text("# Tracked\n\nCurrent text.\n", encoding="utf-8")
        orphan.write_text("# Orphan\n\nOld text.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, tracked, "demo", "project_memory", embed=False)
        db.ingest_markdown(self.conn, orphan, "demo", "project_memory", embed=False)
        docs = {"tracked.md": ("demo", "project_memory")}
        report = sync_tracked.freshness_report(self.root, self.conn, docs)
        self.assertEqual(report["current"], ["tracked.md"])
        self.assertEqual(report["orphaned"], [str(orphan.resolve())])

        tracked.write_text("# Tracked\n\nChanged text.\n", encoding="utf-8")
        self.assertEqual(sync_tracked.freshness_report(self.root, self.conn, docs)["stale"], ["tracked.md"])

        db.ingest_markdown(self.conn, tracked, "demo", "project_memory", embed=False)
        self.conn.execute("UPDATE documents SET kind = 'audit' WHERE source_path = ?", (str(tracked.resolve()),))
        self.conn.commit()
        self.assertEqual(
            sync_tracked.freshness_report(self.root, self.conn, docs)["metadata_mismatch"], ["tracked.md"]
        )

        self.conn.execute("DELETE FROM documents WHERE source_path = ?", (str(tracked.resolve()),))
        self.conn.commit()
        self.assertEqual(sync_tracked.freshness_report(self.root, self.conn, docs)["missing"], ["tracked.md"])

    def test_freshness_report_exposes_an_interrupted_sync_as_missing(self):
        first = self.root / "first.md"
        second = self.root / "second.md"
        first.write_text("# First\n\nIndexed.\n", encoding="utf-8")
        second.write_text("# Second\n\nNot yet indexed.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, first, "demo", "project_memory", embed=False)
        report = sync_tracked.freshness_report(self.root, self.conn, {
            "first.md": ("demo", "project_memory"),
            "second.md": ("demo", "project_memory"),
        })
        self.assertEqual(report["current"], ["first.md"])
        self.assertEqual(report["missing"], ["second.md"])

    def test_prune_proposal_requires_reviewed_hash_identical_orphans(self):
        tracked = self.root / "tracked.md"
        orphan = self.root / "deleted.md"
        tracked.write_text("# Tracked\n\nKeep.\n", encoding="utf-8")
        orphan.write_text("# Deleted\n\nArchived snapshot.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, tracked, "demo", "project_memory", embed=False)
        db.ingest_markdown(self.conn, orphan, "demo", "project_memory", embed=False)
        docs = {"tracked.md": ("demo", "project_memory")}

        proposal = sync_tracked.build_prune_proposal(self.root, self.conn, docs)
        self.assertTrue(proposal["review_required"])
        self.assertEqual(proposal["orphan_count"], 1)
        self.assertEqual(proposal["entries"][0]["source_path"], str(orphan.resolve()))

        changed = json.loads(json.dumps(proposal))
        changed["entries"][0]["content_hash"] = "changed-after-review"
        with self.assertRaisesRegex(ValueError, "changed since review"):
            sync_tracked.apply_prune_proposal(self.root, self.conn, changed, docs)
        self.assertEqual(sync_tracked.apply_prune_proposal(self.root, self.conn, proposal, docs), 1)
        self.assertEqual(sync_tracked.freshness_report(self.root, self.conn, docs)["orphaned"], [])

    def test_prune_proposal_refuses_a_target_that_became_tracked_again(self):
        orphan = self.root / "restored.md"
        orphan.write_text("# Restored\n\nKnowledge.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, orphan, "demo", "project_memory", embed=False)
        proposal = sync_tracked.build_prune_proposal(self.root, self.conn, {})
        with self.assertRaisesRegex(ValueError, "tracked again"):
            sync_tracked.apply_prune_proposal(
                self.root, self.conn, proposal,
                {"restored.md": ("demo", "project_memory")},
            )

    def test_check_unknown_requested_path_is_a_missing_failure(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(sync_tracked, "discover_knowledge_docs", return_value={}), mock.patch.object(
            db, "database_path", return_value=self.database
        ), redirect_stdout(stdout), mock.patch("sys.stderr", stderr):
            exit_code = sync_tracked.main(["--check", "--json", "deleted.md"])

        report = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(report["ok"])
        self.assertEqual(report["checked"], 1)
        self.assertEqual(report["missing"], ["deleted.md"])
        self.assertIn("not in TRACKED_DOCS", stderr.getvalue())

    def test_sync_only_embeds_synchronously_when_companion_is_already_warm(self):
        tracked = self.root / "tracked.md"
        tracked.write_text("# Tracked\n\nBody.\n", encoding="utf-8")
        fake_result = mock.Mock(returncode=0, stdout="{}", stderr="")

        with mock.patch.object(
            sync_tracked, "discover_knowledge_docs", return_value={"tracked.md": ("demo", "project_memory")}
        ), mock.patch.object(sync_tracked, "ROOT", self.root), mock.patch.object(
            db, "embed_companion_ready", return_value=True
        ), mock.patch.object(sync_tracked.subprocess, "run", return_value=fake_result) as warm_run, redirect_stdout(
            io.StringIO()
        ):
            exit_code = sync_tracked.main(["tracked.md"])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("--no-embed", warm_run.call_args[0][0])

        with mock.patch.object(
            sync_tracked, "discover_knowledge_docs", return_value={"tracked.md": ("demo", "project_memory")}
        ), mock.patch.object(sync_tracked, "ROOT", self.root), mock.patch.object(
            db, "embed_companion_ready", return_value=False
        ), mock.patch.object(sync_tracked.subprocess, "run", return_value=fake_result) as cold_run, redirect_stdout(
            io.StringIO()
        ):
            exit_code = sync_tracked.main(["tracked.md"])
        self.assertEqual(exit_code, 0)
        self.assertIn("--no-embed", cold_run.call_args[0][0])

    def test_reingest_replaces_old_knowledge(self):
        source = self.root / "memory.md"
        source.write_text("# Memory\n\nobsolete zebra\n", encoding="utf-8")
        db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        source.write_text("# Memory\n\nfresh yak\n", encoding="utf-8")
        db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        self.assertFalse(db.search(self.conn, "obsolete zebra", "demo", None, 5))
        self.assertTrue(db.search(self.conn, "fresh yak", "demo", None, 5))

    def test_reingest_same_content_updates_project_and_kind(self):
        source = self.root / "shared.md"
        source.write_text("# Shared\n\nReusable note.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, source, "project-a", "project_memory", embed=False)
        outcome = db.ingest_markdown(self.conn, source, "project-b", "training_guide", embed=False)
        self.assertEqual(outcome["status"], "imported")
        document = self.conn.execute("SELECT project, kind FROM documents").fetchone()
        knowledge = self.conn.execute("SELECT project, category FROM knowledge").fetchone()
        self.assertEqual(tuple(document), ("project-b", "training_guide"))
        self.assertEqual(tuple(knowledge), ("project-b", "agent_training"))

    def test_large_single_paragraph_is_hard_split_at_chunk_limit(self):
        chunks = db.markdown_chunks("# Large\n\n" + "x" * (db.MAX_CHUNK_CHARS * 2 + 1), "large")
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk.content) <= db.MAX_CHUNK_CHARS for chunk in chunks))

    def test_large_thai_paragraph_never_starts_a_chunk_with_a_combining_mark(self):
        text = "ก" * db.MAX_CHUNK_CHARS + "่" + "ข" * db.MAX_CHUNK_CHARS
        chunks = db.markdown_chunks("# ไทย\n\n" + text, "thai")
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk.content) <= db.MAX_CHUNK_CHARS for chunk in chunks))
        self.assertTrue(all(not unicodedata.category(chunk.content[0]).startswith("M") for chunk in chunks))
        self.assertIn("ก่", "".join(chunk.content for chunk in chunks))

    def test_ingested_knowledge_never_exceeds_embedding_chunk_limit(self):
        source = self.root / "oversized.md"
        source.write_text("# Large\n\n" + "x" * (db.MAX_CHUNK_CHARS * 2 + 1), encoding="utf-8")
        db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        longest = self.conn.execute("SELECT MAX(length(content)) FROM knowledge").fetchone()[0]
        self.assertLessEqual(longest, db.MAX_CHUNK_CHARS)

    def test_search_falls_back_to_broad_terms_and_matches_english_prefix(self):
        source = self.root / "guide.md"
        source.write_text("# Training\n\nAgent training requires one change per round.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, source, "guide", "training_guide", embed=False)
        self.assertTrue(db.search(self.conn, "train agent", "guide", None, 5))
        self.assertTrue(db.search(self.conn, "วิธี agent", "guide", None, 5))
        self.assertTrue(db.search(self.conn, "วิธีเทรน agent", "guide", None, 5))

    def test_heading_only_rows_are_skipped_but_parent_context_is_preserved(self):
        source = self.root / "hierarchy.md"
        source.write_text("# Parent\n\n## Child\n\nUseful implementation detail.\n", encoding="utf-8")
        outcome = db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        self.assertEqual(outcome["entries"], 1)
        row = self.conn.execute("SELECT title, parent_heading FROM knowledge").fetchone()
        self.assertEqual(row["title"], "Parent > Child")
        self.assertEqual(row["parent_heading"], "Parent")

    def test_chunk_provenance_skips_blank_lines_after_heading(self):
        chunks = db.markdown_chunks("# Heading\n\n\nactual body\n", "fallback")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].content, "actual body")
        self.assertEqual((chunks[0].line_start, chunks[0].line_end), (4, 4))

    def test_chunk_provenance_ends_on_content_not_separator_blank_line(self):
        original_limit = db.MAX_CHUNK_CHARS
        db.MAX_CHUNK_CHARS = 70
        try:
            chunks = db.markdown_chunks(
                "# Heading\n\n" + "a" * 60 + "\n\n" + "b" * 60 + "\n",
                "fallback",
            )
        finally:
            db.MAX_CHUNK_CHARS = original_limit
        self.assertEqual(len(chunks), 2)
        self.assertEqual((chunks[0].line_start, chunks[0].line_end), (3, 3))
        self.assertEqual((chunks[1].line_start, chunks[1].line_end), (5, 5))

    def test_metadata_filters_and_trigram_substring_retrieval(self):
        source = self.root / "bugs.md"
        source.write_text(
            "# Session 12 — V2-DB01 cache bug\n\n**RESOLVED** in `tools/cache.py`. MemorySaver prevents stale cache.\n",
            encoding="utf-8",
        )
        db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        results = db.search(self.conn, "DB01", "demo", None, 5, bug_id="V2-DB01", status="resolved")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["bug_id"], "V2-DB01")
        self.assertEqual(results[0]["module"], "tools/cache.py")
        substring = db.search(self.conn, "orySaver", "demo", None, 5)
        self.assertTrue(substring)
        self.assertIn("trigram", substring[0]["match_reasons"])

    def test_thai_combining_marks_and_identifier_separators_are_searchable(self):
        source = self.root / "thai-search.md"
        source.write_text(
            "# วิธีแก้บั๊ก MemorySaver\n\n"
            "MemorySaver stores the cache safely; วิธีแก้บั๊กต้องรักษาวรรณยุกต์ไทย.\n",
            encoding="utf-8",
        )
        db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        thai = db.search(self.conn, "วิธีแก้บั๊ก", "demo", None, 5, semantic="off")
        self.assertTrue(thai)
        self.assertIn("all_terms", thai[0]["match_reasons"])
        for variant in ("MemorySaver", "memory saver", "memory_saver", "memory-saver"):
            self.assertTrue(db.search(self.conn, variant, "demo", None, 5, semantic="off"), variant)

    def test_native_records_have_porter_and_bounded_trigram_fallback(self):
        record_id = "KNOWLEDGE-FTS-009"
        db.create_memory_record(
            self.conn, record_id, "demo", "knowledge", "Native MemorySaver",
            "MemorySaver prevents cache corruption", "current", "codex",
        )
        substring = db.search_memory_records(self.conn, "orySaver", project="demo")
        self.assertEqual([row["id"] for row in substring], [record_id])
        self.assertEqual(db.search_memory_records(self.conn, "or", project="demo"), [])
        self.assertTrue(db.search_memory_records(self.conn, "MemorySaver", project="demo"))
        typo_source = db.create_memory_record(
            self.conn, "KNOWLEDGE-FTS-TYPO", "demo", "knowledge", "Native detection",
            "A silent failure must be detected.", "current", "codex",
        )
        self.assertIn(
            typo_source,
            [row["id"] for row in db.search_memory_records(self.conn, "falure", project="demo")],
        )

    def test_v9_fts_tables_keep_thai_marks_and_term_vocab(self):
        sql = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_fts'"
        ).fetchone()[0]
        self.assertIn("Co Mn", sql)
        self.assertTrue(db.table_exists(self.conn, "knowledge_fts_terms"))
        self.assertTrue(db.table_exists(self.conn, "memory_records_fts_terms"))

    def test_ascii_typo_fallback_is_unambiguous_and_has_provenance(self):
        source = self.root / "typo.md"
        source.write_text("# Detection\n\nA silent failure must be detected.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        results = db.search(self.conn, "silent falure", "demo", None, 5, semantic="off")
        self.assertTrue(results)
        self.assertIn("typo", results[0]["match_reasons"])
        self.assertIn("typo:falure→failure:d=1", results[0]["match_reasons"])
        self.assertEqual(db._typo_corrected_terms(self.conn, ["a12bc"], "knowledge_fts_terms"), (["a12bc"], []))
        self.assertEqual(
            db._typo_corrected_terms(self.conn, ["failure"], "knowledge_fts_terms"),
            (["failure"], []),
        )

    def test_metadata_status_uses_whole_words_and_preserves_precedence(self):
        self.assertEqual(
            db.extract_metadata("Integration", "OpenAI integration fixed successfully.", "architecture")["status"],
            "resolved",
        )
        self.assertEqual(
            db.extract_metadata("Result", "Behavior varies depending on config; fixed now.", "debugging")["status"],
            "resolved",
        )
        self.assertEqual(
            db.extract_metadata("O-27 opened then ACCEPTED", "No implementation change.", "debugging")["status"],
            "accepted",
        )
        self.assertEqual(db.extract_metadata("Issue", "Still open.", "debugging")["status"], "open")

    def test_metadata_filters_treat_wildcards_literally_and_bug_ids_exactly(self):
        first = self.root / "first.md"
        first.write_text("# V2-DB01\n\nneedle fixed in `a.py`.\n", encoding="utf-8")
        second = self.root / "second.md"
        second.write_text("# V2-DB010\n\nneedle fixed in `b.py`.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, first, "demo", "project_memory", embed=False)
        db.ingest_markdown(self.conn, second, "demo", "project_memory", embed=False)

        self.assertEqual(db.search(self.conn, "missing", "demo", None, 5, module="%", semantic="off"), [])
        exact = db.search(self.conn, "needle", "demo", None, 5, bug_id="V2-DB01", semantic="off")
        self.assertEqual([row["bug_id"] for row in exact], ["V2-DB01"])

    def test_metadata_rescue_respects_every_requested_scope(self):
        source = self.root / "filters.md"
        source.write_text(
            "# Resolved\n\nV2-FILTER1 fixed needle.\n\n# Open\n\nV2-FILTER1 not fixed needle.\n",
            encoding="utf-8",
        )
        db.ingest_markdown(self.conn, source, "wanted", "project_memory", embed=False)
        other = self.root / "other.md"
        other.write_text("# Other\n\nV2-FILTER1 fixed needle.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, other, "unwanted", "project_memory", embed=False)
        results = db.search(self.conn, "needle", "wanted", None, 10, bug_id="V2-FILTER1", status="resolved")
        self.assertEqual([(row["project"], row["status"]) for row in results], [("wanted", "resolved")])

    def test_module_filter_matches_all_documented_modules(self):
        source = self.root / "modules.md"
        source.write_text("# Modules\n\nFixed in `a.py` and `b.py`: module needle.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        results = db.search(self.conn, "needle", "demo", None, 5, module="b.py")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["module"], "a.py")

    def test_diversity_caps_results_from_one_parent_heading(self):
        source = self.root / "diverse.md"
        source.write_text(
            "# One Parent\n\n## A\n\ncache bug fix alpha\n\n## B\n\ncache bug fix beta\n\n## C\n\ncache bug fix gamma\n\n# Other Parent\n\ncache bug fix delta\n",
            encoding="utf-8",
        )
        db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        results = db.search(self.conn, "cache bug fix", "demo", None, 5)
        one_parent = [row for row in results if row["source_heading"].startswith("One Parent")]
        self.assertLessEqual(len(one_parent), 2)

    def test_diversity_does_not_merge_same_parent_name_across_documents(self):
        for filename, children in (
            ("first.md", (("Apple", "apple"), ("Banana", "banana"))),
            ("second.md", (("Cat", "cat"), ("Dog", "dog"))),
        ):
            body = "# Role\n\n" + "\n\n".join(
                f"## {heading}\n\nunique needle {word}" for heading, word in children
            ) + "\n"
            source = self.root / filename
            source.write_text(body, encoding="utf-8")
            db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)

        results = db.search(self.conn, "unique needle", "demo", None, 10, semantic="off")
        self.assertEqual(len(results), 4)
        self.assertEqual({Path(row["source_path"]).name for row in results}, {"first.md", "second.md"})

    def test_evaluation_and_feedback(self):
        source = self.root / "guide.md"
        source.write_text("# Training\n\nSilent failure needs a concrete retry example.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, source, "guide", "training_guide", embed=False)
        cases = self.root / "eval.json"
        cases.write_text(json.dumps([{
            "query": "silent failure", "expected_project": "guide", "expected_category": "agent_training"
        }]), encoding="utf-8")
        report = db.evaluate_queries(self.conn, cases)
        self.assertEqual(report["recall_at_5"], 1.0)
        self.assertEqual(report["pipeline"], "unified")
        self.assertEqual(report["details"][0]["source_mix"], {"markdown": 1})
        feedback_id = db.record_feedback(self.conn, "codex", "silent failure", [1], True, "used source")
        self.assertEqual(feedback_id, 1)

    def test_evaluation_can_compare_markdown_with_production_unified_retrieval(self):
        db.create_memory_record(
            self.conn, "AUDIT-EVAL-NATIVE", "demo", "audit", "Needle only in SQLite",
            "quasarzero native regression evidence", "open", "codex",
        )
        cases = self.root / "unified-eval.json"
        cases.write_text(json.dumps([{
            "query": "quasarzero", "expected_source_kind": "sqlite",
            "expected_project": "demo",
        }]), encoding="utf-8")

        markdown = db.evaluate_queries(self.conn, cases, semantic="off", pipeline="markdown")
        unified = db.evaluate_queries(self.conn, cases, semantic="off", pipeline="unified")

        self.assertEqual(markdown["recall_at_5"], 0.0)
        self.assertEqual(unified["recall_at_5"], 1.0)
        self.assertEqual(unified["details"][0]["source_mix"], {"sqlite": 1})

    def test_ann_restores_zero_keyword_overlap_above_exact_scan_threshold(self):
        source = self.root / "ann.md"
        source.write_text("# Concept\n\nsemantic-only stored wording\n", encoding="utf-8")
        db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        row = self.conn.execute("SELECT id, content FROM knowledge").fetchone()
        vector = [0.0] * db.EMBED_DIM
        vector[0] = 1.0
        self.conn.execute(
            "UPDATE knowledge SET embedding = ?, embedding_hash = ? WHERE id = ?",
            (db.pack_embedding(vector), db.embedding_hash(row["content"]), row["id"]),
        )
        self.conn.commit()
        original_threshold = db.SEMANTIC_FULL_SCAN_LIMIT
        db.SEMANTIC_FULL_SCAN_LIMIT = 0
        try:
            with mock.patch.object(db, "embed_texts", return_value=[vector]), mock.patch.object(
                db, "ann_helper", return_value={"available": True, "fresh": True}
            ), mock.patch.object(db, "ann_candidate_ids", return_value=[row["id"]]) as ann:
                results = db.search(
                    self.conn, "totally different paraphrase", "demo", None, 5,
                    semantic="ready",
                )
        finally:
            db.SEMANTIC_FULL_SCAN_LIMIT = original_threshold
        self.assertEqual(results[0]["id"], row["id"])
        self.assertIn("semantic", results[0]["match_reasons"])
        ann.assert_called_once()

    def test_durable_events_are_deduplicated_ordered_and_acknowledged(self):
        first = db.publish_event(
            self.conn, "delegation.completed", "demo", "run-1",
            {"status": "completed"}, "delegate:run-1:terminal", "codex",
        )
        duplicate = db.publish_event(
            self.conn, "delegation.completed", "demo", "run-1",
            {"status": "changed payload is ignored"}, "delegate:run-1:terminal", "codex",
        )
        second = db.publish_event(
            self.conn, "session.closed", "other", "session-2",
            {"status": "completed"}, "session:2:closed", "claude",
        )
        self.assertEqual(first["id"], duplicate["id"])
        self.assertEqual(duplicate["payload"], {"status": "completed"})
        self.assertEqual([event["id"] for event in db.poll_events(self.conn)], [first["id"], second["id"]])
        acknowledged = db.acknowledge_event(self.conn, first["id"], "codex")
        self.assertEqual(acknowledged["acknowledged_by"], "codex")
        self.assertEqual([event["id"] for event in db.poll_events(self.conn)], [second["id"]])
        self.assertEqual(
            [event["id"] for event in db.poll_events(self.conn, project="demo", include_acked=True)],
            [first["id"]],
        )

    def test_v1_database_migrates_without_losing_knowledge(self):
        self.conn.close()
        self.database.unlink()
        legacy = sqlite3.connect(self.database)
        legacy.executescript("""
            CREATE TABLE database_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO database_meta VALUES ('schema_version', '1', 'old');
            CREATE TABLE documents (id INTEGER PRIMARY KEY, source_path TEXT NOT NULL UNIQUE, title TEXT NOT NULL, kind TEXT NOT NULL, project TEXT NOT NULL, content_hash TEXT NOT NULL, source_mtime REAL, imported_at TEXT NOT NULL);
            CREATE TABLE knowledge (id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL, project TEXT NOT NULL, category TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL, tags TEXT NOT NULL DEFAULT '[]', source_path TEXT NOT NULL, source_heading TEXT, source_line_start INTEGER, source_line_end INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO documents VALUES (1, 'legacy.md', 'Legacy', 'project_memory', 'legacy', 'x', 0, 'old');
            INSERT INTO knowledge VALUES (1, 1, 'legacy', 'debugging', 'Legacy bug', 'fixed legacy cache bug', '[]', 'legacy.md', 'Legacy', 1, 1, 'old', 'old');
        """)
        legacy.commit()
        legacy.close()
        self.conn = db.connect(self.database)
        db.initialize(self.conn)
        row = self.conn.execute("SELECT status, parent_heading FROM knowledge WHERE id = 1").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(self.conn.execute("SELECT value FROM database_meta WHERE key = 'schema_version'").fetchone()[0], db.SCHEMA_VERSION)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM knowledge_fts_trigram").fetchone()[0], 1)

    def test_sqlite_native_audit_fix_and_verification_lifecycle(self):
        audit_id = db.create_memory_record(
            self.conn, "AUDIT-MEM-001", "demo", "audit", "Reference audit",
            "Current implementation has no internal typed references.", "open", "codex",
        )
        fix_id = db.create_memory_record(
            self.conn, "FIX-MEM-001", "demo", "fix", "Typed references implemented",
            "Added stable records and foreign-key relations.", "current", "codex",
            links=[("resolves", audit_id, "Fix closes the audit finding")],
        )
        verification_id = db.create_memory_record(
            self.conn, "VERIFY-MEM-001", "demo", "verification", "Regression verified",
            "The lifecycle and integrity tests pass.", "current", "codex",
            links=[("verifies", fix_id, "Unit regression")],
        )

        context = db.memory_record_context(self.conn, audit_id, depth=2)
        self.assertEqual(context["record"]["effective_status"], "resolved")
        self.assertFalse(context["record"]["is_current"])
        self.assertEqual(context["record"]["current_record_ids"], [fix_id])
        self.assertEqual(set(context["records"]), {audit_id, fix_id, verification_id})
        self.assertEqual(
            {(edge["source_id"], edge["relation"], edge["target_id"]) for edge in context["relations"]},
            {(fix_id, "resolves", audit_id), (verification_id, "verifies", fix_id)},
        )

    def test_supersedes_chain_points_old_record_to_latest_current_record(self):
        for record_id in ("DECISION-MEM-001", "DECISION-MEM-002", "DECISION-MEM-003"):
            db.create_memory_record(
                self.conn, record_id, "demo", "decision", record_id, f"State from {record_id}",
                "current", "codex",
            )
        db.add_memory_relation(
            self.conn, "DECISION-MEM-002", "supersedes", "DECISION-MEM-001", "newer", "codex"
        )
        db.add_memory_relation(
            self.conn, "DECISION-MEM-003", "supersedes", "DECISION-MEM-002", "newest", "codex"
        )
        old = db.memory_record_context(self.conn, "DECISION-MEM-001", depth=3)["record"]
        middle = db.memory_record_context(self.conn, "DECISION-MEM-002", depth=0)["record"]
        latest = db.memory_record_context(self.conn, "DECISION-MEM-003", depth=0)["record"]
        self.assertEqual(old["current_record_ids"], ["DECISION-MEM-003"])
        self.assertEqual(middle["effective_status"], "superseded")
        self.assertTrue(latest["is_current"])

    def test_parallel_lifecycle_successor_is_rejected_atomically(self):
        db.create_memory_record(
            self.conn, "AUDIT-BRANCH-001", "demo", "audit", "Branch", "One finding", "open", "codex"
        )
        db.create_memory_record(
            self.conn, "FIX-BRANCH-001", "demo", "fix", "First fix", "Selected fix", "current", "codex",
            links=[("resolves", "AUDIT-BRANCH-001", "selected")],
        )
        with self.assertRaisesRegex(ValueError, "target is not current"):
            db.create_memory_record(
                self.conn, "FIX-BRANCH-002", "demo", "fix", "Second fix", "Alternative fix",
                "current", "codex", links=[("resolves", "AUDIT-BRANCH-001", "alternative")],
            )
        self.assertIsNone(self.conn.execute(
            "SELECT id FROM memory_records WHERE id = 'FIX-BRANCH-002'"
        ).fetchone())
        record = db.memory_record_context(self.conn, "AUDIT-BRANCH-001", depth=1)["record"]
        self.assertFalse(record["has_ambiguous_current"])
        self.assertEqual(record["current_record_ids"], ["FIX-BRANCH-001"])
        self.assertEqual(db.memory_relation_health(self.conn)["ambiguous_current_records"], 0)

    def test_existing_lifecycle_relation_is_idempotent_and_updates_note(self):
        db.create_memory_record(
            self.conn, "AUDIT-IDEMPOTENT-001", "demo", "audit", "Audit", "finding", "open", "codex"
        )
        db.create_memory_record(
            self.conn, "FIX-IDEMPOTENT-001", "demo", "fix", "Fix", "implementation", "current", "codex",
            links=[("resolves", "AUDIT-IDEMPOTENT-001", "first")],
        )
        db.add_memory_relation(
            self.conn, "FIX-IDEMPOTENT-001", "resolves", "AUDIT-IDEMPOTENT-001",
            "updated note", "codex",
        )
        rows = self.conn.execute(
            "SELECT note FROM memory_relations WHERE relation = 'resolves'"
        ).fetchall()
        self.assertEqual([row["note"] for row in rows], ["updated note"])
        self.assertEqual(
            db._terminal_current_ids(self.conn, "AUDIT-IDEMPOTENT-001"),
            ["FIX-IDEMPOTENT-001"],
        )

    def test_internal_relations_require_existing_records_and_reject_lifecycle_cycles(self):
        db.create_memory_record(
            self.conn, "AUDIT-CYCLE-001", "demo", "audit", "First", "First state", "open", "codex"
        )
        db.create_memory_record(
            self.conn, "FIX-CYCLE-001", "demo", "fix", "Second", "Second state", "current", "codex"
        )
        with self.assertRaisesRegex(ValueError, "does not exist in SQLite"):
            db.add_memory_relation(
                self.conn, "FIX-CYCLE-001", "references", "MISSING-CYCLE-001", "", "codex"
            )
        with self.assertRaisesRegex(ValueError, "does not exist in SQLite"):
            db.create_memory_record(
                self.conn, "FIX-ATOMIC-001", "demo", "fix", "Atomic", "Must roll back",
                "current", "codex", links=[("resolves", "MISSING-CYCLE-001", "")],
            )
        self.assertIsNone(
            self.conn.execute("SELECT id FROM memory_records WHERE id = 'FIX-ATOMIC-001'").fetchone()
        )
        db.add_memory_relation(
            self.conn, "FIX-CYCLE-001", "resolves", "AUDIT-CYCLE-001", "", "codex"
        )
        with self.assertRaisesRegex(ValueError, "matching record types"):
            db.add_memory_relation(
                self.conn, "AUDIT-CYCLE-001", "supersedes", "FIX-CYCLE-001", "", "codex"
            )

    def test_contradiction_is_visible_until_one_side_is_superseded(self):
        for record_id in ("DECISION-CONFLICT-A", "DECISION-CONFLICT-B", "DECISION-CONFLICT-C"):
            db.create_memory_record(
                self.conn, record_id, "demo", "decision", record_id,
                f"Conflicting position {record_id}", "current", "codex",
            )
        db.add_memory_relation(
            self.conn, "DECISION-CONFLICT-A", "contradicts", "DECISION-CONFLICT-B", "", "codex"
        )
        self.assertTrue(
            db.memory_record_context(self.conn, "DECISION-CONFLICT-A", depth=1)["record"]["has_unresolved_conflict"]
        )
        self.assertEqual(db.memory_relation_health(self.conn)["unresolved_conflicts"], 1)
        db.add_memory_relation(
            self.conn, "DECISION-CONFLICT-C", "supersedes", "DECISION-CONFLICT-B", "", "codex"
        )
        self.assertFalse(
            db.memory_record_context(self.conn, "DECISION-CONFLICT-A", depth=1)["record"]["has_unresolved_conflict"]
        )
        self.assertEqual(db.memory_relation_health(self.conn)["unresolved_conflicts"], 0)

    def test_native_record_search_can_limit_results_to_current_truth(self):
        db.create_memory_record(
            self.conn, "KNOWLEDGE-SEARCH-OLD", "demo", "knowledge", "Camera shutdown",
            "Old camera cleanup uses delayed release.", "current", "codex",
        )
        db.create_memory_record(
            self.conn, "KNOWLEDGE-SEARCH-NEW", "demo", "knowledge", "Camera shutdown current",
            "Current camera cleanup uses synchronous track stop.", "current", "codex",
            links=[("supersedes", "KNOWLEDGE-SEARCH-OLD", "current implementation")],
        )
        all_results = db.search_memory_records(self.conn, "camera cleanup", project="demo")
        current = db.search_memory_records(self.conn, "camera cleanup", project="demo", current_only=True)
        resolved_from_old = db.search_memory_records(
            self.conn, "delayed release", project="demo", current_only=True
        )
        self.assertEqual({item["id"] for item in all_results}, {"KNOWLEDGE-SEARCH-OLD", "KNOWLEDGE-SEARCH-NEW"})
        self.assertEqual([item["id"] for item in current], ["KNOWLEDGE-SEARCH-NEW"])
        self.assertEqual([item["id"] for item in resolved_from_old], ["KNOWLEDGE-SEARCH-NEW"])
        self.assertEqual(resolved_from_old[0]["matched_via_record_id"], "KNOWLEDGE-SEARCH-OLD")

    def test_native_search_uses_real_or_fallback(self):
        db.create_memory_record(
            self.conn, "KNOWLEDGE-FALLBACK-001", "demo", "knowledge", "Fallback",
            "alpha exists without the second requested term", "current", "codex",
        )
        results = db.search_memory_records(self.conn, "alpha beta")
        self.assertEqual([item["id"] for item in results], ["KNOWLEDGE-FALLBACK-001"])

    def test_current_only_search_reapplies_scope_to_resolved_head(self):
        db.create_memory_record(
            self.conn, "KNOWLEDGE-SCOPE-OLD", "project-a", "knowledge", "Scoped old",
            "scope leak historical needle", "current", "codex",
        )
        db.create_memory_record(
            self.conn, "KNOWLEDGE-SCOPE-NEW", "project-b", "knowledge", "Scoped current",
            "new implementation", "current", "codex",
            links=[("supersedes", "KNOWLEDGE-SCOPE-OLD", "moved project")],
        )
        self.assertEqual(
            db.search_memory_records(
                self.conn, "historical needle", project="project-a", current_only=True
            ),
            [],
        )

    def test_current_only_search_does_not_starve_on_duplicate_history(self):
        previous = None
        for index in range(40):
            record_id = f"KNOWLEDGE-HISTORY-{index:03d}"
            links = [("supersedes", previous, "next") ] if previous else []
            db.create_memory_record(
                self.conn, record_id, "demo", "knowledge", "History needle",
                "shared starvation needle", "current", "codex", links=links,
            )
            previous = record_id
        for index in range(9):
            db.create_memory_record(
                self.conn, f"KNOWLEDGE-INDEPENDENT-{index:03d}", "demo", "knowledge",
                "Independent needle", "shared starvation needle", "current", "codex",
            )
        results = db.search_memory_records(
            self.conn, "starvation needle", project="demo", limit=10, current_only=True
        )
        self.assertEqual(len(results), 10)
        self.assertEqual(len({item["id"] for item in results}), 10)
        self.assertIn(previous, {item["id"] for item in results})

    def test_symmetric_relations_are_canonical_and_unique(self):
        for record_id in ("DECISION-SYMMETRIC-A", "DECISION-SYMMETRIC-B"):
            db.create_memory_record(
                self.conn, record_id, "demo", "decision", record_id,
                "conflicting truth", "current", "codex",
            )
        db.add_memory_relation(
            self.conn, "DECISION-SYMMETRIC-B", "contradicts", "DECISION-SYMMETRIC-A", "first", "codex"
        )
        db.add_memory_relation(
            self.conn, "DECISION-SYMMETRIC-A", "contradicts", "DECISION-SYMMETRIC-B", "updated", "codex"
        )
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM memory_relations WHERE relation = 'contradicts'"
        ).fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO memory_relations(
                       source_id, target_id, relation, note, created_by, created_at
                   ) VALUES(?, ?, 'contradicts', '', 'human', 'now')""",
                ("DECISION-SYMMETRIC-B", "DECISION-SYMMETRIC-A"),
            )

    def test_sql_triggers_enforce_relation_types_and_related_record_type(self):
        db.create_memory_record(
            self.conn, "AUDIT-TYPED-001", "demo", "audit", "Audit", "finding", "open", "codex"
        )
        db.create_memory_record(
            self.conn, "FIX-TYPED-001", "demo", "fix", "Fix", "implementation", "current", "codex"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "fix -> audit"):
            self.conn.execute(
                """INSERT INTO memory_relations(
                       source_id, target_id, relation, note, created_by, created_at
                   ) VALUES('AUDIT-TYPED-001', 'FIX-TYPED-001', 'resolves', '', 'human', 'now')"""
            )
        db.add_memory_relation(
            self.conn, "FIX-TYPED-001", "resolves", "AUDIT-TYPED-001", "", "codex"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "record type is immutable"):
            db.update_memory_record(
                self.conn, "FIX-TYPED-001", "codex", record_type="decision"
            )

    def test_concurrent_lifecycle_writers_cannot_create_a_branch(self):
        db.create_memory_record(
            self.conn, "AUDIT-RACE-001", "demo", "audit", "Race audit", "finding", "open", "codex"
        )
        for record_id in ("FIX-RACE-001", "FIX-RACE-002"):
            db.create_memory_record(
                self.conn, record_id, "demo", "fix", record_id, "candidate", "current", "codex"
            )
        barrier = threading.Barrier(2)
        outcomes = []

        def link(record_id):
            conn = db.connect(self.database)
            try:
                barrier.wait()
                db.add_memory_relation(conn, record_id, "resolves", "AUDIT-RACE-001", "", "codex")
                outcomes.append("ok")
            except (ValueError, sqlite3.Error):
                outcomes.append("rejected")
            finally:
                conn.close()

        threads = [threading.Thread(target=link, args=(record_id,)) for record_id in (
            "FIX-RACE-001", "FIX-RACE-002"
        )]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["ok", "rejected"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM memory_relations WHERE relation = 'resolves'"
        ).fetchone()[0], 1)

    def test_component_union_depth_and_health_query_count_are_bounded(self):
        total = 256
        heads = []
        for index in range(total):
            record_id = f"DECISION-SCALE-{index:04d}"
            db.create_memory_record(
                self.conn, record_id, "scale", "decision", record_id,
                "large scale lifecycle", "current", "codex",
            )
            heads.append(record_id)
        while len(heads) > 1:
            merged = []
            for old, new in zip(heads[0::2], heads[1::2]):
                db.add_memory_relation(self.conn, new, "supersedes", old, "", "codex")
                merged.append(new)
            heads = merged
        parent = {
            row["id"]: row["parent_id"]
            for row in self.conn.execute("SELECT id, parent_id FROM memory_components")
        }
        max_depth = 0
        for component_id in parent:
            depth = 0
            while parent[component_id] is not None:
                component_id = parent[component_id]
                depth += 1
            max_depth = max(max_depth, depth)
        self.assertLessEqual(max_depth, math.ceil(math.log2(total)))

        statements = []
        self.conn.set_trace_callback(statements.append)
        try:
            health = db.memory_relation_health(self.conn)
        finally:
            self.conn.set_trace_callback(None)
        reads = [statement for statement in statements if statement.lstrip().upper().startswith(("SELECT", "WITH"))]
        self.assertTrue(health["ok"])
        # v9 verifies all three native FTS identities (unicode, Porter, and
        # trigram), adding two bounded index probes to the lifecycle health
        # check without changing its O(N+E) graph pass.
        self.assertLessEqual(len(reads), 14)

    def test_normal_query_includes_current_sqlite_native_records(self):
        db.create_memory_record(
            self.conn, "KNOWLEDGE-UNIFIED-001", "demo", "knowledge", "Unified lookup",
            "unique native unified needle", "current", "codex",
        )
        results = db.search_all(
            self.conn, "native unified needle", "demo", None, 5, semantic="off"
        )
        self.assertEqual(results[0]["id"], "KNOWLEDGE-UNIFIED-001")
        self.assertEqual(results[0]["source_kind"], "sqlite")

    def test_context_cap_never_returns_dangling_relation_endpoints(self):
        for index in range(5):
            db.create_memory_record(
                self.conn, f"KNOWLEDGE-CONTEXT-{index:03d}", "demo", "knowledge",
                f"Context {index}", "bounded graph", "current", "codex",
            )
        for index in range(1, 5):
            db.add_memory_relation(
                self.conn, "KNOWLEDGE-CONTEXT-000", "references",
                f"KNOWLEDGE-CONTEXT-{index:03d}", "", "codex",
            )
        context = db.memory_record_context(
            self.conn, "KNOWLEDGE-CONTEXT-000", depth=3, max_records=3
        )
        self.assertTrue(context["truncated"])
        self.assertLessEqual(len(context["records"]), 3)
        self.assertTrue(all(
            edge["source_id"] in context["records"] and edge["target_id"] in context["records"]
            for edge in context["relations"]
        ))

    def test_read_commands_do_not_run_schema_initialization(self):
        self.conn.commit()
        with mock.patch.object(db, "initialize", side_effect=AssertionError("must stay read-only")):
            with mock.patch.object(db, "_embed_health", return_value=None):
                with redirect_stdout(io.StringIO()):
                    exit_code = db.main(["--db", str(self.database), "stats"])
        self.assertEqual(exit_code, 0)
        read_only = db.connect(self.database, read_only=True)
        try:
            self.assertEqual(read_only.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                read_only.execute("UPDATE database_meta SET value = 'broken'")
        finally:
            read_only.close()

    def test_standalone_init_accepts_any_local_username(self):
        created = self.root / "standalone.sqlite3"
        with mock.patch.object(db.getpass, "getuser", return_value="another-user"), \
             redirect_stdout(io.StringIO()):
            exit_code = db.main(["--db", str(created), "init"])
        self.assertEqual(exit_code, 0)
        self.assertTrue(created.is_file())

    def test_health_reads_real_native_fts_index_identity(self):
        db.create_memory_record(
            self.conn, "KNOWLEDGE-FTS-IDENTITY", "demo", "knowledge", "FTS identity",
            "detect a missing inverted index row", "current", "codex",
        )
        row = self.conn.execute(
            "SELECT fts_rowid, title, content, project, record_type FROM memory_records WHERE id = ?",
            ("KNOWLEDGE-FTS-IDENTITY",),
        ).fetchone()
        self.conn.execute(
            """INSERT INTO memory_records_fts(
                   memory_records_fts, rowid, title, content, project, record_type
               ) VALUES('delete', ?, ?, ?, ?, ?)""",
            tuple(row),
        )
        health = db.memory_relation_health(self.conn)
        self.assertEqual(health["fts_missing"], 1)
        self.assertFalse(health["ok"])

    def test_v3_native_lifecycle_components_are_backfilled(self):
        for record_id in ("DECISION-MIGRATE-001", "DECISION-MIGRATE-002"):
            db.create_memory_record(
                self.conn, record_id, "demo", "decision", record_id,
                "migration lifecycle", "current", "codex",
            )
        db.add_memory_relation(
            self.conn, "DECISION-MIGRATE-002", "supersedes", "DECISION-MIGRATE-001", "", "codex"
        )
        self.conn.executescript("""
            DROP TRIGGER memory_relations_lifecycle_ai;
            DROP TRIGGER memory_relations_validate_bi;
            DROP TRIGGER memory_records_component_ai;
            DROP TABLE memory_record_components;
            DROP TABLE memory_components;
            UPDATE database_meta SET value = '3' WHERE key = 'schema_version';
        """)
        db.initialize(self.conn)
        self.assertEqual(
            db._terminal_current_ids(self.conn, "DECISION-MIGRATE-001"),
            ["DECISION-MIGRATE-002"],
        )
        self.assertTrue(db.memory_relation_health(self.conn)["ok"])

    def test_v3_inverse_symmetric_duplicates_are_merged_on_migration(self):
        for record_id in ("DECISION-MIGRATE-SYMMETRIC-A", "DECISION-MIGRATE-SYMMETRIC-B"):
            db.create_memory_record(
                self.conn, record_id, "demo", "decision", record_id,
                "legacy conflict", "current", "codex",
            )
        self.conn.execute("DROP INDEX idx_memory_relations_symmetric")
        self.conn.execute("""INSERT INTO memory_relations(
            source_id, target_id, relation, note, created_by, created_at
        ) VALUES(?, ?, 'contradicts', 'first', 'legacy', 'old')""", (
            "DECISION-MIGRATE-SYMMETRIC-A", "DECISION-MIGRATE-SYMMETRIC-B",
        ))
        self.conn.execute("""INSERT INTO memory_relations(
            source_id, target_id, relation, note, created_by, created_at
        ) VALUES(?, ?, 'contradicts', 'second', 'legacy', 'old')""", (
            "DECISION-MIGRATE-SYMMETRIC-B", "DECISION-MIGRATE-SYMMETRIC-A",
        ))
        self.conn.execute(
            "UPDATE database_meta SET value = '3' WHERE key = 'schema_version'"
        )
        self.conn.commit()
        db.initialize(self.conn)
        rows = self.conn.execute(
            "SELECT note FROM memory_relations WHERE relation = 'contradicts'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["note"], "first | second")
        self.assertTrue(db.memory_relation_health(self.conn)["ok"])

    def test_rejected_v3_branch_migration_rolls_back_v4_triggers(self):
        for record_id, record_type in (
            ("AUDIT-MIGRATE-BRANCH", "audit"),
            ("FIX-MIGRATE-BRANCH-A", "fix"),
            ("FIX-MIGRATE-BRANCH-B", "fix"),
        ):
            db.create_memory_record(
                self.conn, record_id, "demo", record_type, record_id,
                "legacy ambiguous lifecycle", "open" if record_type == "audit" else "current", "codex",
            )
        self.conn.executescript("""
            DROP TRIGGER memory_relations_lifecycle_ai;
            DROP TRIGGER memory_relations_validate_bi;
            DROP TRIGGER memory_relations_lifecycle_bd;
            DROP TRIGGER memory_records_component_ai;
            DROP TABLE memory_record_components;
            DROP TABLE memory_components;
            INSERT INTO memory_relations(source_id, target_id, relation, note, created_by, created_at)
            VALUES('FIX-MIGRATE-BRANCH-A', 'AUDIT-MIGRATE-BRANCH', 'resolves', '', 'legacy', 'old');
            INSERT INTO memory_relations(source_id, target_id, relation, note, created_by, created_at)
            VALUES('FIX-MIGRATE-BRANCH-B', 'AUDIT-MIGRATE-BRANCH', 'resolves', '', 'legacy', 'old');
            UPDATE database_meta SET value = '3' WHERE key = 'schema_version';
        """)
        with self.assertRaisesRegex(ValueError, "branches"):
            db.initialize(self.conn)
        self.assertEqual(db.database_schema_version(self.conn), "3")
        self.assertFalse(db.table_exists(self.conn, "memory_components"))
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = 'memory_relations_lifecycle_bd'"
        ).fetchone())

    def test_record_update_preserves_stable_id_and_refreshes_fts(self):
        record_id = db.create_memory_record(
            self.conn, "AUDIT-UPDATE-001", "demo", "audit", "Old title",
            "obsolete zebra wording", "open", "codex"
        )
        updated = db.update_memory_record(
            self.conn, record_id, "claude", title="New title", content="fresh searchable wording"
        )
        self.assertEqual(updated["id"], record_id)
        self.assertFalse(db.search_memory_records(self.conn, "obsolete zebra"))
        self.assertEqual(db.search_memory_records(self.conn, "fresh searchable")[0]["id"], record_id)

    def test_action_state_is_independent_from_truth_status_and_closes_explicitly(self):
        decision = db.create_memory_record(
            self.conn, "DECISION-NOT-WORK", "demo", "decision", "not open work",
            "This is current truth, not a task.", "open", "codex",
        )
        audit = db.create_memory_record(
            self.conn, "AUDIT-DEFERRED-WORK", "demo", "audit", "defer this",
            "Current finding, scheduled later.", "open", "codex", action_state="deferred",
        )
        pending = db.build_pending_worklist(self.conn, "demo")
        self.assertNotIn(decision, {item["id"] for item in pending["actionable_records"]})
        self.assertEqual([item["id"] for item in pending["deferred_records"]], [audit])
        self.assertEqual(db.memory_record_context(self.conn, decision)["record"]["action_state"], "nonactionable")

        updated = db.update_memory_record(self.conn, audit, "codex", status="resolved")
        self.assertEqual(updated["status"], "resolved")
        self.assertEqual(updated["action_state"], "done")
        self.assertEqual(db.build_pending_worklist(self.conn, "demo")["deferred_records"], [])

    def test_v9_record_action_state_migration_is_deterministic_and_not_prose_based(self):
        db.create_memory_record(
            self.conn, "AUDIT-MIGRATE-ACTION", "demo", "audit", "FIXED in title",
            "The prose claims fixed but lifecycle is still open.", "open", "codex",
        )
        db.create_memory_record(
            self.conn, "DECISION-MIGRATE-ACTION", "demo", "decision", "not open work",
            "Current decision.", "open", "codex",
        )
        self.conn.execute("DROP INDEX idx_memory_records_action_state")
        self.conn.execute("ALTER TABLE memory_records DROP COLUMN action_state")
        self.conn.execute(
            "UPDATE database_meta SET value = '9' WHERE key = 'schema_version'"
        )
        self.conn.commit()

        db.initialize(self.conn)

        states = dict(self.conn.execute("SELECT id, action_state FROM memory_records"))
        self.assertEqual(states["AUDIT-MIGRATE-ACTION"], "actionable")
        self.assertEqual(states["DECISION-MIGRATE-ACTION"], "nonactionable")

    def test_resolve_record_content_prefers_file_over_inline_and_rejects_both_or_neither(self):
        content_file = self.root / "content.md"
        content_file.write_text("multi-line\nreport body\n", encoding="utf-8")
        self.assertEqual(
            db.resolve_record_content(None, str(content_file)), "multi-line\nreport body\n"
        )
        self.assertEqual(db.resolve_record_content("inline text", None), "inline text")
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            db.resolve_record_content("inline text", str(content_file))
        with self.assertRaisesRegex(ValueError, "requires --content"):
            db.resolve_record_content(None, None)

    def test_resolve_record_content_reads_stdin_when_file_is_dash(self):
        with mock.patch("sys.stdin", io.StringIO("piped report body\n")):
            self.assertEqual(db.resolve_record_content(None, "-"), "piped report body\n")

    def test_record_add_cli_reads_content_file_and_stores_source_metadata(self):
        content_file = self.root / "audit.md"
        content_file.write_text("long report\nwith multiple lines\n", encoding="utf-8")
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = db.main([
                "--db", str(self.database), "record-add",
                "--id", "AUDIT-CLI-001", "--project", "demo", "--type", "audit",
                "--title", "CLI content-file test",
                "--content-file", str(content_file),
                "--source", "ENDMEMEX/developer/audit.md",
                "--agent", "claude",
            ])
        self.assertEqual(exit_code, 0)
        record = json.loads(stdout.getvalue())["record"]
        self.assertEqual(record["content"], "long report\nwith multiple lines")
        self.assertEqual(record["metadata"]["source"], "ENDMEMEX/developer/audit.md")
        self.assertTrue(db.search_memory_records(self.conn, "multiple lines"))

    def test_checkpoint_handoff_round_trip(self):
        session_id = db.start_session(self.conn, "demo", "build feature", "codex", {})
        session = db.resolve_session(self.conn, session_id, None)
        db.add_checkpoint(self.conn, session, "codex", {
            "summary": "schema done",
            "work_done": "created tables",
            "current_state": "tests pending",
            "next_steps": "run tests",
            "files_changed": ["schema.sql"],
            "verification": ["schema initialized"],
            "status": "paused",
        })
        data = db.handoff(self.conn, session_id, None)
        self.assertEqual(data["session"]["status"], "paused")
        self.assertEqual(data["checkpoint"]["sequence"], 1)
        self.assertEqual(data["checkpoint"]["files_changed"], ["schema.sql"])
        self.assertEqual(data["checkpoint"]["next_steps"], "run tests")

    def test_paused_handoffs_lists_every_project_and_excludes_active_sessions(self):
        first_id = db.start_session(self.conn, "first", "first goal", "codex", {})
        first = db.resolve_session(self.conn, first_id, None)
        db.add_checkpoint(self.conn, first, "codex", {"summary": "first paused", "status": "paused"})
        active_id = db.start_session(self.conn, "active", "active goal", "codex", {})
        active = db.resolve_session(self.conn, active_id, None)
        db.add_checkpoint(self.conn, active, "codex", {"summary": "still active", "status": "active"})
        second_id = db.start_session(self.conn, "second", "second goal", "claude", {})
        second = db.resolve_session(self.conn, second_id, None)
        db.add_checkpoint(self.conn, second, "claude", {"summary": "second paused", "status": "paused"})

        handoffs = db.paused_handoffs(self.conn)

        self.assertEqual({item["session"]["project"] for item in handoffs}, {"first", "second"})
        self.assertEqual({item["checkpoint"]["summary"] for item in handoffs}, {"first paused", "second paused"})

    def test_handoff_all_paused_cli_returns_all_projects(self):
        session_id = db.start_session(self.conn, "demo", "goal", "codex", {})
        session = db.resolve_session(self.conn, session_id, None)
        db.add_checkpoint(self.conn, session, "codex", {"summary": "paused", "status": "paused"})
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = db.main(["--db", str(self.database), "handoff", "--all-paused", "--json"])

        self.assertEqual(exit_code, 0)
        data = json.loads(stdout.getvalue())
        self.assertEqual(data["handoffs"][0]["session"]["project"], "demo")

    def test_resolve_or_start_checkpoint_session_starts_one_when_none_exists(self):
        session = db.resolve_or_start_checkpoint_session(self.conn, None, "demo", "auto-started goal", "codex")
        self.assertEqual(session["project"], "demo")
        self.assertEqual(session["goal"], "auto-started goal")
        self.assertEqual(session["status"], "active")
        # A second call with the same project must reuse the now-active session, not start another.
        again = db.resolve_or_start_checkpoint_session(self.conn, None, "demo", "ignored goal", "codex")
        self.assertEqual(again["id"], session["id"])

    def test_resolve_or_start_checkpoint_session_requires_goal_and_project(self):
        with self.assertRaisesRegex(ValueError, "no matching"):
            db.resolve_or_start_checkpoint_session(self.conn, None, "demo", None, "codex")
        with self.assertRaisesRegex(ValueError, "provide --session or --project"):
            db.resolve_or_start_checkpoint_session(self.conn, None, None, "some goal", "codex")

    def test_project_session_resolution_rejects_ambiguity_and_preserves_explicit_identity(self):
        first_id = db.start_session(self.conn, "demo", "first", "codex", {})
        second_id = db.start_session(self.conn, "demo", "second", "claude", {})
        with self.assertRaisesRegex(db.AmbiguousSessionError, "provide --session"):
            db.resolve_session(self.conn, None, "demo")
        self.assertEqual(db.resolve_session(self.conn, second_id, "demo")["goal"], "second")
        with self.assertRaisesRegex(ValueError, "belongs to project demo"):
            db.resolve_session(self.conn, first_id, "other")

    def test_auto_start_does_not_mask_an_ambiguous_project(self):
        db.start_session(self.conn, "demo", "first", "codex", {})
        db.start_session(self.conn, "demo", "second", "claude", {})
        with self.assertRaises(db.AmbiguousSessionError):
            db.resolve_or_start_checkpoint_session(self.conn, None, "demo", "must not start", "codex")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE project = 'demo'"
        ).fetchone()[0], 2)

    def test_resolve_or_start_checkpoint_session_does_not_mask_a_bad_explicit_session_id(self):
        with self.assertRaisesRegex(ValueError, "no matching"):
            db.resolve_or_start_checkpoint_session(self.conn, "typo-session-id", "demo", "goal", "codex")

    def test_checkpoint_cli_auto_starts_session_from_goal(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = db.main([
                "--db", str(self.database), "checkpoint",
                "--project", "demo", "--goal", "first pass", "--agent", "claude",
                "--summary", "auto-created via checkpoint --goal",
            ])
        self.assertEqual(exit_code, 0)
        result = json.loads(stdout.getvalue())
        session = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (result["session_id"],)).fetchone()
        self.assertEqual(session["project"], "demo")
        self.assertEqual(session["goal"], "first pass")
        self.assertEqual(self.conn.execute(
            "SELECT summary FROM checkpoints WHERE session_id = ?", (result["session_id"],)
        ).fetchone()[0], "auto-created via checkpoint --goal")

    def test_handoff_json_returns_nulls_for_project_without_session(self):
        self.conn.commit()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = db.main(["--db", str(self.database), "handoff", "--project", "brand-new", "--json"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"checkpoint": None, "session": None})

    def test_handoff_without_json_or_with_bad_explicit_session_stays_an_error(self):
        self.conn.commit()
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", stderr):
            self.assertEqual(
                db.main(["--db", str(self.database), "handoff", "--project", "brand-new"]), 1
            )
            # An explicit --session that fails to resolve is a typo, not an
            # empty state — --json must not mask it as nulls.
            self.assertEqual(
                db.main(["--db", str(self.database), "handoff", "--session", "typo-id", "--json"]), 1
            )
        self.assertIn("no matching", stderr.getvalue())

    def test_timeline_cli_json_and_markdown_default(self):
        active_id = db.start_session(self.conn, "demo", "active goal", "codex", {})
        active = db.resolve_session(self.conn, active_id, None)
        db.add_checkpoint(self.conn, active, "codex", {"summary": "active work", "status": "active"})
        paused_id = db.start_session(self.conn, "demo", "paused goal", "claude", {})
        paused = db.resolve_session(self.conn, paused_id, None)
        db.add_checkpoint(self.conn, paused, "claude", {
            "summary": "paused work", "status": "paused",
            "files_changed": ["ENDMEMEX/sessions.py"],
        })

        json_stdout = io.StringIO()
        with redirect_stdout(json_stdout):
            exit_code = db.main(["--db", str(self.database), "timeline", "--json"])
        self.assertEqual(exit_code, 0)
        data = json.loads(json_stdout.getvalue())
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["order"], "newest_first")
        self.assertIn("retention_notice", data)

        markdown_stdout = io.StringIO()
        with redirect_stdout(markdown_stdout):
            exit_code = db.main(["--db", str(self.database), "timeline"])
        self.assertEqual(exit_code, 0)
        rendered = markdown_stdout.getvalue()
        self.assertIn("# ENDMEMEX — Checkpoint Timeline", rendered)
        self.assertIn("paused work", rendered)
        self.assertIn("active work", rendered)

        filtered_stdout = io.StringIO()
        with redirect_stdout(filtered_stdout):
            exit_code = db.main([
                "--db", str(self.database), "timeline",
                "--project", "demo", "--status", "paused", "--json",
            ])
        self.assertEqual(exit_code, 0)
        filtered = json.loads(filtered_stdout.getvalue())
        self.assertEqual(filtered["count"], 1)
        self.assertEqual(filtered["records"][0]["summary"], "paused work")

    def test_timeline_cli_is_read_only_and_writes_nothing(self):
        session_id = db.start_session(self.conn, "demo", "goal", "codex", {})
        session = db.resolve_session(self.conn, session_id, None)
        db.add_checkpoint(self.conn, session, "codex", {"summary": "x"})
        before = self.conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0]

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = db.main(["--db", str(self.database), "timeline", "--json"])
        self.assertEqual(exit_code, 0)

        after = self.conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0]
        self.assertEqual(before, after)
        self.assertFalse((self.root / "ACTIVITY.md").exists())

    def test_timeline_cli_legacy_empty_evidence_and_distinct_statuses(self):
        # A checkpoint recorded with only the required fields (no --work-done,
        # --files, etc.) matches production's older, sparser rows -- empty
        # fields must stay explicit, never invented.
        session_id = db.start_session(self.conn, "demo", "goal", "codex", {})
        session = db.resolve_session(self.conn, session_id, None)
        db.add_checkpoint(self.conn, session, "codex", {"summary": "bare"})

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = db.main(["--db", str(self.database), "timeline", "--json"])
        self.assertEqual(exit_code, 0)
        record = json.loads(stdout.getvalue())["records"][0]
        self.assertEqual(record["work_done"], "")
        self.assertEqual(record["files_changed"], [])
        self.assertEqual(record["checkpoint_status"], "current")
        self.assertEqual(record["session_status"], "active")

    def test_hook_status_and_install_hooks_lifecycle(self):
        hooks_dir = self.root / "githooks"
        source = self.root / "hook_source"
        source.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        with mock.patch.object(db, "_git_hooks_dir", return_value=hooks_dir), mock.patch.object(
            db, "GIT_HOOK_SOURCES", {"pre-commit": source}
        ):
            self.assertEqual(db.hook_status(), {"pre-commit": "missing"})
            self.assertFalse(db.hooks_ok())
            self.assertEqual(db.install_hooks(), {"pre-commit": "installed"})
            self.assertEqual(db.hook_status(), {"pre-commit": "installed"})
            self.assertTrue(db.hooks_ok())
            self.assertTrue((hooks_dir / "pre-commit").stat().st_mode & 0o111)
            (hooks_dir / "pre-commit").write_text("#!/bin/sh\necho drifted\n", encoding="utf-8")
            self.assertEqual(db.hook_status(), {"pre-commit": "differs"})
            self.assertFalse(db.hooks_ok())

    def test_hook_status_outside_git_repo_passes_but_missing_source_fails(self):
        with mock.patch.object(db, "_git_hooks_dir", return_value=None):
            self.assertEqual(db.hook_status(), {"pre-commit": "not_a_git_repo"})
            self.assertTrue(db.hooks_ok())
        with mock.patch.object(db, "GIT_HOOK_SOURCES", {"pre-commit": self.root / "does-not-exist"}):
            self.assertEqual(db.hook_status(), {"pre-commit": "tracked_copy_missing"})
            self.assertFalse(db.hooks_ok())

    def test_doctor_gates_ok_on_hook_installation_state(self):
        self.conn.commit()
        for status, expected_exit in (("installed", 0), ("missing", 1)):
            stdout = io.StringIO()
            with mock.patch.object(db, "_embed_health", return_value=None), mock.patch.object(
                db, "hook_status", return_value={"pre-commit": status}
            ), redirect_stdout(stdout):
                exit_code = db.main(["--db", str(self.database), "doctor"])
            report = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, expected_exit, report)
            self.assertEqual(report["hooks"], {"pre-commit": status})

    def test_tracked_docs_summary_counts_and_degrades_without_git(self):
        with mock.patch.object(sync_tracked, "discover_knowledge_docs", return_value={}):
            summary = db._tracked_docs_summary(self.conn)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["stale"], 0)
        with mock.patch.object(
            sync_tracked, "discover_knowledge_docs", side_effect=RuntimeError("git missing")
        ):
            summary = db._tracked_docs_summary(self.conn)
        self.assertIsNone(summary["ok"])
        self.assertIn("git missing", summary["error"])

    def test_tracked_docs_summary_scopes_a_readiness_preflight_to_its_project(self):
        tracked = {
            "project-a/PROJECT_MEMORY.md": ("PROJECT_A", "project_memory"),
            "project-b/PROJECT_MEMORY.md": ("PROJECT_B", "project_memory"),
        }
        empty_report = {
            "current": [], "stale": [], "missing": [], "orphaned": [], "metadata_mismatch": [],
        }
        with mock.patch.object(sync_tracked, "discover_knowledge_docs", return_value=tracked), mock.patch.object(
            sync_tracked, "freshness_report", return_value=empty_report
        ) as freshness:
            summary = db._tracked_docs_summary(self.conn, "PROJECT_A")
        self.assertEqual(summary["scope"], "PROJECT_A")
        self.assertTrue(summary["ok"])
        self.assertEqual(freshness.call_args.args[2], {
            "project-a/PROJECT_MEMORY.md": ("PROJECT_A", "project_memory"),
        })
        self.assertFalse(freshness.call_args.kwargs["include_orphans"])

    def test_readiness_reports_machine_health_coverage_ann_and_safe_actions(self):
        doctor = {
            "ok": True, "schema_version": db.SCHEMA_VERSION, "schema_current": True,
            "integrity": "ok", "foreign_key_issues": [], "knowledge_rows": 20_001,
            "fts_rows": {name: 20_001 for name in ("knowledge_fts", "knowledge_fts_porter", "knowledge_fts_trigram")},
            "fts_identity": {
                name: {"indexed": 20_001, "verified": True, "missing": [], "extra": []}
                for name in ("knowledge_fts", "knowledge_fts_porter", "knowledge_fts_trigram")
            },
            "embedding": {
                "knowledge_rows": 20_001, "embedded": 20_001, "pending": 0,
                "invalid_blobs": 0, "stale_hashes": 0,
                "memory_record_chunks": 3, "memory_record_embedded": 3,
                "memory_record_pending": 0,
            },
            "memory_relations": {"ok": True}, "hooks": {"pre-commit": "installed"},
        }
        docs = {"current": 1, "stale": 0, "missing": 0, "metadata_mismatch": 0, "orphaned": 2, "ok": False}
        with mock.patch.object(db, "doctor_report", return_value=doctor), mock.patch.object(
            db, "_tracked_docs_summary", return_value=docs
        ), mock.patch.object(db, "ann_helper", return_value={
            "status": "missing", "available": True, "fresh": False,
        }), mock.patch.object(db, "local_machine", return_value="local-host"):
            report = db.readiness_report(self.conn, "demo", self.database)
        self.assertTrue(report["read_only"])
        self.assertEqual(report["machine"], {"name": "local-host", "role": "local", "write_allowed": True})
        self.assertTrue(report["database"]["core_ok"])
        self.assertEqual(report["embedding"]["coverage"], 1.0)
        self.assertTrue(report["ann"]["required"])
        self.assertEqual(report["overall"], "attention")
        actions = {item["code"]: item for item in report["next_actions"]}
        self.assertEqual(actions["ann_sidecar"]["command"], "python3 ENDMEMEX/endeavor_db.py ann-build")
        self.assertIn("propose-prune", actions["review_orphaned_documents"]["command"])
        self.assertNotIn("bootstrap", "\n".join(item["command"] or "" for item in report["next_actions"]))

    def test_readiness_quotes_a_ready_project_name_in_its_follow_up_command(self):
        doctor = {
            "ok": True, "schema_version": db.SCHEMA_VERSION, "schema_current": True,
            "integrity": "ok", "foreign_key_issues": [], "knowledge_rows": 0,
            "fts_rows": {name: 0 for name in ("knowledge_fts", "knowledge_fts_porter", "knowledge_fts_trigram")},
            "fts_identity": {
                name: {"indexed": 0, "verified": True, "missing": [], "extra": []}
                for name in ("knowledge_fts", "knowledge_fts_porter", "knowledge_fts_trigram")
            },
            "embedding": {
                "knowledge_rows": 0, "embedded": 0, "pending": 0,
                "invalid_blobs": 0, "stale_hashes": 0,
                "memory_record_chunks": 0, "memory_record_embedded": 0,
                "memory_record_pending": 0,
            },
            "memory_relations": {"ok": True}, "hooks": {"pre-commit": "installed"},
        }
        docs = {"current": 0, "stale": 0, "missing": 0, "metadata_mismatch": 0, "orphaned": 0, "ok": True}
        project = "demo; touch /private/tmp/owned"
        with mock.patch.object(db, "doctor_report", return_value=doctor), mock.patch.object(
            db, "_tracked_docs_summary", return_value=docs
        ), mock.patch.object(db, "ann_helper", return_value={
            "status": "ready", "available": True, "fresh": True,
        }), mock.patch.object(db, "local_machine", return_value="local-host"):
            report = db.readiness_report(self.conn, project, self.database)
        self.assertEqual(report["next_actions"], [{
            "priority": "OK", "code": "ready",
            "reason": "The project preflight has no blocking or attention items.",
            "command": "python3 ENDMEMEX/endeavor_db.py bootstrap --project 'demo; touch /private/tmp/owned' --json",
            "guidance": "Start or resume the normal session workflow when work is ready to begin.",
        }])

    def test_readiness_does_not_call_unknown_ann_state_a_missing_dependency(self):
        row_count = db.SEMANTIC_FULL_SCAN_LIMIT + 1
        doctor = {
            "ok": True, "schema_version": db.SCHEMA_VERSION, "schema_current": True,
            "integrity": "ok", "foreign_key_issues": [], "knowledge_rows": row_count,
            "fts_rows": {name: row_count for name in ("knowledge_fts", "knowledge_fts_porter", "knowledge_fts_trigram")},
            "fts_identity": {
                name: {"indexed": row_count, "verified": True, "missing": [], "extra": []}
                for name in ("knowledge_fts", "knowledge_fts_porter", "knowledge_fts_trigram")
            },
            "embedding": {
                "knowledge_rows": row_count, "embedded": row_count, "pending": 0,
                "invalid_blobs": 0, "stale_hashes": 0,
                "memory_record_chunks": 0, "memory_record_embedded": 0,
                "memory_record_pending": 0,
            },
            "memory_relations": {"ok": True}, "hooks": {"pre-commit": "installed"},
        }
        docs = {"current": 0, "stale": 0, "missing": 0, "metadata_mismatch": 0, "orphaned": 0, "ok": True}
        with mock.patch.object(db, "doctor_report", return_value=doctor), mock.patch.object(
            db, "_tracked_docs_summary", return_value=docs
        ), mock.patch.object(db, "ann_helper", return_value={}), mock.patch.object(
            db, "local_machine", return_value="local-host"
        ):
            report = db.readiness_report(self.conn, "demo", self.database)
        action = report["next_actions"][0]
        self.assertEqual(report["ann"]["status"], "unavailable")
        self.assertIsNone(report["ann"]["available"])
        self.assertEqual(action["command"], "python3 ENDMEMEX/endeavor_db.py ann-status")
        self.assertNotIn("Install optional", action["guidance"])

    def test_readiness_requires_a_boolean_true_ann_fresh_signal(self):
        row_count = db.SEMANTIC_FULL_SCAN_LIMIT + 1
        doctor = {
            "ok": True, "schema_version": db.SCHEMA_VERSION, "schema_current": True,
            "integrity": "ok", "foreign_key_issues": [], "knowledge_rows": row_count,
            "fts_rows": {name: row_count for name in ("knowledge_fts", "knowledge_fts_porter", "knowledge_fts_trigram")},
            "fts_identity": {
                name: {"indexed": row_count, "verified": True, "missing": [], "extra": []}
                for name in ("knowledge_fts", "knowledge_fts_porter", "knowledge_fts_trigram")
            },
            "embedding": {
                "knowledge_rows": row_count, "embedded": row_count, "pending": 0,
                "invalid_blobs": 0, "stale_hashes": 0,
                "memory_record_chunks": 0, "memory_record_embedded": 0,
                "memory_record_pending": 0,
            },
            "memory_relations": {"ok": True}, "hooks": {"pre-commit": "installed"},
        }
        docs = {"current": 0, "stale": 0, "missing": 0, "metadata_mismatch": 0, "orphaned": 0, "ok": True}
        with mock.patch.object(db, "doctor_report", return_value=doctor), mock.patch.object(
            db, "_tracked_docs_summary", return_value=docs
        ), mock.patch.object(db, "ann_helper", return_value={
            "status": "ready", "available": True, "fresh": "yes",
        }), mock.patch.object(db, "local_machine", return_value="local-host"):
            report = db.readiness_report(self.conn, "demo", self.database)
        self.assertFalse(report["ann"]["fresh"])
        self.assertEqual(report["next_actions"][0]["command"], "python3 ENDMEMEX/endeavor_db.py ann-build")

    def test_ann_helper_status_reports_empty_subprocess_output_as_unavailable(self):
        completed = subprocess.CompletedProcess(args=["ann_index.py"], returncode=1, stdout="", stderr="failed")
        with mock.patch.object(db.subprocess, "run", return_value=completed):
            report = db.ann_helper(self.conn, "status")
        self.assertEqual(report["status"], "unavailable")
        self.assertIsNone(report["available"])
        self.assertFalse(report["fresh"])
        self.assertIn("no output", report["error"])

    def test_readiness_cli_returns_structured_report_without_a_write(self):
        expected = {"project": "demo", "overall": "ready", "read_only": True, "next_actions": []}
        stdout = io.StringIO()
        with mock.patch.object(db, "readiness_report", return_value=expected), redirect_stdout(stdout):
            exit_code = db.main(["--db", str(self.database), "readiness", "--project", "demo", "--json"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), expected)

    def test_readiness_missing_database_is_actionable_without_creating_it(self):
        missing = self.root / "not-created.sqlite3"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = db.main(["--db", str(missing), "readiness", "--project", "demo", "--json"])
        report = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(report["overall"], "blocked")
        self.assertFalse(report["database"]["ok"])
        self.assertEqual(report["next_actions"][0]["code"], "database_unavailable")
        self.assertIn(" init", report["next_actions"][0]["command"])
        self.assertFalse(missing.exists())

    def _bootstrap_mocks(self):
        return (
            mock.patch.object(db, "backfill_embeddings", return_value={
                "status": "ok", "candidates": 0, "embedded": 0, "attempts": 0,
            }),
            mock.patch.object(db, "_tracked_docs_summary", return_value={"ok": True}),
            mock.patch.object(db, "hook_status", return_value={"pre-commit": "installed"}),
        )

    def test_bootstrap_reports_null_session_for_a_new_project(self):
        backfill, docs, hooks = self._bootstrap_mocks()
        with backfill as backfill_call, docs, hooks:
            data = db.bootstrap(self.conn, "fresh-project")
        self.assertIsNone(data["session"])
        self.assertIsNone(data["checkpoint"])
        self.assertEqual(data["embedding"]["status"], "ok")
        self.assertEqual(data["docs"], {"ok": True})
        self.assertEqual(data["hooks"], {"pre-commit": "installed"})
        backfill_call.assert_called_once()

    def test_bootstrap_includes_latest_checkpoint_when_session_exists(self):
        session_id = db.start_session(self.conn, "demo", "resume me", "codex", {})
        session = db.resolve_session(self.conn, session_id, None)
        db.add_checkpoint(self.conn, session, "codex", {"summary": "left off here", "status": "paused"})
        backfill, docs, hooks = self._bootstrap_mocks()
        with backfill, docs, hooks:
            data = db.bootstrap(self.conn, "demo")
        self.assertEqual(data["session"]["id"], session_id)
        self.assertEqual(data["checkpoint"]["summary"], "left off here")

    def test_bootstrap_and_pack_preserve_an_explicit_session_selection(self):
        first_id = db.start_session(self.conn, "demo", "first", "codex", {})
        second_id = db.start_session(self.conn, "demo", "second", "claude", {})
        first = db.resolve_session(self.conn, first_id, "demo")
        second = db.resolve_session(self.conn, second_id, "demo")
        db.add_checkpoint(self.conn, first, "codex", {"summary": "first checkpoint", "status": "paused"})
        db.add_checkpoint(self.conn, second, "claude", {"summary": "second checkpoint", "status": "paused"})
        backfill, docs, hooks = self._bootstrap_mocks()
        with backfill, docs, hooks:
            boot = db.bootstrap(self.conn, "demo", session_id=first_id)
        self.assertEqual(boot["session"]["id"], first_id)
        self.assertEqual(boot["checkpoint"]["summary"], "first checkpoint")
        pack = db.build_pack(self.conn, "demo", session_id=second_id)
        self.assertEqual(pack["session"]["id"], second_id)
        self.assertEqual(pack["checkpoint"]["summary"], "second checkpoint")
        with self.assertRaises(db.AmbiguousSessionError):
            db.build_pack(self.conn, "demo")

    def test_bootstrap_and_pack_reject_an_unknown_explicit_session(self):
        backfill, docs, hooks = self._bootstrap_mocks()
        with backfill, docs, hooks, self.assertRaises(db.SessionNotFoundError):
            db.bootstrap(self.conn, "demo", session_id="missing-session")
        with self.assertRaises(db.SessionNotFoundError):
            db.build_pack(self.conn, "demo", session_id="missing-session")

    def test_bootstrap_cli_is_a_single_call(self):
        backfill, docs, hooks = self._bootstrap_mocks()
        stdout = io.StringIO()
        with backfill, docs, hooks, redirect_stdout(stdout):
            exit_code = db.main(["--db", str(self.database), "bootstrap", "--project", "demo", "--json"])
        self.assertEqual(exit_code, 0)
        data = json.loads(stdout.getvalue())
        self.assertEqual(data["project"], "demo")
        self.assertIn("embedding", data)
        self.assertIn("docs", data)
        self.assertIn("hooks", data)

    def test_render_activity_enriches_every_action_type(self):
        source = self.root / "memo.md"
        source.write_text("# Memo\n\nSearchable body.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        db.create_memory_record(
            self.conn, "AUDIT-ACT-001", "demo", "audit", "Activity render audit",
            "body", "open", "codex",
        )
        session_id = db.start_session(self.conn, "demo", "render the log", "claude", {})
        session = db.resolve_session(self.conn, session_id, None)
        db.add_checkpoint(self.conn, session, "claude", {"summary": "multi\nline  summary"})
        self.conn.commit()

        text = db.render_activity(self.conn, 50)
        self.assertIn('checkpoint · demo — #1 "multi line summary" [session active: render the log]', text)
        self.assertIn('goal: "render the log"', text)
        self.assertIn('AUDIT-ACT-001 [audit/open] "Activity render audit"', text)
        self.assertIn("(1 entries)", text)
        # Newest first: the checkpoint line must come before the ingest line.
        self.assertLess(text.index("checkpoint · demo"), text.index("ingest · demo"))

    def test_render_activity_survives_pruned_checkpoint_and_clamps_limit(self):
        session_id = db.start_session(self.conn, "demo", "prune survivor", "codex", {})
        session = db.resolve_session(self.conn, session_id, None)
        db.add_checkpoint(self.conn, session, "codex", {"summary": "will be pruned"})
        self.conn.execute("DELETE FROM checkpoints")
        self.conn.commit()
        text = db.render_activity(self.conn, 10_000)
        self.assertIn("(checkpoint pruned by retention)", text)
        self.assertIn("Latest 2 of 2", text)

    def test_write_commands_auto_refresh_activity_export(self):
        export = db.activity_export_path(self.database)
        self.assertFalse(export.exists())
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = db.main([
                "--db", str(self.database), "checkpoint",
                "--project", "demo", "--goal", "auto export goal", "--agent", "claude",
                "--summary", "auto export probe summary",
            ])
        self.assertEqual(exit_code, 0)
        self.assertTrue(export.exists())
        content = export.read_text(encoding="utf-8")
        self.assertIn("auto export probe summary", content)
        self.assertIn("session_start", content)

    def test_read_commands_do_not_write_activity_export(self):
        self.conn.commit()
        with mock.patch.object(db, "_embed_health", return_value=None), redirect_stdout(io.StringIO()):
            self.assertEqual(db.main(["--db", str(self.database), "stats"]), 0)
        self.assertFalse(db.activity_export_path(self.database).exists())

    def test_activity_cli_writes_file_and_stdout_mode_prints(self):
        db.start_session(self.conn, "demo", "cli export", "claude", {})
        self.conn.commit()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = db.main(["--db", str(self.database), "activity"])
        self.assertEqual(exit_code, 0)
        target = Path(json.loads(stdout.getvalue())["path"])
        self.assertEqual(target.resolve(), db.activity_export_path(self.database).resolve())
        self.assertIn("cli export", target.read_text(encoding="utf-8"))
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(db.main(["--db", str(self.database), "activity", "--stdout"]), 0)
        self.assertIn("cli export", stdout.getvalue())

    def test_session_close_is_logged_and_exported(self):
        db.start_session(self.conn, "demo", "close me", "claude", {})
        self.conn.commit()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = db.main([
                "--db", str(self.database), "session-close",
                "--project", "demo", "--agent", "claude", "--status", "completed",
            ])
        self.assertEqual(exit_code, 0)
        content = db.activity_export_path(self.database).read_text(encoding="utf-8")
        self.assertIn("session_close", content)
        self.assertIn("status: completed", content)

    def test_refresh_activity_export_failure_never_raises(self):
        with mock.patch.object(db, "render_activity", side_effect=RuntimeError("boom")):
            stderr = io.StringIO()
            with mock.patch("sys.stderr", stderr):
                self.assertIsNone(db.refresh_activity_export(self.conn, self.database))
            self.assertIn("could not refresh", stderr.getvalue())

    def test_completed_session_cannot_be_reopened_by_checkpoint(self):
        session_id = db.start_session(self.conn, "demo", "completed lifecycle", "codex", {})
        self.conn.execute("UPDATE sessions SET status = 'completed' WHERE id = ?", (session_id,))
        self.conn.commit()
        session = db.resolve_session(self.conn, session_id, None)
        with self.assertRaisesRegex(ValueError, "completed"):
            db.add_checkpoint(self.conn, session, "claude", {"summary": "must not reopen"})
        self.assertEqual(self.conn.execute("SELECT status FROM sessions WHERE id = ?", (session_id,)).fetchone()[0], "completed")

    def test_checkpoint_retention_keeps_only_newest_records(self):
        session_id = db.start_session(self.conn, "demo", "retention", "codex", {})
        session = db.resolve_session(self.conn, session_id, None)
        original_limit = db.MAX_CHECKPOINTS
        db.MAX_CHECKPOINTS = 3
        try:
            for sequence in range(4):
                db.add_checkpoint(self.conn, session, "codex", {"summary": f"checkpoint {sequence}"})
        finally:
            db.MAX_CHECKPOINTS = original_limit
        retained = [row[0] for row in self.conn.execute(
            "SELECT sequence FROM checkpoints WHERE session_id = ? ORDER BY sequence", (session_id,)
        )]
        self.assertEqual(retained, [2, 3, 4])

    def _closed_session_with_checkpoints(self, goal: str, summaries: list[str]) -> str:
        """add_checkpoint refuses to write to a completed session, so a closed
        session's history has to be built while it is still open and closed
        afterwards -- the same order real work happens in."""
        session_id = db.start_session(self.conn, "demo", goal, "codex", {})
        session = db.resolve_session(self.conn, session_id, None)
        for summary in summaries:
            db.add_checkpoint(self.conn, session, "codex", {"summary": summary})
        self.conn.execute("UPDATE sessions SET status = 'completed' WHERE id = ?", (session_id,))
        self.conn.commit()
        return session_id

    def test_global_checkpoint_cap_trims_oldest_across_sessions(self):
        # MAX_CHECKPOINTS is scoped by session_id and sessions are never
        # pruned, so the real ceiling was 500 x an ever-growing session count.
        # This is the second tier that actually bounds the table.
        for index in range(3):
            self._closed_session_with_checkpoints(f"goal {index}", [f"s{index}-c0", f"s{index}-c1"])
        removed = db.prune_checkpoints_globally(self.conn, keep=4)
        self.assertEqual(removed, 2)
        summaries = [row[0] for row in self.conn.execute("SELECT summary FROM checkpoints ORDER BY id")]
        # Every session closed, so nothing is exempt: a plain newest-4 window.
        self.assertEqual(summaries, ["s1-c0", "s1-c1", "s2-c0", "s2-c1"])

    def test_global_checkpoint_cap_never_evicts_an_open_session_handoff(self):
        # The reason a plain "keep newest N" was not acceptable: it deletes the
        # globally oldest rows, and the oldest row can be the LAST checkpoint of
        # a session paused months ago -- exactly what handoff() returns. Without
        # the exemption that session's resume context would silently go null.
        paused_id = db.start_session(self.conn, "demo", "paused work", "codex", {})
        paused = db.resolve_session(self.conn, paused_id, None)
        db.add_checkpoint(self.conn, paused, "codex", {"summary": "resume here", "status": "paused"})
        busy_id = self._closed_session_with_checkpoints(
            "busy work", [f"busy {index}" for index in range(8)]
        )

        removed = db.prune_checkpoints_globally(self.conn, keep=3)
        self.assertEqual(removed, 5)
        # Survives despite being the globally oldest row by a wide margin, and
        # the exemption sits ON TOP of the budget rather than consuming it.
        self.assertEqual(db.handoff(self.conn, paused_id, None)["checkpoint"]["summary"], "resume here")
        busy_kept = [row[0] for row in self.conn.execute(
            "SELECT summary FROM checkpoints WHERE session_id = ? ORDER BY id", (busy_id,)
        )]
        self.assertEqual(busy_kept, ["busy 5", "busy 6", "busy 7"])

    def test_global_checkpoint_cap_exemption_follows_the_status_just_written(self):
        # add_checkpoint prunes AFTER the session status update, so a checkpoint
        # that CLOSES a session drops that session's exemption in the same call.
        # Had the prune read the pre-update status, "done now" would still have
        # been exempt and "old work" would have survived as the newest
        # non-exempt row -- so this assertion is what pins the ordering.
        closing_id = db.start_session(self.conn, "demo", "closing work", "codex", {})
        closing = db.resolve_session(self.conn, closing_id, None)
        original = db.MAX_TOTAL_CHECKPOINTS
        db.MAX_TOTAL_CHECKPOINTS = 1
        try:
            db.add_checkpoint(self.conn, closing, "codex", {"summary": "old work", "status": "active"})
            db.add_checkpoint(self.conn, closing, "codex", {"summary": "done now", "status": "completed"})
        finally:
            db.MAX_TOTAL_CHECKPOINTS = original
        remaining = [row[0] for row in self.conn.execute("SELECT summary FROM checkpoints ORDER BY id")]
        self.assertEqual(remaining, ["done now"])

    def test_global_checkpoint_cap_protects_every_open_status(self):
        # Guarded as `status != 'completed'` rather than an explicit
        # IN ('active','paused','blocked') list, so a status added to the
        # schema later is protected by default instead of silently prunable.
        open_ids = []
        for status in ("active", "paused", "blocked"):
            session_id = db.start_session(self.conn, "demo", f"{status} work", "codex", {})
            session = db.resolve_session(self.conn, session_id, None)
            db.add_checkpoint(self.conn, session, "codex", {"summary": f"keep {status}", "status": status})
            open_ids.append(session_id)
        self._closed_session_with_checkpoints("busy work", [f"busy {i}" for i in range(4)])

        db.prune_checkpoints_globally(self.conn, keep=1)
        for session_id in open_ids:
            self.assertIsNotNone(db.handoff(self.conn, session_id, None)["checkpoint"])
        # Budget spent only on prunable rows: 3 exemptions + 1 kept = 4.
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0], 4)

    def test_checkpoint_retention_does_not_evict_another_session(self):
        first_id = db.start_session(self.conn, "demo", "paused work", "codex", {})
        second_id = db.start_session(self.conn, "demo", "busy work", "codex", {})
        first = db.resolve_session(self.conn, first_id, None)
        second = db.resolve_session(self.conn, second_id, None)
        original_limit = db.MAX_CHECKPOINTS
        db.MAX_CHECKPOINTS = 2
        try:
            db.add_checkpoint(self.conn, first, "codex", {"summary": "preserve me", "status": "paused"})
            for index in range(3):
                db.add_checkpoint(self.conn, second, "codex", {"summary": f"busy {index}"})
        finally:
            db.MAX_CHECKPOINTS = original_limit
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE session_id = ?", (first_id,)
        ).fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE session_id = ?", (second_id,)
        ).fetchone()[0], 2)

    def test_pinned_checkpoint_survives_per_session_retention(self):
        session_id = db.start_session(self.conn, "demo", "pin retention", "codex", {})
        session = db.resolve_session(self.conn, session_id, None)
        original_limit = db.MAX_CHECKPOINTS
        db.MAX_CHECKPOINTS = 2
        try:
            pinned_id = db.add_checkpoint(self.conn, session, "codex", {"summary": "important", "pinned": True})
            for index in range(3):
                db.add_checkpoint(self.conn, session, "codex", {"summary": f"routine {index}"})
        finally:
            db.MAX_CHECKPOINTS = original_limit
        remaining = {row[0] for row in self.conn.execute(
            "SELECT id FROM checkpoints WHERE session_id = ?", (session_id,)
        )}
        self.assertIn(pinned_id, remaining)
        summaries = [row[0] for row in self.conn.execute(
            "SELECT summary FROM checkpoints WHERE session_id = ? ORDER BY id", (session_id,)
        )]
        # Pinned row survives on top of the newest-2 unpinned window.
        self.assertEqual(summaries, ["important", "routine 1", "routine 2"])

    def test_pinned_checkpoint_survives_global_prune_even_in_completed_session(self):
        closed_id = self._closed_session_with_checkpoints("closed with a keeper", ["forgettable"])
        pinned_checkpoint = self.conn.execute(
            "SELECT id FROM checkpoints WHERE session_id = ?", (closed_id,)
        ).fetchone()[0]
        db.set_checkpoint_pinned(self.conn, pinned_checkpoint, True, "claude")
        self._closed_session_with_checkpoints("busy work", [f"busy {i}" for i in range(4)])

        removed = db.prune_checkpoints_globally(self.conn, keep=1)
        # The pinned row is excluded from the candidate set entirely, leaving
        # only the 4 "busy work" rows to prune against keep=1 -> 3 removed.
        self.assertEqual(removed, 3)
        remaining_summaries = {row[0] for row in self.conn.execute("SELECT summary FROM checkpoints")}
        self.assertIn("forgettable", remaining_summaries)

    def test_unpin_checkpoint_returns_it_to_normal_pruning(self):
        session_id = db.start_session(self.conn, "demo", "unpin", "codex", {})
        session = db.resolve_session(self.conn, session_id, None)
        checkpoint_id = db.add_checkpoint(self.conn, session, "codex", {"summary": "was important", "pinned": True})
        updated = db.set_checkpoint_pinned(self.conn, checkpoint_id, False, "claude")
        self.assertEqual(updated["pinned"], 0)

        original_limit = db.MAX_CHECKPOINTS
        db.MAX_CHECKPOINTS = 1
        try:
            db.add_checkpoint(self.conn, session, "codex", {"summary": "newer"})
        finally:
            db.MAX_CHECKPOINTS = original_limit
        remaining_ids = {row[0] for row in self.conn.execute(
            "SELECT id FROM checkpoints WHERE session_id = ?", (session_id,)
        )}
        self.assertNotIn(checkpoint_id, remaining_ids)

    def test_pin_checkpoint_unknown_id_raises(self):
        with self.assertRaisesRegex(ValueError, "no checkpoint"):
            db.set_checkpoint_pinned(self.conn, 999999, True, "claude")

    def test_pinned_checkpoint_warning_is_none_under_threshold(self):
        session_id = db.start_session(self.conn, "demo", "few pins", "codex", {})
        session = db.resolve_session(self.conn, session_id, None)
        db.add_checkpoint(self.conn, session, "codex", {"summary": "pinned one", "pinned": True})
        self.assertIsNone(db.pinned_checkpoint_warning(self.conn))

    def test_pinned_checkpoint_warning_names_the_oldest_pin_over_threshold(self):
        session_id = db.start_session(self.conn, "demo", "many pins", "codex", {})
        session = db.resolve_session(self.conn, session_id, None)
        original_threshold = db.MAX_PINNED_CHECKPOINTS_WARN
        db.MAX_PINNED_CHECKPOINTS_WARN = 2
        try:
            first_id = db.add_checkpoint(self.conn, session, "codex", {"summary": "oldest pin", "pinned": True})
            db.add_checkpoint(self.conn, session, "codex", {"summary": "second pin", "pinned": True})
            db.add_checkpoint(self.conn, session, "codex", {"summary": "third pin", "pinned": True})
            warning = db.pinned_checkpoint_warning(self.conn)
        finally:
            db.MAX_PINNED_CHECKPOINTS_WARN = original_threshold
        self.assertIsNotNone(warning)
        self.assertEqual(warning["pinned_total"], 3)
        self.assertEqual(warning["threshold"], 2)
        self.assertEqual(warning["suggested_unpin"]["id"], first_id)
        self.assertEqual(warning["suggested_unpin"]["summary"], "oldest pin")

    def test_pin_checkpoint_surfaces_warning_only_when_pinning_over_threshold(self):
        session_id = db.start_session(self.conn, "demo", "retroactive pins", "codex", {})
        session = db.resolve_session(self.conn, session_id, None)
        checkpoint_ids = [
            db.add_checkpoint(self.conn, session, "codex", {"summary": f"cp {i}"}) for i in range(3)
        ]
        original_threshold = db.MAX_PINNED_CHECKPOINTS_WARN
        db.MAX_PINNED_CHECKPOINTS_WARN = 2
        try:
            result = db.set_checkpoint_pinned(self.conn, checkpoint_ids[0], True, "claude")
            self.assertNotIn("pin_warning", result)
            db.set_checkpoint_pinned(self.conn, checkpoint_ids[1], True, "claude")
            over_threshold = db.set_checkpoint_pinned(self.conn, checkpoint_ids[2], True, "claude")
            self.assertIn("pin_warning", over_threshold)
            self.assertEqual(over_threshold["pin_warning"]["pinned_total"], 3)
            # Unpinning never triggers the warning even while still over threshold.
            unpin_result = db.set_checkpoint_pinned(self.conn, checkpoint_ids[2], False, "claude")
            self.assertNotIn("pin_warning", unpin_result)
        finally:
            db.MAX_PINNED_CHECKPOINTS_WARN = original_threshold

    def test_checkpoint_cli_pin_flag_surfaces_warning_over_threshold(self):
        original_threshold = db.MAX_PINNED_CHECKPOINTS_WARN
        db.MAX_PINNED_CHECKPOINTS_WARN = 1
        try:
            with redirect_stdout(io.StringIO()):
                db.main([
                    "--db", str(self.database), "checkpoint", "--project", "demo", "--goal", "cli pin test",
                    "--agent", "claude", "--summary", "first pin", "--pin",
                ])
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = db.main([
                    "--db", str(self.database), "checkpoint", "--project", "demo",
                    "--agent", "claude", "--summary", "second pin", "--pin",
                ])
        finally:
            db.MAX_PINNED_CHECKPOINTS_WARN = original_threshold
        self.assertEqual(exit_code, 0)
        result = json.loads(stdout.getvalue())
        self.assertIn("pin_warning", result)
        self.assertEqual(result["pin_warning"]["pinned_total"], 2)

    def test_stats_reports_pin_warning_over_threshold(self):
        session_id = db.start_session(self.conn, "demo", "stats pin", "codex", {})
        session = db.resolve_session(self.conn, session_id, None)
        original_threshold = db.MAX_PINNED_CHECKPOINTS_WARN
        db.MAX_PINNED_CHECKPOINTS_WARN = 1
        try:
            db.add_checkpoint(self.conn, session, "codex", {"summary": "pin a", "pinned": True})
            db.add_checkpoint(self.conn, session, "codex", {"summary": "pin b", "pinned": True})
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = db.main(["--db", str(self.database), "stats"])
        finally:
            db.MAX_PINNED_CHECKPOINTS_WARN = original_threshold
        self.assertEqual(exit_code, 0)
        stats = json.loads(stdout.getvalue())
        self.assertEqual(stats["checkpoints_pinned"], 2)
        self.assertIsNotNone(stats["checkpoints_pin_warning"])

    def test_native_fts_survives_vacuum_with_stable_key(self):
        record_id = db.create_memory_record(
            self.conn, "KNOWLEDGE-FTS-STABLE", "demo", "knowledge", "Stable FTS",
            "vacuum-safe searchable content", "current", "codex",
        )
        before = self.conn.execute("SELECT fts_rowid FROM memory_records WHERE id = ?", (record_id,)).fetchone()[0]
        self.conn.execute("VACUUM")
        after = self.conn.execute("SELECT fts_rowid FROM memory_records WHERE id = ?", (record_id,)).fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual([row["id"] for row in db.search_memory_records(self.conn, "vacuum-safe")], [record_id])

    def test_negative_fixed_wording_is_open(self):
        self.assertEqual(db.extract_metadata("Heading", "not fixed", "debugging")["status"], "open")

    def test_wal_and_foreign_keys_are_enabled(self):
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_two_agents_can_checkpoint_concurrently(self):
        session_id = db.start_session(self.conn, "demo", "shared work", "codex", {})
        barrier = threading.Barrier(2)
        errors = []

        def writer(agent):
            connection = db.connect(self.database)
            try:
                session = db.resolve_session(connection, session_id, None)
                barrier.wait(timeout=2)
                db.add_checkpoint(connection, session, agent, {"summary": f"checkpoint from {agent}"})
            except Exception as exc:
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=writer, args=(agent,)) for agent in ("codex", "claude")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        self.assertFalse(errors)
        sequences = [row[0] for row in self.conn.execute(
            "SELECT sequence FROM checkpoints WHERE session_id = ? ORDER BY sequence", (session_id,)
        )]
        self.assertEqual(sequences, [1, 2])

    # ── Semantic search: packing, hashing, graceful degrade ─────────────────
    # These never spawn embed_server.py (no `wait=True` / `semantic="on"`
    # calls here) — that would load a real MiniLM model and leak a
    # background process into a supposedly-fast unit test. Real end-to-end
    # semantic behavior is verified manually/live, matching how this
    # codebase never runs a real local LLM inside its fast test suites.

    def test_embedding_pack_unpack_round_trip_is_lossy_within_float16(self):
        sample = [0.1, -0.5, 0.999, 0.0, -1.0, 0.333333]
        vector = (sample * ((db.EMBED_DIM + len(sample) - 1) // len(sample)))[:db.EMBED_DIM]
        blob = db.pack_embedding(vector)
        self.assertEqual(len(blob), len(vector) * 2)  # float16 = 2 bytes/dim
        restored = db.unpack_embedding(blob)
        for original, back in zip(vector, restored):
            self.assertAlmostEqual(original, back, places=2)

    def test_embedding_hash_is_deterministic_and_content_sensitive(self):
        first = db.embedding_hash("cache race with one lock")
        second = db.embedding_hash("cache race with one lock")
        different = db.embedding_hash("a completely different sentence")
        self.assertEqual(first, second)
        self.assertNotEqual(first, different)

    def test_native_memory_records_are_chunked_and_backfilled(self):
        record_id = db.create_memory_record(
            self.conn, "KNOWLEDGE-EMBED-1", "demo", "knowledge", "Native",
            "x" * (db.MAX_CHUNK_CHARS + 20), "current", "codex",
        )
        chunks = self.conn.execute(
            "SELECT chunk_index, content FROM memory_record_embeddings WHERE record_id = ? ORDER BY chunk_index",
            (record_id,),
        ).fetchall()
        self.assertEqual(len(chunks), 2)
        self.assertTrue(all(len(row["content"]) <= db.MAX_CHUNK_CHARS for row in chunks))
        vector = [0.1] * db.EMBED_DIM
        with mock.patch.object(db, "ensure_embed_server", return_value=True), mock.patch.object(
            db, "embed_texts", return_value=[vector, vector]
        ):
            result = db.backfill_embeddings(self.conn)
        self.assertEqual(result["embedded"], 2)
        stats = db.embedding_stats(self.conn)
        self.assertEqual(stats["memory_record_chunks"], 2)
        self.assertEqual(stats["memory_record_pending"], 0)
        with mock.patch.object(db, "ensure_embed_server", return_value=True), mock.patch.object(db, "embed_texts") as embed:
            second = db.backfill_embeddings(self.conn)
        self.assertEqual(second["candidates"], 0)
        embed.assert_not_called()

    def test_native_memory_record_rejects_wrong_embedding_dimension(self):
        db.create_memory_record(self.conn, "KNOWLEDGE-EMBED-DIM", "demo", "knowledge", "Native", "body", "current", "codex")
        with mock.patch.object(db, "ensure_embed_server", return_value=True), mock.patch.object(
            db, "embed_texts", return_value=[[0.1] * 3]
        ):
            result = db.backfill_embeddings(self.conn)
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(db.embedding_stats(self.conn)["memory_record_pending"], 1)

    def test_native_semantic_search_does_not_load_blobs_over_scan_limit(self):
        db.create_memory_record(self.conn, "KNOWLEDGE-EMBED-BOUND", "demo", "knowledge", "Native", "body", "current", "codex")
        self.conn.execute(
            "UPDATE memory_record_embeddings SET embedding = ?, embedding_hash = ?",
            (db.pack_embedding([0.1] * db.EMBED_DIM), db.embedding_hash("body")),
        )
        self.conn.commit()
        with mock.patch.object(db, "embed_texts") as embed:
            original = db.SEMANTIC_FULL_SCAN_LIMIT
            db.SEMANTIC_FULL_SCAN_LIMIT = 0
            try:
                self.assertEqual(db.semantic_memory_records(self.conn, "query", "demo", "knowledge", 10), [])
            finally:
                db.SEMANTIC_FULL_SCAN_LIMIT = original
        embed.assert_not_called()

    def test_native_semantic_match_on_history_ranks_the_current_lifecycle_head(self):
        audit_id = db.create_memory_record(
            self.conn, "AUDIT-SEMANTIC-HISTORY", "demo", "audit", "Old wording",
            "obsolete quasar failure signature", "open", "codex",
        )
        fix_id = db.create_memory_record(
            self.conn, "FIX-SEMANTIC-CURRENT", "demo", "fix", "Current truth",
            "replacement implementation", "current", "codex",
            links=[("resolves", audit_id, "fixed")],
        )
        for record_id, axis in ((audit_id, 0), (fix_id, 1)):
            row = self.conn.execute(
                "SELECT content FROM memory_record_embeddings WHERE record_id = ?", (record_id,),
            ).fetchone()
            vector = [0.0] * db.EMBED_DIM
            vector[axis] = 1.0
            self.conn.execute(
                "UPDATE memory_record_embeddings SET embedding = ?, embedding_hash = ? WHERE record_id = ?",
                (db.pack_embedding(vector), db.embedding_hash(row["content"]), record_id),
            )
        self.conn.commit()

        query_vector = [1.0] + [0.0] * (db.EMBED_DIM - 1)
        with mock.patch.object(db, "embed_texts", return_value=[query_vector]):
            results = db.semantic_memory_records(self.conn, "distant paraphrase", "demo", None, 5)
        self.assertEqual(results[0]["id"], fix_id)
        self.assertEqual(results[0]["matched_via_record_id"], audit_id)

    def test_native_semantic_above_scan_limit_reranks_lexical_history_candidates(self):
        audit_id = db.create_memory_record(
            self.conn, "AUDIT-SEMANTIC-BOUND", "demo", "audit", "Bounded old wording",
            "bounded lexical nebula", "open", "codex",
        )
        fix_id = db.create_memory_record(
            self.conn, "FIX-SEMANTIC-BOUND", "demo", "fix", "Bounded current truth",
            "replacement text", "current", "codex",
            links=[("resolves", audit_id, "fixed")],
        )
        row = self.conn.execute(
            "SELECT content FROM memory_record_embeddings WHERE record_id = ?", (audit_id,),
        ).fetchone()
        vector = [1.0] + [0.0] * (db.EMBED_DIM - 1)
        self.conn.execute(
            "UPDATE memory_record_embeddings SET embedding = ?, embedding_hash = ? WHERE record_id = ?",
            (db.pack_embedding(vector), db.embedding_hash(row["content"]), audit_id),
        )
        self.conn.commit()
        original = db.SEMANTIC_FULL_SCAN_LIMIT
        db.SEMANTIC_FULL_SCAN_LIMIT = 0
        try:
            with mock.patch.object(db, "embed_texts", return_value=[vector]):
                results = db.semantic_memory_records(self.conn, "bounded lexical nebula", "demo", None, 5)
        finally:
            db.SEMANTIC_FULL_SCAN_LIMIT = original
        self.assertEqual(results[0]["id"], fix_id)
        self.assertEqual(results[0]["matched_via_record_id"], audit_id)

    def test_native_semantic_bounded_fallback_caps_chunks_per_candidate_record(self):
        record_id = db.create_memory_record(
            self.conn, "KNOWLEDGE-SEMANTIC-MANY-CHUNKS", "demo", "knowledge",
            "Chunkbounded candidate", "chunkbounded " + "x" * (db.MAX_CHUNK_CHARS * 25),
            "current", "codex",
        )
        vector = [1.0] + [0.0] * (db.EMBED_DIM - 1)
        rows = self.conn.execute(
            "SELECT chunk_index, content FROM memory_record_embeddings WHERE record_id = ?",
            (record_id,),
        ).fetchall()
        self.assertGreater(len(rows), db.SEMANTIC_CHUNKS_PER_RECORD_LIMIT)
        for row in rows:
            self.conn.execute(
                "UPDATE memory_record_embeddings SET embedding = ?, embedding_hash = ? "
                "WHERE record_id = ? AND chunk_index = ?",
                (db.pack_embedding(vector), db.embedding_hash(row["content"]), record_id, row["chunk_index"]),
            )
        self.conn.commit()
        original = db.SEMANTIC_FULL_SCAN_LIMIT
        db.SEMANTIC_FULL_SCAN_LIMIT = 0
        try:
            with mock.patch.object(db, "embed_texts", return_value=[vector]), mock.patch.object(
                db, "unpack_embedding", wraps=db.unpack_embedding,
            ) as unpack:
                results = db.semantic_memory_records(self.conn, "chunkbounded", "demo", "knowledge", 5)
        finally:
            db.SEMANTIC_FULL_SCAN_LIMIT = original
        self.assertEqual(results[0]["id"], record_id)
        self.assertLessEqual(unpack.call_count, db.SEMANTIC_CHUNKS_PER_RECORD_LIMIT)

    def test_backfill_rejects_nonpositive_batch_size_before_starting_companion(self):
        with mock.patch.object(db, "ensure_embed_server") as ensure:
            with self.assertRaisesRegex(ValueError, "batch_size must be positive"):
                db.backfill_embeddings(self.conn, batch_size=-1)
        ensure.assert_not_called()
        with mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as invalid_cli:
                db.build_parser().parse_args(["embed-backfill", "--batch-size", "-1"])
        self.assertEqual(invalid_cli.exception.code, 2)

    def test_embedding_update_skips_content_changed_during_request(self):
        source = self.root / "race.md"
        source.write_text("# Race\n\nOriginal content.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        row = self.conn.execute("SELECT id FROM knowledge").fetchone()

        def change_content(_texts):
            writer = db.connect(self.database)
            try:
                writer.execute(
                    "UPDATE knowledge SET content = 'Changed during embedding.' WHERE id = ?",
                    (row["id"],),
                )
                writer.commit()
            finally:
                writer.close()
            return [[0.1] * db.EMBED_DIM]

        with mock.patch.object(db, "embed_texts", side_effect=change_content):
            result = db._embed_knowledge_rows_result(self.conn, [row["id"]])

        stored = self.conn.execute(
            "SELECT embedding, embedding_hash FROM knowledge WHERE id = ?", (row["id"],)
        ).fetchone()
        self.assertEqual(result["embedded"], 0)
        self.assertEqual(result["reason"], "content_changed_during_embedding")
        self.assertIsNone(stored["embedding"])
        self.assertEqual(stored["embedding_hash"], "")

    def test_out_of_float16_range_embedding_is_a_structured_failure(self):
        source = self.root / "overflow.md"
        source.write_text("# Overflow\n\nVector content.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        row_id = self.conn.execute("SELECT id FROM knowledge").fetchone()["id"]

        with mock.patch.object(
            db, "embed_texts", return_value=[[1e100] * db.EMBED_DIM]
        ):
            result = db._embed_knowledge_rows_result(self.conn, [row_id])

        self.assertEqual(result["embedded"], 0)
        self.assertEqual(result["reason"], "invalid_embedding_vector")
        self.assertIsNone(self.conn.execute(
            "SELECT embedding FROM knowledge WHERE id = ?", (row_id,)
        ).fetchone()["embedding"])

    def test_partial_embedding_response_is_not_reported_as_success(self):
        first = self.root / "first.md"
        second = self.root / "second.md"
        first.write_text("# First\n\nOne.\n", encoding="utf-8")
        second.write_text("# Second\n\nTwo.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, first, "demo", "project_memory", embed=False)
        db.ingest_markdown(self.conn, second, "demo", "project_memory", embed=False)
        ids = [row[0] for row in self.conn.execute("SELECT id FROM knowledge ORDER BY id")]
        original = db.embed_texts
        db.embed_texts = lambda _texts: [[0.1] * db.EMBED_DIM]
        try:
            self.assertEqual(db.embed_knowledge_rows(self.conn, ids), 0)
        finally:
            db.embed_texts = original
        stored = self.conn.execute("SELECT COUNT(*) FROM knowledge WHERE embedding IS NOT NULL").fetchone()[0]
        self.assertEqual(stored, 0)

    def test_backfill_reports_partial_request_failure_and_logs_batch(self):
        for name in ("first", "second"):
            source = self.root / f"{name}.md"
            source.write_text(f"# {name}\n\n{name} body\n", encoding="utf-8")
            db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        vector = [0.1] * db.EMBED_DIM
        with mock.patch.object(db, "ensure_embed_server", return_value=True), mock.patch.object(
            db, "embed_texts", side_effect=[[vector], None]
        ):
            result = db.backfill_embeddings(self.conn, batch_size=1)
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["embedded"], 1)
        self.assertEqual(result["reason"], "embedding_request_failed")
        detail = json.loads(self.conn.execute(
            "SELECT detail FROM activity_log WHERE action = 'embedding_batch_failed'"
        ).fetchone()[0])
        self.assertEqual(detail["attempts"], 1)
        self.assertEqual(detail["reason"], "embedding_request_failed")
        self.assertIn("started_at", detail)
        self.assertIn("finished_at", detail)

    def test_ingest_surfaces_embedding_request_failure_without_rolling_back_lexical_data(self):
        source = self.root / "request-failure.md"
        source.write_text("# Request failure\n\nLexical text survives.\n", encoding="utf-8")
        with mock.patch.object(db, "ensure_embed_server", return_value=True), mock.patch.object(
            db, "embed_texts", return_value=None
        ):
            result = db.ingest_markdown(self.conn, source, "demo", "project_memory")
        self.assertEqual(result["status"], "imported")
        self.assertEqual(result["embedded"], 0)
        self.assertEqual(result["embedding_warning"], "embedding_request_failed")
        self.assertTrue(db.search(self.conn, "Lexical text", "demo", None, 5))

    def test_corrupt_embedding_is_skipped_and_reported_as_pending(self):
        source = self.root / "corrupt.md"
        source.write_text("# Corrupt\n\nsemantic needle\n", encoding="utf-8")
        db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        self.conn.execute(
            "UPDATE knowledge SET embedding = ?, embedding_hash = ?",
            (b"bad", db.embedding_hash("semantic needle")),
        )
        self.conn.commit()
        original_ensure = db.ensure_embed_server
        original_embed = db.embed_texts
        db.ensure_embed_server = lambda **_kwargs: True
        db.embed_texts = lambda _texts: [[0.0] * db.EMBED_DIM]
        try:
            results = db.search(self.conn, "semantic needle", "demo", None, 5, semantic="on")
            stats = db.embedding_stats(self.conn)
        finally:
            db.ensure_embed_server = original_ensure
            db.embed_texts = original_embed
        self.assertEqual(len(results), 1)
        self.assertNotIn("semantic", results[0]["match_reasons"])
        self.assertEqual(stats["embedded"], 0)
        self.assertEqual(stats["invalid_blobs"], 1)
        self.assertEqual(stats["pending"], 1)

    def test_stale_embedding_hash_is_not_used_for_semantic_ranking(self):
        source = self.root / "stale-vector.md"
        source.write_text("# Stale\n\nstale semantic needle\n", encoding="utf-8")
        db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        vector = [0.0] * db.EMBED_DIM
        vector[0] = 1.0
        self.conn.execute(
            "UPDATE knowledge SET embedding = ?, embedding_hash = 'wrong'",
            (db.pack_embedding(vector),),
        )
        self.conn.commit()
        with mock.patch.object(db, "ensure_embed_server", return_value=True):
            with mock.patch.object(db, "embed_texts", return_value=[vector]):
                results = db.search(
                    self.conn, "stale semantic needle", "demo", None, 5, semantic="on"
                )
        self.assertNotIn("semantic", results[0]["match_reasons"])
        self.assertEqual(db.embedding_stats(self.conn)["stale_hashes"], 1)

    def test_semantic_search_is_exact_below_limit_and_bounded_above_it(self):
        lexical = self.root / "lexical.md"
        semantic = self.root / "semantic.md"
        lexical.write_text("# Lexical\n\nlexical needle present\n", encoding="utf-8")
        semantic.write_text(
            "# Semantic\n\ndistant concept without matching vocabulary\n", encoding="utf-8"
        )
        db.ingest_markdown(self.conn, lexical, "demo", "project_memory", embed=False)
        db.ingest_markdown(self.conn, semantic, "demo", "project_memory", embed=False)
        rows = self.conn.execute("SELECT id, title, content FROM knowledge").fetchall()
        target_id = next(row["id"] for row in rows if row["title"] == "Semantic")
        for row in rows:
            vector = [0.0] * db.EMBED_DIM
            vector[0 if row["id"] == target_id else 1] = 1.0
            self.conn.execute(
                "UPDATE knowledge SET embedding = ?, embedding_hash = ? WHERE id = ?",
                (db.pack_embedding(vector), db.embedding_hash(row["content"]), row["id"]),
            )
        self.conn.commit()

        with mock.patch.object(db, "ensure_embed_server", return_value=True):
            with mock.patch.object(
                db, "embed_texts", return_value=[[1.0] + [0.0] * (db.EMBED_DIM - 1)]
            ):
                original_limit = db.SEMANTIC_FULL_SCAN_LIMIT
                try:
                    db.SEMANTIC_FULL_SCAN_LIMIT = 20_000
                    exact = db.search(
                        self.conn, "lexical needle", "demo", None, 5, semantic="on"
                    )
                    db.SEMANTIC_FULL_SCAN_LIMIT = 0
                    bounded = db.search(
                        self.conn, "lexical needle", "demo", None, 5, semantic="on"
                    )
                finally:
                    db.SEMANTIC_FULL_SCAN_LIMIT = original_limit
        exact_target = next(item for item in exact if item["id"] == target_id)
        self.assertIn("semantic", exact_target["match_reasons"])
        self.assertNotIn(target_id, {item["id"] for item in bounded})

    def test_semantic_evaluation_probes_unavailable_companion_once(self):
        cases = self.root / "semantic-eval.json"
        cases.write_text(json.dumps([
            {"query": "first"}, {"query": "second"}, {"query": "third"},
        ]), encoding="utf-8")
        with mock.patch.object(db, "ensure_embed_server", return_value=False) as ensure:
            report = db.evaluate_queries(self.conn, cases, semantic="on")
        self.assertEqual(ensure.call_count, 1)
        self.assertFalse(report["semantic_available"])

    def test_embed_texts_returns_none_when_companion_unreachable(self):
        with mock.patch.object(db, "urlopen", side_effect=db.URLError("offline")):
            self.assertIsNone(db.embed_texts(["some knowledge chunk"]))

    def test_ensure_embed_server_without_wait_returns_false_and_never_spawns(self):
        with mock.patch.object(db, "_embed_health", return_value=None), mock.patch.object(
            db, "_spawn_embed_server"
        ) as spawn:
            self.assertFalse(db.ensure_embed_server(wait=False))
        spawn.assert_not_called()

    def test_embed_warm_mode_starts_companion_then_posts_explicit_mode(self):
        response = mock.MagicMock()
        response.read.return_value = b'{"status":"ready","keep_warm":true}'
        response.__enter__.return_value = response
        with mock.patch.object(db, "ensure_embed_server", return_value=True), mock.patch.object(
            db, "urlopen", return_value=response
        ) as open_url:
            result = db.set_embed_keep_warm(True)
        self.assertEqual(result, {"ready": True, "keep_warm": True})
        self.assertIn("warm-mode", open_url.call_args.args[0].full_url)

    def test_embed_warm_cli_is_read_only_and_forwards_keep_alive(self):
        with mock.patch.object(db, "set_embed_keep_warm", return_value={"ready": True, "keep_warm": True}) as warm:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = db.main(["--db", str(self.database), "embed-warm", "--keep-alive"])
        self.assertEqual(exit_code, 0)
        warm.assert_called_once_with(True)

    def test_embed_companion_ready_reflects_health_without_spawning(self):
        ready_health = {"status": "ready", "model": db.EMBED_MODEL_NAME, "dim": db.EMBED_DIM}
        with mock.patch.object(db, "_embed_health", return_value=ready_health), mock.patch.object(
            db, "_spawn_embed_server"
        ) as spawn:
            self.assertTrue(db.embed_companion_ready())
        spawn.assert_not_called()
        with mock.patch.object(db, "_embed_health", return_value=None):
            self.assertFalse(db.embed_companion_ready())
        with mock.patch.object(db, "_embed_health", return_value={"status": "loading"}):
            self.assertFalse(db.embed_companion_ready())

    def test_resolve_embed_python_skips_cli_interpreter_without_ml_dependencies(self):
        cli_python = self.root / "homebrew-python"
        companion_python = self.root / "endeavor-python"
        candidates = [cli_python, companion_python]
        with mock.patch.object(db, "_embed_python_candidates", return_value=candidates):
            with mock.patch.object(
                db, "_embed_python_has_dependencies", side_effect=lambda path: path == companion_python
            ) as probe:
                self.assertEqual(db.resolve_embed_python(), companion_python)
        self.assertEqual([call.args[0] for call in probe.call_args_list], candidates)

    def test_embed_python_environment_override_is_authoritative(self):
        override = self.root / "custom-python"
        with mock.patch.dict(db.os.environ, {db.EMBED_PYTHON_ENV: str(override)}):
            self.assertEqual(db._embed_python_candidates(), [override])

    def test_embed_diagnostics_never_mislabels_sandbox_denial_as_missing_dependencies(self):
        cli_python = self.root / "homebrew-python"
        companion_python = self.root / "conda-python"
        permission_error = {
            "type": "PermissionError",
            "errno": 1,
            "message": "[Errno 1] Operation not permitted",
        }
        with mock.patch.object(
            db, "_embed_python_candidates", return_value=[cli_python, companion_python]
        ):
            with mock.patch.object(
                db, "_embed_python_has_dependencies", side_effect=[False, True]
            ):
                with mock.patch.object(db, "_embed_health_probe", return_value=(None, permission_error)):
                    report = db.embedding_diagnostics()

        self.assertEqual(report["diagnosis"], "localhost_permission_denied")
        self.assertEqual(report["selected_companion_python"], str(companion_python))
        self.assertTrue(report["companion_candidates"][1]["has_dependencies"])
        self.assertIn("do not install packages", report["next_action"].lower())

    def test_embed_health_probe_preserves_nested_permission_error(self):
        denied = db.URLError(PermissionError(1, "Operation not permitted"))
        with mock.patch.object(db, "urlopen", side_effect=denied):
            health, error = db._embed_health_probe()
        self.assertIsNone(health)
        self.assertEqual(error, {
            "type": "PermissionError",
            "errno": 1,
            "message": "[Errno 1] Operation not permitted",
        })

    def test_embed_diagnose_cli_does_not_require_a_database_connection(self):
        report = {"diagnosis": "localhost_permission_denied"}
        output = io.StringIO()
        with mock.patch.object(db, "embedding_diagnostics", return_value=report):
            with mock.patch.object(db, "connect", side_effect=AssertionError("must not connect")):
                with redirect_stdout(output):
                    self.assertEqual(db.main(["embed-diagnose"]), 0)
        self.assertEqual(json.loads(output.getvalue()), report)

    def test_failed_backfill_always_returns_structured_diagnostics(self):
        report = {"diagnosis": "localhost_permission_denied"}
        with mock.patch.object(db, "ensure_embed_server", return_value=False):
            with mock.patch.object(db, "embed_failure_reason", return_value="not ready"):
                with mock.patch.object(db, "embedding_diagnostics", return_value=report):
                    result = db.backfill_embeddings(self.conn)
        self.assertEqual(result["status"], "companion_unavailable")
        self.assertEqual(result["diagnostics"], report)

    def test_resolve_embed_python_reports_lexical_fallback_when_none_is_usable(self):
        candidate = self.root / "python"
        with mock.patch.object(db, "_embed_python_candidates", return_value=[candidate]):
            with mock.patch.object(db, "_embed_python_has_dependencies", return_value=False):
                with self.assertRaisesRegex(OSError, "Lexical search remains available"):
                    db.resolve_embed_python()

    def test_spawn_embed_server_uses_resolved_companion_python(self):
        companion_python = self.root / "endeavor-python"
        original_log = db.EMBED_LOG_PATH
        db.EMBED_LOG_PATH = self.root / "embed.log"
        try:
            with mock.patch.object(db, "resolve_embed_python", return_value=companion_python):
                with mock.patch.object(db.subprocess, "Popen") as popen:
                    db._spawn_embed_server()
            command = popen.call_args.args[0]
            self.assertEqual(command, [str(companion_python), str(db.HERE / "embed_server.py")])
            self.assertIn(str(companion_python), db.EMBED_LOG_PATH.read_text(encoding="utf-8"))
        finally:
            db.EMBED_LOG_PATH = original_log

    def test_ensure_embed_server_rejects_wrong_model_or_dimension(self):
        original_health = db._embed_health
        original_spawn = db._spawn_embed_server
        original_lock = db.EMBED_START_LOCK_PATH
        db.EMBED_START_LOCK_PATH = self.root / "embed.lock"
        db._embed_health = lambda timeout=0: {"status": "ready", "model": "wrong-model", "dim": db.EMBED_DIM}
        db._spawn_embed_server = lambda: self.fail("must not spawn onto an incompatible occupied port")
        try:
            self.assertFalse(db.ensure_embed_server(wait=False))
            self.assertFalse(db.ensure_embed_server(wait=True, timeout=0.01))
            self.assertFalse(db.embedding_stats(self.conn)["companion_warm"])
        finally:
            db._embed_health = original_health
            db._spawn_embed_server = original_spawn
            db.EMBED_START_LOCK_PATH = original_lock

    def test_ensure_embed_server_rechecks_health_after_start_lock(self):
        original_health = db._embed_health
        original_spawn = db._spawn_embed_server
        original_lock = db.EMBED_START_LOCK_PATH
        db.EMBED_START_LOCK_PATH = self.root / "embed.lock"
        health_values = iter([
            None,
            {"status": "ready", "model": db.EMBED_MODEL_NAME, "dim": db.EMBED_DIM},
        ])
        db._embed_health = lambda timeout=0: next(health_values)
        db._spawn_embed_server = lambda: self.fail("must not spawn when another agent became ready")
        try:
            self.assertTrue(db.ensure_embed_server(wait=True))
        finally:
            db._embed_health = original_health
            db._spawn_embed_server = original_spawn
            db.EMBED_START_LOCK_PATH = original_lock

    def test_ensure_embed_server_stops_waiting_when_child_exits(self):
        original_health = db._embed_health
        original_spawn = db._spawn_embed_server
        original_lock = db.EMBED_START_LOCK_PATH
        db.EMBED_START_LOCK_PATH = self.root / "embed.lock"
        db._embed_health = lambda timeout=0: None

        class ExitedProcess:
            @staticmethod
            def poll():
                return 1

        db._spawn_embed_server = lambda: ExitedProcess()
        try:
            started = db.time.monotonic()
            self.assertFalse(db.ensure_embed_server(wait=True, timeout=30))
            self.assertLess(db.time.monotonic() - started, 1.0)
        finally:
            db._embed_health = original_health
            db._spawn_embed_server = original_spawn
            db.EMBED_START_LOCK_PATH = original_lock

    def test_search_semantic_auto_degrades_to_lexical_only_when_unreachable(self):
        source = self.root / "memory.md"
        source.write_text("# Project\n\n## Bug Fix\n\nFix cache race with one lock.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        results = db.search(self.conn, "cache race", "demo", None, 5, semantic="auto")
        self.assertEqual(len(results), 1)
        self.assertNotIn("semantic", results[0]["match_reasons"])

    def test_ingest_without_embed_leaves_embedding_columns_null(self):
        source = self.root / "memory.md"
        source.write_text("# Project\n\nSome content.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        row = self.conn.execute("SELECT embedding, embedding_hash FROM knowledge").fetchone()
        self.assertIsNone(row["embedding"])
        self.assertEqual(row["embedding_hash"], "")

    def test_unchanged_ingest_backfills_missing_embeddings_when_enabled(self):
        source = self.root / "memory.md"
        source.write_text("# Memory\n\nReusable knowledge.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        original_ensure = db.ensure_embed_server
        original_embed = db._embed_knowledge_rows_result
        calls = []
        db.ensure_embed_server = lambda **_kwargs: True
        db._embed_knowledge_rows_result = lambda _conn, ids: {
            "attempts": 1,
            "candidates": len(ids),
            "embedded": calls.append(ids) or len(ids),
            "started_at": "now",
            "finished_at": "now",
        }
        try:
            result = db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=True)
        finally:
            db.ensure_embed_server = original_ensure
            db._embed_knowledge_rows_result = original_embed
        self.assertEqual(result["status"], "unchanged")
        self.assertEqual(result["embedded"], 1)
        self.assertEqual(len(calls), 1)

    def test_embedding_stats_reports_coverage_without_spawning(self):
        source = self.root / "memory.md"
        source.write_text("# Project\n\nSome content.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        with mock.patch.object(db, "_embed_health", return_value=None):
            stats = db.embedding_stats(self.conn)
        self.assertEqual(stats["knowledge_rows"], 1)
        self.assertEqual(stats["embedded"], 0)
        self.assertFalse(stats["companion_warm"])

    def test_embedding_stats_requires_ready_health_status(self):
        original_health = db._embed_health
        db._embed_health = lambda timeout=0: {"status": "loading"}
        try:
            self.assertFalse(db.embedding_stats(self.conn)["companion_warm"])
        finally:
            db._embed_health = original_health

    def test_embed_failure_reason_returns_last_actionable_log_error(self):
        original_log = db.EMBED_LOG_PATH
        db.EMBED_LOG_PATH = self.root / "embed_server.log"
        db.EMBED_LOG_PATH.write_text("noise\nRuntimeError: model cache unavailable\n", encoding="utf-8")
        try:
            self.assertEqual(db.embed_failure_reason(), "RuntimeError: model cache unavailable")
        finally:
            db.EMBED_LOG_PATH = original_log

    # ── Agent-friendliness: compact results, staleness, pack, auto-files ────

    def test_compact_result_trims_fields_and_builds_location(self):
        item = {
            "id": 5, "title": "T", "project": "demo", "category": "debugging",
            "excerpt": "e", "match_reasons": ["all_terms"], "rank": 0.1,
            "source_kind": "markdown", "source_path": "foo/bar.md",
            "line_start": 10, "line_end": 12, "source_heading": "",
            "status": "open", "bug_id": "V2-1", "fts_rowid": 99, "metadata": {},
        }
        compact = db.compact_result(item)
        self.assertEqual(compact["location"], "foo/bar.md:10-12")
        self.assertNotIn("fts_rowid", compact)
        self.assertNotIn("metadata", compact)
        self.assertEqual(compact["bug_id"], "V2-1")
        self.assertEqual(compact["status"], "open")
        self.assertNotIn("stale", compact)  # falsy/absent fields are omitted, not sent as noise

    def test_compact_result_uses_source_heading_when_no_line_range(self):
        item = {
            "id": 1, "title": "T", "project": "demo", "category": "audit",
            "excerpt": "e", "match_reasons": [], "rank": 0.0, "source_kind": "sqlite",
            "source_path": "SQLite:memory_records", "source_heading": "AUDIT-1",
            "line_start": None, "line_end": None,
        }
        self.assertEqual(db.compact_result(item)["location"], "SQLite:memory_records:AUDIT-1")

    def test_annotate_staleness_flags_source_file_drifted_after_indexing(self):
        source = self.root / "memory.md"
        source.write_text("# Project\n\nOriginal searchable content.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)

        fresh = db.search_all(self.conn, "searchable content", "demo", None, 5)
        db.annotate_staleness(self.conn, fresh)
        self.assertFalse(fresh[0]["stale"])

        source.write_text("# Project\n\nOriginal searchable content, edited.\n", encoding="utf-8")
        drifted = db.search_all(self.conn, "searchable content", "demo", None, 5)
        db.annotate_staleness(self.conn, drifted)
        self.assertTrue(drifted[0]["stale"])

    def test_annotate_staleness_leaves_sqlite_native_records_alone(self):
        db.create_memory_record(
            self.conn, "AUDIT-STALE-1", "demo", "audit", "T", "some searchable body", "open", "codex",
        )
        results = db.search_all(self.conn, "searchable body", "demo", None, 5)
        db.annotate_staleness(self.conn, results)
        self.assertNotIn("stale", results[0])

    def test_query_cli_compact_and_check_stale_end_to_end(self):
        source = self.root / "memory.md"
        source.write_text("# Project\n\nSearchable content here.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        self.conn.commit()

        def run_query():
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = db.main([
                    "--db", str(self.database), "query", "searchable content",
                    "--project", "demo", "--json", "--compact", "--check-stale",
                ])
            self.assertEqual(exit_code, 0)
            return json.loads(stdout.getvalue())

        fresh = run_query()
        self.assertNotIn("stale", fresh[0])
        self.assertLessEqual(
            set(fresh[0]),
            {"id", "title", "project", "category", "excerpt", "match_reasons",
             "rank", "source_kind", "location", "bug_id", "status", "stale"},
        )

        source.write_text("# Project\n\nSearchable content here, edited.\n", encoding="utf-8")
        stale = run_query()
        self.assertTrue(stale[0]["stale"])

    def test_build_pack_includes_handoff_open_records_and_recent_knowledge(self):
        session_id = db.start_session(self.conn, "demo", "goal", "codex", {})
        session = db.resolve_session(self.conn, session_id, None)
        db.add_checkpoint(self.conn, session, "codex", {"summary": "did work", "status": "paused"})
        db.create_memory_record(
            self.conn, "AUDIT-PACK-1", "demo", "audit", "Open issue", "needs a fix", "open", "codex",
        )
        source = self.root / "memory.md"
        source.write_text("# Project\n\nRecent knowledge chunk.\n", encoding="utf-8")
        db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)

        pack = db.build_pack(self.conn, "demo")
        self.assertEqual(pack["session"]["id"], session_id)
        self.assertEqual(pack["checkpoint"]["summary"], "did work")
        self.assertEqual([r["id"] for r in pack["open_records"]], ["AUDIT-PACK-1"])
        self.assertTrue(any("Recent knowledge chunk" in k["excerpt"] for k in pack["knowledge"]))
        self.assertFalse(pack["truncated"])

    def test_build_pack_respects_budget_and_marks_truncated(self):
        for i in range(5):
            source = self.root / f"memory{i}.md"
            source.write_text(f"# Doc {i}\n\n" + ("word " * 200) + "\n", encoding="utf-8")
            db.ingest_markdown(self.conn, source, "demo", "project_memory", embed=False)
        pack = db.build_pack(self.conn, "demo", budget_chars=500)
        self.assertTrue(pack["truncated"])
        self.assertLess(len(pack["knowledge"]), 5)
        self.assertLessEqual(len(db.json_text(pack)), 500)
        self.assertGreater(pack["budget_omitted_counts"]["knowledge"], 0)

    def test_build_pack_budgets_actionable_records_and_reports_omissions(self):
        for index in range(20):
            db.create_memory_record(
                self.conn, f"AUDIT-PACK-BUDGET-{index}", "demo", "audit",
                f"Open issue {index}", "x" * 200, "open", "codex",
            )
        pack = db.build_pack(self.conn, "demo", budget_chars=500)
        self.assertLessEqual(len(db.json_text(pack)), 500)
        self.assertTrue(pack["truncated"])
        self.assertGreater(pack["budget_omitted_counts"]["actionable_records"], 0)

    def test_pending_worklist_uses_current_lifecycle_heads_and_keeps_sessions_separate(self):
        audit = db.create_memory_record(
            self.conn, "AUDIT-PENDING-OLD", "demo", "audit", "Historical audit", "fixed already", "open", "codex",
        )
        db.create_memory_record(
            self.conn, "FIX-PENDING-HEAD", "demo", "fix", "Implemented fix", "the new current head", "current", "codex",
            links=[("resolves", audit, "resolved")],
        )
        db.create_memory_record(
            self.conn, "VERIFY-PENDING-OPEN", "demo", "verification", "Live smoke pending", "needs live API", "open", "codex",
        )
        session_id = db.start_session(self.conn, "demo", "resume this", "codex", {})
        session = db.resolve_session(self.conn, session_id, None)
        db.add_checkpoint(self.conn, session, "codex", {"summary": "pause", "status": "paused", "next_steps": "resume"})

        pending = db.build_pending_worklist(self.conn, "demo")
        self.assertTrue(pending["complete"])
        self.assertEqual([item["id"] for item in pending["actionable_records"]], ["VERIFY-PENDING-OPEN"])
        self.assertEqual([item["id"] for item in pending["resumable_sessions"]], [session_id])
        self.assertTrue(pending["requires_user_selection"])
        self.assertEqual(pending["summary"]["historical_open_suppressed"], 1)

        pack = db.build_pack(self.conn, "demo")
        self.assertEqual({item["id"] for item in pack["open_records"]}, {"VERIFY-PENDING-OPEN", "AUDIT-PENDING-OLD"})
        self.assertEqual([item["id"] for item in pack["actionable_records"]], ["VERIFY-PENDING-OPEN"])
        self.assertEqual(pack["open_records_semantics"], "raw_stored_status")

    def test_pending_is_incomplete_when_health_detects_an_invalid_typed_relation(self):
        first = db.create_memory_record(
            self.conn, "AUDIT-PENDING-TYPE-A", "demo", "audit", "First", "first", "open", "codex",
        )
        second = db.create_memory_record(
            self.conn, "AUDIT-PENDING-TYPE-B", "demo", "audit", "Second", "second", "open", "codex",
        )
        self.conn.execute("DROP TRIGGER memory_relations_validate_bi")
        self.conn.execute(
            "INSERT INTO memory_relations(source_id, target_id, relation, note, created_by, created_at) "
            "VALUES(?, ?, 'resolves', '', 'legacy', ?)",
            (first, second, db.now_utc()),
        )
        self.conn.commit()
        self.assertEqual(db.memory_relation_health(self.conn)["invalid_typed_relations"], 1)
        pending = db.build_pending_worklist(self.conn, "demo")
        self.assertFalse(pending["complete"])
        self.assertEqual(pending["actionable_records"], [])
        self.assertTrue(pending["warnings"])

    def test_pending_cli_and_bootstrap_opt_in(self):
        db.create_memory_record(
            self.conn, "AUDIT-PENDING-CLI", "demo", "audit", "Open issue", "needs work", "open", "codex",
        )
        self.conn.commit()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = db.main(["--db", str(self.database), "pending", "--project", "demo", "--json"])
        self.assertEqual(exit_code, 0)
        self.assertEqual([item["id"] for item in json.loads(stdout.getvalue())["actionable_records"]], ["AUDIT-PENDING-CLI"])

        report = db.bootstrap(self.conn, "demo", include_pending=True)
        self.assertIn("pending", report)
        self.assertEqual([item["id"] for item in report["pending"]["actionable_records"]], ["AUDIT-PENDING-CLI"])

    def test_pending_active_session_is_not_labeled_blocked_and_rejects_blank_scope(self):
        session_id = db.start_session(self.conn, "demo", "active goal", "codex", {})
        session = db.resolve_session(self.conn, session_id, None)
        db.add_checkpoint(self.conn, session, "codex", {"summary": "working", "status": "active"})
        pending = db.build_pending_worklist(self.conn, "demo")
        self.assertEqual(
            pending["active_sessions"][0]["rank_reason"],
            "durable active session; presence is checked separately",
        )
        with self.assertRaises(SystemExit):
            db.build_parser().parse_args(["pending", "--project", ""])

    def test_pending_project_scope_does_not_count_other_project_history_and_pack_surfaces_incomplete(self):
        audit = db.create_memory_record(
            self.conn, "AUDIT-OTHER-OLD", "other", "audit", "Old", "old", "open", "codex",
        )
        db.create_memory_record(
            self.conn, "FIX-OTHER-HEAD", "other", "fix", "Fix", "fixed", "current", "codex",
            links=[("resolves", audit, "")],
        )
        self.assertEqual(db.build_pending_worklist(self.conn, "demo")["summary"]["historical_open_suppressed"], 0)
        incomplete = {"actionable_records": [], "complete": False, "warnings": ["broken lifecycle"]}
        with mock.patch.object(db, "build_pending_worklist", return_value=incomplete):
            pack = db.build_pack(self.conn, "demo")
        self.assertFalse(pack["pending_complete"])
        self.assertEqual(pack["pending_warnings"], ["broken lifecycle"])

    def test_pack_cli_json_returns_project_briefing(self):
        db.start_session(self.conn, "demo", "goal", "codex", {})
        self.conn.commit()
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = db.main(["--db", str(self.database), "pack", "--project", "demo", "--json"])
        self.assertEqual(exit_code, 0)
        data = json.loads(stdout.getvalue())
        self.assertEqual(data["project"], "demo")
        self.assertIn("open_records", data)

    def test_auto_detect_changed_files_returns_empty_for_unknown_project(self):
        self.assertEqual(db.auto_detect_changed_files("not-a-real-directory-xyz"), [])

    def test_auto_detect_changed_files_rejects_path_traversal_labels(self):
        # "." resolves to ROOT itself and would widen the pathspec to the
        # whole repository, breaking the scoping guarantee.
        for label in (".", "..", "", "a/b", "a\\b"):
            self.assertEqual(db.auto_detect_changed_files(label), [])

    def test_auto_detect_changed_files_keeps_non_ascii_filenames_unmangled(self):
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        project_dir = self.root / "demo-project"
        project_dir.mkdir()
        (project_dir / "ไฟล์ไทย.md").write_text("x\n", encoding="utf-8")
        with mock.patch.object(db, "ROOT", self.root):
            files = db.auto_detect_changed_files("demo-project")
        # Default core.quotepath would return a C-quoted octal escape like
        # "demo-project/\\340\\271\\204..." instead of the real UTF-8 path.
        self.assertEqual(files, ["demo-project/ไฟล์ไทย.md"])

    def test_auto_detect_changed_files_scopes_to_real_project_directory(self):
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        project_dir = self.root / "demo-project"
        project_dir.mkdir()
        (project_dir / "a.py").write_text("print(1)\n", encoding="utf-8")
        other_dir = self.root / "other-project"
        other_dir.mkdir()
        (other_dir / "b.py").write_text("print(2)\n", encoding="utf-8")
        with mock.patch.object(db, "ROOT", self.root):
            files = db.auto_detect_changed_files("demo-project")
        self.assertEqual(files, ["demo-project/a.py"])

    def test_load_payload_auto_files_requires_project(self):
        args = argparse.Namespace(
            payload=None, summary="s", work_done=None, current_state=None, next_steps=None,
            blockers=None, status=None, files_changed=None, commands_run=None, verification=None,
            auto_files=True, project=None,
        )
        with self.assertRaisesRegex(ValueError, "--auto-files requires --project"):
            db.load_payload(args)

    def test_load_payload_auto_files_merges_with_explicit_files_deduped(self):
        args = argparse.Namespace(
            payload=None, summary="s", work_done=None, current_state=None, next_steps=None,
            blockers=None, status=None, files_changed=["explicit.py"], commands_run=None, verification=None,
            auto_files=True, project="demo",
        )
        with mock.patch.object(db, "auto_detect_changed_files", return_value=["explicit.py", "auto.py"]):
            payload = db.load_payload(args)
        self.assertEqual(payload["files_changed"], ["explicit.py", "auto.py"])

    def test_load_payload_pin_overrides_malformed_payload_before_auto_files_validation(self):
        payload_file = self.root / "pin-override.json"
        payload_file.write_text(
            json.dumps({"summary": "s", "pinned": "malformed"}),
            encoding="utf-8",
        )
        args = argparse.Namespace(
            payload=str(payload_file), summary=None, work_done=None, current_state=None,
            next_steps=None, blockers=None, status=None, files_changed=None,
            commands_run=None, verification=None, auto_files=True, project="demo", pin=True,
        )
        with mock.patch.object(db, "auto_detect_changed_files", return_value=["auto.py"]):
            payload = db.load_payload(args)
        self.assertIs(payload["pinned"], True)
        self.assertEqual(payload["files_changed"], ["auto.py"])

    def test_add_checkpoint_rejects_malformed_programmatic_evidence_before_write(self):
        session_id = db.start_session(self.conn, "demo", "goal", "codex", {})
        session = db.resolve_session(self.conn, session_id, None)
        with self.assertRaisesRegex(ValueError, "files_changed.*array of strings"):
            db.add_checkpoint(
                self.conn, session, "codex",
                {"summary": "s", "files_changed": [123]},
            )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE session_id = ?", (session_id,)
            ).fetchone()[0],
            0,
        )

    def test_checkpoint_payload_rejects_invalid_status_before_auto_start(self):
        payload_file = self.root / "invalid-status.json"
        payload_file.write_text(json.dumps({"summary": "s", "status": "bogus"}), encoding="utf-8")
        self.conn.commit()
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", stderr):
            exit_code = db.main([
                "--db", str(self.database), "checkpoint", "--project", "invalid-status-demo",
                "--goal", "goal", "--agent", "claude", "--payload", str(payload_file),
            ])
        self.assertEqual(exit_code, 1)
        self.assertIn("payload.status", stderr.getvalue())
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sessions WHERE project = ?", ("invalid-status-demo",)).fetchone()[0],
            0,
        )

    def test_checkpoint_payload_rejects_non_string_evidence_before_auto_start(self):
        for field in ("files_changed", "commands_run", "verification"):
            with self.subTest(field=field):
                payload_file = self.root / f"invalid-{field}.json"
                payload_file.write_text(json.dumps({"summary": "s", field: [123]}), encoding="utf-8")
                self.conn.commit()
                stderr = io.StringIO()
                with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", stderr):
                    exit_code = db.main([
                        "--db", str(self.database), "checkpoint", "--project", f"invalid-{field}",
                        "--goal", "goal", "--agent", "claude", "--payload", str(payload_file),
                    ])
                self.assertEqual(exit_code, 1)
                self.assertIn(f"payload.{field}", stderr.getvalue())
                self.assertEqual(
                    self.conn.execute("SELECT COUNT(*) FROM sessions WHERE project = ?", (f"invalid-{field}",)).fetchone()[0],
                    0,
                )

    def test_checkpoint_payload_must_be_an_object(self):
        payload_file = self.root / "array-payload.json"
        payload_file.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
        self.conn.commit()
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", stderr):
            exit_code = db.main([
                "--db", str(self.database), "checkpoint", "--project", "array-payload-demo",
                "--goal", "goal", "--agent", "claude", "--payload", str(payload_file),
            ])
        self.assertEqual(exit_code, 1)
        self.assertIn("must be a JSON object", stderr.getvalue())
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sessions WHERE project = ?", ("array-payload-demo",)).fetchone()[0],
            0,
        )

    def test_two_agents_cannot_both_auto_start_a_checkpoint_session(self):
        barrier = threading.Barrier(2)
        sessions: list[str] = []
        errors: list[Exception] = []

        def starter(agent):
            connection = db.connect(self.database)
            try:
                barrier.wait(timeout=2)
                session = db.resolve_or_start_checkpoint_session(
                    connection, None, "race-demo", "shared goal", agent,
                )
                sessions.append(session["id"])
            except Exception as exc:  # noqa: BLE001 — surfaced via assertFalse(errors) below
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=starter, args=(agent,)) for agent in ("codex", "claude")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

        self.assertFalse(errors)
        self.assertEqual(len(set(sessions)), 1)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE project = ?", ("race-demo",)
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_checkpoint_cli_payload_error_does_not_strand_an_auto_started_session(self):
        self.conn.commit()
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", stderr):
            exit_code = db.main([
                "--db", str(self.database), "checkpoint",
                "--project", "no-summary-demo", "--goal", "goal", "--agent", "claude",
            ])
        self.assertEqual(exit_code, 1)
        self.assertIn("summary", stderr.getvalue())
        count = self.conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE project = ?", ("no-summary-demo",)
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_maintenance_cli_requires_yes_then_vacuums(self):
        self.conn.commit()
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", stderr):
            exit_code = db.main(["--db", str(self.database), "maintenance"])
        self.assertEqual(exit_code, 1)
        self.assertIn("--yes", stderr.getvalue())

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = db.main(["--db", str(self.database), "maintenance", "--yes"])
        self.assertEqual(exit_code, 0)
        result = json.loads(stdout.getvalue())
        self.assertIn("reclaimed", result)

    def test_agent_help_cli_prints_without_touching_database(self):
        missing_db = self.root / "does-not-exist" / "nope.sqlite3"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            exit_code = db.main(["--db", str(missing_db), "agent-help"])
        self.assertEqual(exit_code, 0)
        help_text = stdout.getvalue()
        self.assertIn("bootstrap --project", help_text)
        self.assertIn("writable SQLite database local to one host", help_text)
        self.assertNotIn("[read-only-", help_text)
        self.assertNotIn("confirm with the user", help_text)
        self.assertFalse(missing_db.parent.exists())

    # ── activity --follow: cross-agent visibility polling ───────────────────

    def test_poll_activity_since_returns_only_new_rows_oldest_first(self):
        db.start_session(self.conn, "demo", "goal", "codex", {})
        first_id = self.conn.execute("SELECT MAX(id) FROM activity_log").fetchone()[0]
        db.create_memory_record(self.conn, "AUDIT-POLL-1", "demo", "audit", "T", "body", "open", "claude")
        db.create_memory_record(self.conn, "AUDIT-POLL-2", "other", "audit", "T", "body", "open", "claude")

        rows = db.poll_activity_since(self.conn, first_id)
        self.assertEqual([r["action"] for r in rows], ["memory_record_add", "memory_record_add"])
        self.assertLess(rows[0]["id"], rows[1]["id"])
        self.assertEqual(db.poll_activity_since(self.conn, rows[-1]["id"]), [])

    def test_poll_activity_since_filters_by_project(self):
        first_id = self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM activity_log").fetchone()[0]
        db.create_memory_record(self.conn, "AUDIT-POLL-3", "demo", "audit", "T", "body", "open", "claude")
        db.create_memory_record(self.conn, "AUDIT-POLL-4", "other", "audit", "T", "body", "open", "claude")

        rows = db.poll_activity_since(self.conn, first_id, project="demo")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["project"], "demo")

    def test_activity_line_matches_render_activity_wording(self):
        db.create_memory_record(self.conn, "AUDIT-POLL-5", "demo", "audit", "My Title", "body", "open", "claude")
        row = self.conn.execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT 1").fetchone()
        line = db.activity_line(self.conn, row)
        digest = db.render_activity(self.conn, 1)
        self.assertIn(line, digest)

    def test_activity_follow_cli_flag_accepted_by_parser(self):
        args = db.build_parser().parse_args(["activity", "--follow", "--project", "demo", "--interval", "0.5"])
        self.assertTrue(args.follow)
        self.assertEqual(args.project, "demo")
        self.assertEqual(args.interval, 0.5)

    # ── activity_log retention: the sliding window Codex-generated writes need ──

    def test_prune_activity_log_keeps_only_newest_rows(self):
        for i in range(5):
            db.start_session(self.conn, "demo", f"goal {i}", "codex", {})
        all_ids_before = [
            row[0] for row in self.conn.execute("SELECT id FROM activity_log ORDER BY id")
        ]
        original_limit = db.MAX_ACTIVITY_LOG_ROWS
        db.MAX_ACTIVITY_LOG_ROWS = 3
        try:
            removed = db.prune_activity_log(self.conn)
        finally:
            db.MAX_ACTIVITY_LOG_ROWS = original_limit
        remaining = [row[0] for row in self.conn.execute("SELECT id FROM activity_log ORDER BY id")]
        self.assertEqual(removed, len(all_ids_before) - 3)
        # Kept rows are the newest (highest id), not an arbitrary 3.
        self.assertEqual(remaining, all_ids_before[-3:])

    def test_prune_activity_log_is_a_noop_under_the_cap(self):
        db.start_session(self.conn, "demo", "goal", "codex", {})
        before = self.conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0]
        removed = db.prune_activity_log(self.conn)
        after = self.conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0]
        self.assertEqual(removed, 0)
        self.assertEqual(before, after)

    def test_refresh_activity_export_prunes_activity_log(self):
        for i in range(5):
            db.start_session(self.conn, "demo", f"goal {i}", "codex", {})
        original_limit = db.MAX_ACTIVITY_LOG_ROWS
        db.MAX_ACTIVITY_LOG_ROWS = 2
        try:
            db.refresh_activity_export(self.conn, self.database)
        finally:
            db.MAX_ACTIVITY_LOG_ROWS = original_limit
        count = self.conn.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0]
        self.assertEqual(count, 2)

    def test_presence_start_heartbeat_stop_local_roundtrip(self):
        # Each call stands in for a SEPARATE CLI subprocess (the real calling
        # pattern) -- no pid is threaded between them, only (machine, agent,
        # project). If identity secretly depended on pid, heartbeat/stop below
        # would silently no-op exactly like the CLI smoke test caught pre-fix.
        presence_dir = self.root / ".presence"
        with mock.patch.object(db, "PRESENCE_DIR", presence_dir), mock.patch.object(
            db, "local_machine", return_value="machineA"
        ):
            db.presence_start(self.conn, "machineA", "claude", "demo", "writing tests")
            listing = db.list_presence(self.conn)
            self.assertEqual(len(listing["local"]), 1)
            self.assertEqual(listing["local"][0]["task"], "writing tests")
            self.assertFalse(listing["local"][0]["stale"])
            self.assertEqual(listing["remote"], [])

            updated = db.presence_heartbeat(self.conn, "machineA", "claude", "demo", task="running suite")
            self.assertEqual(updated, 1)
            listing = db.list_presence(self.conn)
            self.assertEqual(listing["local"][0]["task"], "running suite")

            stopped = db.presence_stop(self.conn, "machineA", "claude", "demo")
            self.assertEqual(stopped, 1)
            listing = db.list_presence(self.conn)
            self.assertEqual(listing["local"], [])

            sidecar = presence_dir / "machineA.json"
            first_sidecar = sidecar.read_bytes()
            with mock.patch.object(db, "_write_presence_sidecar") as write_sidecar:
                second_stop = db.presence_stop(self.conn, "machineA", "claude", "demo")
            write_sidecar.assert_not_called()
            self.assertEqual(second_stop, 0)
            self.assertEqual(sidecar.read_bytes(), first_sidecar)
            self.assertEqual(self.conn.execute(
                "SELECT COUNT(*) FROM activity_log WHERE action = 'presence_stop'"
            ).fetchone()[0], 1)

    def test_presence_start_upserts_same_agent_project(self):
        presence_dir = self.root / ".presence"
        with mock.patch.object(db, "PRESENCE_DIR", presence_dir), mock.patch.object(
            db, "local_machine", return_value="machineA"
        ):
            db.presence_start(self.conn, "machineA", "claude", "demo", "first task")
            db.presence_start(self.conn, "machineA", "claude", "demo", "second task")
            rows = self.conn.execute(
                "SELECT COUNT(*) FROM agent_presence WHERE machine='machineA' AND agent='claude' AND project='demo'"
            ).fetchone()[0]
            self.assertEqual(rows, 1)
            listing = db.list_presence(self.conn)
            self.assertEqual(listing["local"][0]["task"], "second task")

    def test_presence_instance_disambiguates_two_concurrent_agents(self):
        presence_dir = self.root / ".presence"
        with mock.patch.object(db, "PRESENCE_DIR", presence_dir), mock.patch.object(
            db, "local_machine", return_value="machineA"
        ):
            db.presence_start(self.conn, "machineA", "claude", "demo", "task 1", instance="a")
            db.presence_start(self.conn, "machineA", "claude", "demo", "task 2", instance="b")
            listing = db.list_presence(self.conn)
            self.assertEqual(len(listing["local"]), 2)
            self.assertEqual(
                {row["instance"] for row in listing["local"]}, {"a", "b"},
            )
            stopped = db.presence_stop(self.conn, "machineA", "claude", "demo", instance="a")
            self.assertEqual(stopped, 1)
            listing = db.list_presence(self.conn)
            self.assertEqual(len(listing["local"]), 1)
            self.assertEqual(listing["local"][0]["instance"], "b")

    def test_presence_heartbeat_on_unknown_identity_is_a_noop(self):
        presence_dir = self.root / ".presence"
        with mock.patch.object(db, "PRESENCE_DIR", presence_dir):
            updated = db.presence_heartbeat(self.conn, "machineA", "claude", "never-started")
            self.assertEqual(updated, 0)

    def test_presence_sidecar_is_written_and_never_reads_own_machine_back(self):
        presence_dir = self.root / ".presence"
        with mock.patch.object(db, "PRESENCE_DIR", presence_dir):
            db.presence_start(self.conn, "machineA", "claude", "demo", "task")
            sidecar = presence_dir / "machineA.json"
            self.assertTrue(sidecar.exists())
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            self.assertEqual(payload["machine"], "machineA")
            self.assertEqual(len(payload["agents"]), 1)

            # A process ON machineA must never read its own sidecar back as
            # "remote" -- the live table row is already authoritative for it.
            with mock.patch.object(db, "local_machine", return_value="machineA"):
                listing = db.list_presence(self.conn)
            self.assertEqual(listing["remote"], [])

    def test_presence_lists_other_machines_sidecar_as_remote_and_flags_stale(self):
        presence_dir = self.root / ".presence"
        presence_dir.mkdir(parents=True)
        stale_heartbeat = (datetime.now(timezone.utc) - timedelta(seconds=db.PRESENCE_STALE_SEC + 60)).isoformat(timespec="seconds")
        fresh_heartbeat = datetime.now(timezone.utc).isoformat(timespec="seconds")
        (presence_dir / "machineB.json").write_text(json.dumps({
            "machine": "machineB",
            "updated_at": fresh_heartbeat,
            "agents": [
                {
                    "machine": "machineB", "agent": "codex", "pid": 222, "project": "demo",
                    "task": "old task", "status": "active",
                    "started_at": stale_heartbeat, "last_heartbeat": stale_heartbeat,
                },
            ],
        }), encoding="utf-8")
        with mock.patch.object(db, "PRESENCE_DIR", presence_dir), mock.patch.object(
            db, "local_machine", return_value="machineA"
        ):
            listing = db.list_presence(self.conn)
        self.assertEqual(listing["machine"], "machineA")
        self.assertEqual(len(listing["remote"]), 1)
        self.assertEqual(listing["remote"][0]["machine"], "machineB")
        self.assertEqual(listing["remote"][0]["source"], "sidecar")
        self.assertTrue(listing["remote"][0]["stale"])

    def test_presence_read_survives_malformed_sidecar_files(self):
        # Regression coverage for a Sol-audited finding: earlier code only
        # caught (OSError, JSONDecodeError), so a structurally malformed
        # sidecar (missing keys, wrong container type, unparsable timestamp)
        # crashed the entire `presence` command instead of just being skipped.
        presence_dir = self.root / ".presence"
        presence_dir.mkdir(parents=True)
        fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
        good_entry = {
            "machine": "machineC", "agent": "codex", "pid": 1, "project": "demo",
            "task": "ok", "status": "active", "started_at": fresh, "last_heartbeat": fresh,
        }
        (presence_dir / "missing_updated_at.json").write_text(
            json.dumps({"machine": "missing_updated_at", "agents": [good_entry]}), encoding="utf-8"
        )
        (presence_dir / "not_a_dict.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        (presence_dir / "agents_not_a_list.json").write_text(
            json.dumps({"machine": "agents_not_a_list", "updated_at": fresh, "agents": "oops"}), encoding="utf-8"
        )
        (presence_dir / "bad_entry_timestamp.json").write_text(json.dumps({
            "machine": "bad_entry_timestamp", "updated_at": fresh,
            "agents": [{**good_entry, "last_heartbeat": "not-a-timestamp"}],
        }), encoding="utf-8")
        (presence_dir / "entry_missing_key.json").write_text(json.dumps({
            "machine": "entry_missing_key", "updated_at": fresh,
            "agents": [{k: v for k, v in good_entry.items() if k != "last_heartbeat"}],
        }), encoding="utf-8")
        # A conflicted/renamed copy: filename stem disagrees with the
        # payload's own declared machine identity.
        (presence_dir / "machineD.json").write_text(json.dumps({
            "machine": "machineE", "updated_at": fresh, "agents": [good_entry],
        }), encoding="utf-8")
        # The only file that should actually survive and produce a result.
        (presence_dir / "machineF.json").write_text(json.dumps({
            "machine": "machineF", "updated_at": fresh, "agents": [good_entry],
        }), encoding="utf-8")

        with mock.patch.object(db, "PRESENCE_DIR", presence_dir), mock.patch.object(
            db, "local_machine", return_value="machineA"
        ):
            listing = db.list_presence(self.conn)  # must not raise
        self.assertEqual(len(listing["remote"]), 1)
        self.assertEqual(listing["remote"][0]["machine"], "machineC")

    def test_presence_gracefully_degrades_on_v5_database_without_agent_presence_table(self):
        legacy_db = self.root / "legacy_v5.sqlite3"
        legacy = sqlite3.connect(legacy_db)
        legacy.executescript("""
            CREATE TABLE database_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
            INSERT INTO database_meta VALUES ('schema_version', '5', 'old');
        """)
        legacy.commit()
        legacy.close()
        conn = sqlite3.connect(legacy_db)
        conn.row_factory = sqlite3.Row
        try:
            listing = db.list_presence(conn)  # must not raise OperationalError
        finally:
            conn.close()
        self.assertEqual(listing["local"], [])

    def test_presence_start_prunes_any_row_older_than_three_days(self):
        presence_dir = self.root / ".presence"
        with mock.patch.object(db, "PRESENCE_DIR", presence_dir), mock.patch.object(
            db, "local_machine", return_value="machineA"
        ):
            db.presence_start(self.conn, "machineA", "claude", "demo", "old", instance="old-instance")
            db.presence_stop(self.conn, "machineA", "claude", "demo", instance="old-instance")
            old_cutoff = (datetime.now(timezone.utc) - timedelta(days=db.PRESENCE_ROW_MAX_AGE_DAYS + 1)).isoformat(timespec="seconds")
            self.conn.execute(
                "UPDATE agent_presence SET last_heartbeat = ? WHERE machine='machineA' AND instance='old-instance'",
                (old_cutoff,),
            )
            self.conn.commit()

            db.presence_start(self.conn, "machineA", "claude", "demo", "recent", instance="recent-instance")
            db.presence_stop(self.conn, "machineA", "claude", "demo", instance="recent-instance")

            db.presence_start(self.conn, "machineA", "claude", "demo", "still active", instance="active-instance")
            self.conn.execute(
                "UPDATE agent_presence SET last_heartbeat = ? WHERE machine='machineA' AND instance='active-instance'",
                (old_cutoff,),
            )
            self.conn.commit()

            # This call's internal prune should sweep old stopped and crashed-active rows.
            db.presence_start(self.conn, "machineA", "claude", "other-project", "trigger prune")

            remaining = {
                row["instance"] for row in self.conn.execute(
                    "SELECT instance FROM agent_presence WHERE machine='machineA'"
                ).fetchall()
            }
        self.assertNotIn("old-instance", remaining)
        self.assertNotIn("active-instance", remaining)
        self.assertIn("recent-instance", remaining)

    def test_presence_sidecar_writes_serialize_across_concurrent_local_publishers(self):
        # mock.patch.object's enter/exit save-and-restore is not safe to run
        # concurrently from multiple threads on the SAME target: one thread's
        # exit can restore the real module attribute while another thread is
        # still relying on the patched value, leaking real writes onto disk
        # (this bit once already -- caught real files landing in the actual
        # ENDMEMEX/.presence/ directory). Patch once, outside the threads,
        # and restore once at the end instead.
        presence_dir = self.root / ".presence"
        errors: list[Exception] = []
        original_presence_dir = db.PRESENCE_DIR
        original_local_machine = db.local_machine
        db.PRESENCE_DIR = presence_dir
        db.local_machine = lambda: "machineA"
        try:
            def publish(task: str) -> None:
                conn = db.connect(self.database)
                try:
                    db.presence_start(conn, "machineA", "claude", "demo", task, instance=task)
                except Exception as exc:  # pragma: no cover - surfaced via errors list
                    errors.append(exc)
                finally:
                    conn.close()

            threads = [threading.Thread(target=publish, args=(f"t{i}",)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
        finally:
            db.PRESENCE_DIR = original_presence_dir
            db.local_machine = original_local_machine
        self.assertEqual(errors, [])
        sidecar = presence_dir / "machineA.json"
        self.assertTrue(sidecar.exists())
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        # The final published snapshot must reflect ALL committed writers, not
        # a stale interleaved partial write from the unlocked temp-path race.
        self.assertEqual(len(payload["agents"]), 8)

    def test_presence_project_filter_applies_to_local_and_remote(self):
        presence_dir = self.root / ".presence"
        presence_dir.mkdir(parents=True)
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        (presence_dir / "machineB.json").write_text(json.dumps({
            "machine": "machineB", "updated_at": timestamp,
            "agents": [
                {
                    "machine": "machineB", "agent": "codex", "pid": 222, "project": "other-project",
                    "task": "x", "status": "active", "started_at": timestamp, "last_heartbeat": timestamp,
                },
            ],
        }), encoding="utf-8")
        with mock.patch.object(db, "PRESENCE_DIR", presence_dir), mock.patch.object(
            db, "local_machine", return_value="machineA"
        ):
            db.presence_start(self.conn, "machineA", "claude", "demo", "task")
            listing = db.list_presence(self.conn, project="demo")
        self.assertEqual(len(listing["local"]), 1)
        self.assertEqual(listing["remote"], [])

    def test_presence_cli_start_heartbeat_stop_and_list(self):
        # Each db.main(...) call below is its own fresh argparse Namespace
        # (a different os.getpid() in real life, same process here since it's
        # in-process) -- deliberately never threading --pid between calls, so
        # this exercises the actual (machine, agent, project) identity path a
        # real separate-subprocess CLI caller relies on.
        presence_dir = self.root / ".presence"
        with mock.patch.object(db, "PRESENCE_DIR", presence_dir), mock.patch.object(
            db, "database_path", return_value=self.database
        ), redirect_stdout(io.StringIO()) as out:
            rc = db.main([
                "presence-start", "--agent", "claude", "--project", "demo", "--task", "cli test",
            ])
            self.assertEqual(rc, 0)
        started = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(started["project"], "demo")

        with mock.patch.object(db, "PRESENCE_DIR", presence_dir), mock.patch.object(
            db, "database_path", return_value=self.database
        ), redirect_stdout(io.StringIO()) as out:
            rc = db.main([
                "presence-heartbeat", "--agent", "claude", "--project", "demo", "--task", "still going",
            ])
            self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue().strip())["updated"], True)

        with mock.patch.object(db, "PRESENCE_DIR", presence_dir), mock.patch.object(
            db, "database_path", return_value=self.database
        ), redirect_stdout(io.StringIO()) as out:
            rc = db.main(["presence", "--json"])
            self.assertEqual(rc, 0)
        listing = json.loads(out.getvalue().strip())
        self.assertEqual(len(listing["local"]), 1)
        self.assertEqual(listing["local"][0]["task"], "still going")

        with mock.patch.object(db, "PRESENCE_DIR", presence_dir), mock.patch.object(
            db, "database_path", return_value=self.database
        ), redirect_stdout(io.StringIO()) as out:
            rc = db.main(["presence-stop", "--agent", "claude", "--project", "demo"])
            self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue().strip())["updated"], True)

        with mock.patch.object(db, "PRESENCE_DIR", presence_dir), mock.patch.object(
            db, "database_path", return_value=self.database
        ), mock.patch.object(db, "_write_presence_sidecar") as presence_sidecar, mock.patch.object(
            db, "refresh_activity_export"
        ) as activity_export, mock.patch.object(
            db, "write_sync_freshness_signal"
        ) as freshness_signal, redirect_stdout(io.StringIO()) as out:
            rc = db.main(["presence-stop", "--agent", "claude", "--project", "demo"])
            self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue().strip())["updated"], False)
        presence_sidecar.assert_not_called()
        activity_export.assert_not_called()
        freshness_signal.assert_not_called()

    def test_write_sync_freshness_signal_roundtrip(self):
        freshness_dir = self.root / ".sync_freshness"
        with mock.patch.object(db, "SYNC_FRESHNESS_DIR", freshness_dir):
            db.write_sync_freshness_signal("machineA", "checkpoint")
            report = db.sync_freshness_report("machineB")
        self.assertIn("machineA", report["machines"])
        entry = report["machines"]["machineA"]
        self.assertEqual(entry["last_command"], "checkpoint")
        self.assertFalse(entry["is_local"])
        self.assertLess(entry["age_seconds"], 5)

    def test_sync_freshness_report_flags_local_machine(self):
        freshness_dir = self.root / ".sync_freshness"
        with mock.patch.object(db, "SYNC_FRESHNESS_DIR", freshness_dir):
            db.write_sync_freshness_signal("machineA", "checkpoint")
            report = db.sync_freshness_report("machineA")
        self.assertTrue(report["machines"]["machineA"]["is_local"])

    def test_sync_freshness_report_empty_when_no_signals_yet(self):
        freshness_dir = self.root / ".sync_freshness"
        with mock.patch.object(db, "SYNC_FRESHNESS_DIR", freshness_dir):
            report = db.sync_freshness_report("machineA")
        self.assertEqual(report["machines"], {})

    def test_write_commands_emit_a_sync_freshness_signal_but_reads_do_not(self):
        freshness_dir = self.root / ".sync_freshness"
        with mock.patch.object(db, "SYNC_FRESHNESS_DIR", freshness_dir), mock.patch.object(
            db, "database_path", return_value=self.database
        ), redirect_stdout(io.StringIO()):
            db.main(["session-start", "--project", "demo", "--goal", "g", "--agent", "claude"])
        self.assertTrue((freshness_dir / f"{db.local_machine()}.json").exists())
        payload = json.loads((freshness_dir / f"{db.local_machine()}.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["last_command"], "session-start")

        freshness_dir_2 = self.root / ".sync_freshness_2"
        with mock.patch.object(db, "SYNC_FRESHNESS_DIR", freshness_dir_2), mock.patch.object(
            db, "database_path", return_value=self.database
        ), redirect_stdout(io.StringIO()):
            db.main(["stats"])
        self.assertFalse(freshness_dir_2.exists())

    def test_sync_status_cli_reports_json(self):
        freshness_dir = self.root / ".sync_freshness"
        with mock.patch.object(db, "SYNC_FRESHNESS_DIR", freshness_dir):
            db.write_sync_freshness_signal("machineB", "record-add")
        with mock.patch.object(db, "SYNC_FRESHNESS_DIR", freshness_dir), mock.patch.object(
            db, "database_path", return_value=self.database
        ), mock.patch.object(db, "local_machine", return_value="machineA"), redirect_stdout(io.StringIO()) as out:
            rc = db.main(["sync-status", "--json"])
            self.assertEqual(rc, 0)
        report = json.loads(out.getvalue().strip())
        self.assertEqual(report["machine"], "machineA")
        self.assertIn("machineB", report["machines"])

    def test_sync_status_survives_malformed_freshness_files(self):
        # Deliberate mirror of test_presence_read_survives_malformed_sidecar_files:
        # these sidecars are the same untrusted cross-machine input class, so
        # sync_freshness_report must degrade to "skip that one file" too. It
        # previously caught only (OSError, JSONDecodeError) while dereferencing
        # payload["last_write_at"]/["machine"] outside the try, so ONE bad file
        # raised KeyError/TypeError/ValueError and took every valid sibling
        # down with it.
        freshness_dir = self.root / ".sync_freshness"
        freshness_dir.mkdir(parents=True)
        fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
        (freshness_dir / "missing_key.json").write_text(
            json.dumps({"machine": "missing_key"}), encoding="utf-8"
        )
        (freshness_dir / "not_a_dict.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        (freshness_dir / "bad_timestamp.json").write_text(json.dumps({
            "machine": "bad_timestamp", "last_write_at": "not-a-timestamp",
        }), encoding="utf-8")
        (freshness_dir / "not_json.json").write_text("{{{", encoding="utf-8")
        # A conflicted/renamed copy: filename stem disagrees with the payload's
        # own declared machine identity.
        (freshness_dir / "machineD.json").write_text(json.dumps({
            "machine": "machineE", "last_write_at": fresh, "last_command": "checkpoint",
        }), encoding="utf-8")
        (freshness_dir / "machineF.json").write_text(json.dumps({
            "machine": "machineF", "last_write_at": fresh, "last_command": "checkpoint",
        }), encoding="utf-8")

        with mock.patch.object(db, "SYNC_FRESHNESS_DIR", freshness_dir):
            report = db.sync_freshness_report("machineA")  # must not raise
        # The one good file survives -- "did not crash" is not enough, the
        # valid sibling must still be reported.
        self.assertEqual(list(report["machines"]), ["machineF"])

    def test_sync_freshness_signal_writes_serialize_across_concurrent_local_publishers(self):
        # Same concurrency shape as
        # test_presence_sidecar_writes_serialize_across_concurrent_local_publishers:
        # this writer used a SHARED temp path with no lock, so two local
        # processes could interleave truncate/write/rename. Patch once outside
        # the threads (see that test for why mock.patch.object is unsafe here).
        freshness_dir = self.root / ".sync_freshness"
        errors: list[Exception] = []
        original_dir = db.SYNC_FRESHNESS_DIR
        db.SYNC_FRESHNESS_DIR = freshness_dir
        try:
            def publish(index: int) -> None:
                try:
                    db.write_sync_freshness_signal("machineA", f"checkpoint-{index}")
                except Exception as exc:  # pragma: no cover - surfaced via errors list
                    errors.append(exc)

            threads = [threading.Thread(target=publish, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
        finally:
            db.SYNC_FRESHNESS_DIR = original_dir
        self.assertEqual(errors, [])
        # Whatever landed must be one writer's complete payload, never a torn
        # interleaving of two -- and no temp file may be left visible.
        payload = json.loads((freshness_dir / "machineA.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["machine"], "machineA")
        self.assertTrue(payload["last_command"].startswith("checkpoint-"))
        self.assertEqual(sorted(p.name for p in freshness_dir.glob("*.json")), ["machineA.json"])

    def test_presence_heartbeat_does_not_resurrect_a_stopped_row(self):
        # A heartbeat racing a stop must not resurrect a row. Resuming is
        # presence_start's job.
        presence_dir = self.root / ".presence"
        with mock.patch.object(db, "PRESENCE_DIR", presence_dir):
            db.presence_start(self.conn, "machineA", "claude", "demo", "task")
            db.presence_stop(self.conn, "machineA", "claude", "demo")
            updated = db.presence_heartbeat(self.conn, "machineA", "claude", "demo", task="late")
            self.assertEqual(updated, 0)
            row = self.conn.execute("SELECT status, task FROM agent_presence").fetchone()
            self.assertEqual(row["status"], "stopped")
            self.assertEqual(row["task"], "task")  # a no-op heartbeat changes nothing
            # presence_start still resumes the same identity.
            db.presence_start(self.conn, "machineA", "claude", "demo", "resumed")
            self.assertEqual(
                self.conn.execute("SELECT status FROM agent_presence").fetchone()["status"], "active"
            )

    def test_presence_sidecar_failure_never_fails_the_presence_call(self):
        # The sidecar refresh runs AFTER the caller's DB write has committed,
        # so raising would report failure for work that actually succeeded.
        # It previously caught only OSError -- and sqlite3.Error is NOT an
        # OSError subclass, so a DB-side failure on the sidecar's own SELECT
        # escaped to the caller.
        self.assertFalse(issubclass(sqlite3.OperationalError, OSError))
        presence_dir = self.root / ".presence"
        with mock.patch.object(db, "PRESENCE_DIR", presence_dir):
            db.presence_start(self.conn, "machineA", "claude", "demo", "task")
            broken = db.connect(self.database)
            broken.close()
            with mock.patch("sys.stderr", new=io.StringIO()) as err:
                db._write_presence_sidecar(broken, "machineA")  # must not raise
            self.assertIn("could not refresh presence sidecar", err.getvalue())
            # A failed refresh is not sticky: the next one republishes.
            db.presence_heartbeat(self.conn, "machineA", "claude", "demo")
            self.assertTrue((presence_dir / "machineA.json").exists())

    def test_stale_sidecar_temp_files_are_reaped_but_live_ones_survive(self):
        # A crash between write_text and replace leaves a *.json.tmp that no
        # reader's "*.json" glob will ever see, so nothing reclaimed it.
        # Age-gated: a concurrent publisher's in-flight temp is seconds old
        # and must never be deleted out from under it.
        presence_dir = self.root / ".presence"
        presence_dir.mkdir(parents=True)
        stale = presence_dir / ".machineA.123.deadbeef.json.tmp"
        fresh = presence_dir / ".machineA.456.cafebabe.json.tmp"
        stale.write_text("{}", encoding="utf-8")
        fresh.write_text("{}", encoding="utf-8")
        old_epoch = time.time() - db.SIDECAR_TEMP_MAX_AGE_SEC - 60
        os.utime(stale, (old_epoch, old_epoch))
        db._reap_stale_sidecar_temps(presence_dir)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
