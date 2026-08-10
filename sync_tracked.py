#!/usr/bin/env python3
"""Re-ingest reviewed project knowledge Markdown into endeavor_memory.sqlite3.

Single command to keep the shared Codex/Claude Code knowledge store current —
ingest is hash-based and idempotent (a file that hasn't changed reports
"unchanged", no duplicate rows), so this is always safe to re-run in full.

Usage:
    python3 ENDMEMEX/sync_tracked.py           # sync every eligible doc
    python3 ENDMEMEX/sync_tracked.py <path>...  # sync only named docs
      (paths must be keys of TRACKED_DOCS below)

Includes Git-tracked `PROTOTYPE/` documentation by explicit workspace policy.
It still excludes third-party notices, source libraries/translations,
prompt-baseline snapshots, and generated directories.

Active project-memory files may be listed explicitly while their new project
directory is still awaiting its first Git commit. This keeps current cross-agent
memory searchable without staging or committing user work implicitly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

import endeavor_db as db

DB_SCRIPT = Path(__file__).resolve().parent / "endeavor_db.py"
# Keep tracked-document discovery aligned with the database/runtime root. The
# public repository is standalone (its .git lives beside this script), unlike
# the Main-Mac monorepo where the Git root is its parent.
ROOT = db.ROOT

_EXCLUDED_PARTS = {".git", ".kiro", "__pycache__", "graphify-out", "node_modules", "venv", ".venv"}
_EXCLUDED_PREFIXES = (
    ("ENDEAVOR_VOX", "library"),
    ("ENDEAVOR_VOX", "translations"),
)
_EXCLUDED_NAMES = {"THIRD_PARTY_NOTICES.md"}
_PROJECT_PREFIXES = (
    ("AWAKE", "AWAKE"),
    ("ENDMEMEX", "ENDMEMEX"),
    ("PROTOTYPE/ENDEAVOR_LOCAL_AGENT_VLM", "PROTOTYPE_ENDEAVOR_LOCAL_AGENT_VLM"),
    ("PROTOTYPE/ENDEAVOR_AGENT", "PROTOTYPE_ENDEAVOR_AGENT"),
    ("PROTOTYPE/ENDEAVOR_CORE", "PROTOTYPE_ENDEAVOR_CORE"),
    ("ENDEAVOR_AGENT_API_MAX/ENDEAVOR_LOCAL_AGENT_API", "ENDEAVOR_AGENT_API_MAX"),
    ("ENDEAVOR_AGENT_API_MAX/ENDEAVOR_RAG_API", "ENDEAVOR_RAG_API"),
    ("ENDEAVOR_LOCAL_AGENT_MAX_VLM", "ENDEAVOR_LOCAL_AGENT_MAX_VLM"),
    ("ENDEAVOR_LOCAL_AGENT_MAX", "ENDEAVOR_LOCAL_AGENT_MAX"),
    ("ENDEAVOR_RAG_MAX", "ENDEAVOR_RAG_MAX"),
    ("ENDEAVOR_VOX", "ENDEAVOR_VOX"),
    ("ENDEAVOR_VISSION", "ENDEAVOR_VISSION"),
    ("ENDEAVOR_API_MAX", "ENDEAVOR_API_MAX"),
    ("agent_training_guide", "agent-training-guide"),
    ("Telegram_MAX", "Telegram_MAX"),
)
_ACTIVE_PROJECT_DOCS = ("AWAKE/PROJECT_MEMORY.md",)


def _kind_for(rel_path: str) -> str:
    lower = rel_path.lower()
    if "training_guide" in lower:
        return "training_guide"
    if any(token in lower for token in ("bug_report", "audit", "ledger")):
        return "audit"
    if any(token in lower for token in ("walking_test", "performance_test", "perf_report")):
        return "test_report"
    if "/skills/" in f"/{lower}" or lower.endswith("/skill_build.md"):
        return "skill_reference"
    if "/plan" in lower:
        return "plan"
    return "project_memory"


def _project_for(rel_path: str, *, standalone: bool = False) -> str:
    if standalone:
        return "ENDMEMEX"
    for prefix, project in _PROJECT_PREFIXES:
        if rel_path == prefix or rel_path.startswith(f"{prefix}/"):
            return project
    return "ENDEAVOR_AGENTIC"


def _git_tracked_markdown(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "*.md"],
        capture_output=True, check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"could not list Git-tracked Markdown: {detail}")
    return [item for item in result.stdout.decode("utf-8", errors="strict").split("\0") if item]


def staged_markdown_renames(root: Path) -> list[tuple[str, str]]:
    """Return (old_rel, new_rel) pairs for staged *.md files that Git detects
    as a RENAME (status R). Used to clean up ONLY the old orphan of a rename;
    a plain delete (status D) is never reported here, so a deleted doc's
    indexed snapshot is retained by design. Best-effort: any git failure (not
    a repo, detached state) returns [] and the caller simply skips cleanup —
    never raises, since this feeds an advisory pre-commit hook.

    `-z --name-status` emits each rename as three NUL fields: `R<score>`, old,
    new. `--find-renames` enables detection; without it Git may split a rename
    into delete+add, which we deliberately leave alone (safer to keep an orphan
    than to guess-delete)."""
    result = subprocess.run(
        ["git", "-C", str(root), "diff", "--cached", "--name-status", "-z",
         "--find-renames", "--diff-filter=R", "--", "*.md"],
        capture_output=True, check=False,
    )
    if result.returncode != 0:
        return []
    fields = [f for f in result.stdout.decode("utf-8", errors="strict").split("\0") if f]
    renames: list[tuple[str, str]] = []
    for i in range(0, len(fields), 3):
        if i + 2 >= len(fields):
            break
        status, old_rel, new_rel = fields[i], fields[i + 1], fields[i + 2]
        if status.startswith("R"):
            renames.append((old_rel, new_rel))
    return renames


def discover_knowledge_docs(
    root: Path = ROOT, *, tracked_paths: Iterable[str] | None = None
) -> dict[str, tuple[str, str]]:
    """Return reviewed Markdown tracked by Git or the active-project registry."""
    documents: dict[str, tuple[str, str]] = {}
    if tracked_paths is not None:
        candidate_paths = list(tracked_paths)
    else:
        candidate_paths = _git_tracked_markdown(root) + list(_ACTIVE_PROJECT_DOCS)
    for rel in dict.fromkeys(candidate_paths):
        path = root / rel
        if not path.is_file():
            continue
        parts = set(Path(rel).parts)
        if parts & _EXCLUDED_PARTS or path.name in _EXCLUDED_NAMES:
            continue
        if any(Path(rel).parts[:len(prefix)] == prefix for prefix in _EXCLUDED_PREFIXES):
            continue
        if path.name.startswith("prompt_baseline_") and path.name.endswith("_full.md"):
            continue
        documents[rel] = (_project_for(rel, standalone=root == DB_SCRIPT.parent), _kind_for(rel))
    return dict(sorted(documents.items()))


# Kept as a module-level value so callers can inspect exactly what a full sync
# will index. Regenerate it for each invocation to discover newly added docs.
TRACKED_DOCS = discover_knowledge_docs()


def _database_source_path(root: Path, rel_path: str) -> str:
    """Match endeavor_db.display_path() for either the real or test root."""
    absolute = (root / rel_path).resolve()
    try:
        return absolute.relative_to(db.ROOT).as_posix()
    except ValueError:
        return str(absolute)


def freshness_report(
    root: Path, conn, tracked_docs: dict[str, tuple[str, str]], *, include_orphans: bool = True
) -> dict[str, list[str]]:
    """Read-only comparison of tracked Markdown against indexed documents."""
    indexed = {
        row["source_path"]: row
        for row in conn.execute("SELECT source_path, content_hash, index_version, project, kind FROM documents")
    }
    expected_paths = {_database_source_path(root, rel): rel for rel in tracked_docs}
    report: dict[str, list[str]] = {
        key: [] for key in ("current", "stale", "missing", "orphaned", "metadata_mismatch")
    }
    for source_path, rel_path in expected_paths.items():
        expected_project, expected_kind = tracked_docs[rel_path]
        row = indexed.get(source_path)
        if row is None:
            report["missing"].append(rel_path)
            continue
        digest = hashlib.sha256((root / rel_path).read_bytes()).hexdigest()
        if row["content_hash"] != digest:
            report["stale"].append(rel_path)
        elif (
            row["index_version"] != db.INDEX_VERSION
            or row["project"] != expected_project
            or row["kind"] != expected_kind
        ):
            report["metadata_mismatch"].append(rel_path)
        else:
            report["current"].append(rel_path)
    if include_orphans:
        report["orphaned"] = sorted(path for path in indexed if path not in expected_paths)
    for values in report.values():
        values.sort()
    return report


def build_prune_proposal(root: Path, conn, tracked_docs: dict[str, tuple[str, str]]) -> dict[str, Any]:
    """Describe exact orphan rows for human review without changing SQLite."""
    allowed = {_database_source_path(root, rel) for rel in tracked_docs}
    rows = conn.execute(
        """SELECT d.source_path, d.content_hash, d.project, d.kind, d.imported_at,
                  COUNT(k.id) AS knowledge_rows
           FROM documents d LEFT JOIN knowledge k ON k.document_id = d.id
           GROUP BY d.id ORDER BY d.source_path"""
    ).fetchall()
    entries = [dict(row) for row in rows if row["source_path"] not in allowed]
    return {
        "schema_version": 1,
        "generated_at": db.now_utc(),
        "database": str(db.database_path()),
        "manifest_paths": len(allowed),
        "orphan_count": len(entries),
        "knowledge_rows": sum(item["knowledge_rows"] for item in entries),
        "entries": entries,
        "review_required": True,
    }


def apply_prune_proposal(root: Path, conn, proposal: dict[str, Any], tracked_docs: dict[str, tuple[str, str]]) -> int:
    """Apply an exact reviewed proposal only if every target is still unchanged."""
    if proposal.get("schema_version") != 1 or not isinstance(proposal.get("entries"), list):
        raise ValueError("invalid prune proposal schema")
    protected = {_database_source_path(root, rel) for rel in tracked_docs}
    targets: list[str] = []
    for entry in proposal["entries"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("source_path"), str):
            raise ValueError("invalid prune proposal entry")
        source_path = entry["source_path"]
        if source_path in protected:
            raise ValueError(f"proposal target is tracked again: {source_path}")
        row = conn.execute(
            "SELECT content_hash FROM documents WHERE source_path = ?", (source_path,),
        ).fetchone()
        if row is None:
            raise ValueError(f"proposal target no longer exists in index: {source_path}")
        if row["content_hash"] != entry.get("content_hash"):
            raise ValueError(f"proposal target changed since review: {source_path}")
        targets.append(source_path)
    return db.delete_documents_by_source_paths(conn, targets)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="Tracked Markdown paths to sync or check")
    parser.add_argument("--prune", action="store_true", help="Remove documents outside the tracked manifest")
    parser.add_argument(
        "--propose-prune", metavar="PATH",
        help="Write an exact, hash-pinned orphan tombstone proposal for human review (no DB mutation)",
    )
    parser.add_argument(
        "--apply-prune", metavar="PATH",
        help="Apply a reviewed proposal only if every target remains orphaned and hash-identical",
    )
    parser.add_argument(
        "--prune-renames", action="store_true",
        help="After sync, remove ONLY the old orphan of each staged git rename (R) of a tracked "
             ".md; deleted files (status D) keep their indexed snapshot. Safe for the pre-commit hook.",
    )
    parser.add_argument("--check", action="store_true", help="Read-only source freshness audit")
    parser.add_argument("--json", action="store_true", help="Machine-readable output (requires --check)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    maintenance_modes = sum(bool(value) for value in (
        args.prune, args.prune_renames, args.propose_prune, args.apply_prune,
    ))
    if maintenance_modes > 1:
        print("error: choose only one prune/proposal mode", file=sys.stderr)
        return 2
    if args.json and not args.check:
        print("error: --json requires --check", file=sys.stderr)
        return 2
    if args.check and maintenance_modes:
        print("error: --check cannot be combined with prune/proposal modes", file=sys.stderr)
        return 2
    requested = args.paths
    prune = args.prune
    prune_renames = args.prune_renames
    tracked_docs = discover_knowledge_docs()
    targets = requested if requested else list(tracked_docs)
    unknown = [t for t in targets if t not in tracked_docs]
    if unknown:
        print(f"not in TRACKED_DOCS, skipping: {unknown}", file=sys.stderr)
        targets = [t for t in targets if t in tracked_docs]

    if args.check:
        selected_docs = {path: tracked_docs[path] for path in targets}
        conn = db.connect(db.database_path(), read_only=True)
        try:
            report: dict[str, Any] = freshness_report(
                ROOT, conn, selected_docs, include_orphans=not bool(requested)
            )
        finally:
            conn.close()
        report["missing"].extend(unknown)
        report["missing"].sort()
        report["checked"] = len(targets) + len(unknown)
        report["ok"] = not any(report[key] for key in ("stale", "missing", "orphaned", "metadata_mismatch"))
        if args.json:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        else:
            for key in ("current", "stale", "missing", "orphaned", "metadata_mismatch"):
                print(f"{key}: {len(report[key])}")
                for path in report[key]:
                    print(f"  {path}")
        return 0 if report["ok"] else 1

    if args.propose_prune:
        conn = db.connect(db.database_path(), read_only=True)
        try:
            proposal = build_prune_proposal(ROOT, conn, tracked_docs)
        finally:
            conn.close()
        target = Path(args.propose_prune).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(proposal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"proposal": str(target), "orphan_count": proposal["orphan_count"]}, ensure_ascii=False))
        return 0

    if args.apply_prune:
        proposal_path = Path(args.apply_prune).expanduser().resolve()
        proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
        conn = db.connect(db.database_path())
        try:
            db.initialize(conn)
            removed = apply_prune_proposal(ROOT, conn, proposal, tracked_docs)
        finally:
            conn.close()
        print(json.dumps({"proposal": str(proposal_path), "removed": removed}, ensure_ascii=False))
        return 0

    failures = list(unknown)
    # Probe once (never spawns, ~0.3s worst case) rather than per file. A cold
    # companion still gets --no-embed -- without it, a changed doc would make
    # ingest wait the full ~30s embed-server startup on every commit. But when
    # something already warmed the companion this process (a recent `query
    # --semantic on` / `embed-backfill` inside its 1hr idle window), embedding
    # synchronously here is a fast path (ensure_embed_server returns
    # immediately when already ready), so a hook-triggered sync doesn't leave
    # newly-tracked docs at 0 embedding coverage until someone runs
    # embed-backfill by hand.
    embed_flags = [] if db.embed_companion_ready() else ["--no-embed"]
    ingested: set[str] = set()  # only paths whose ingest actually succeeded this run
    for rel_path in targets:
        project, kind = tracked_docs[rel_path]
        abs_path = ROOT / rel_path
        if not abs_path.is_file():
            print(f"[skip] {rel_path} — file does not exist")
            continue
        result = subprocess.run(
            [
                sys.executable, str(DB_SCRIPT), "ingest", rel_path,
                "--project", project, "--kind", kind, *embed_flags,
            ],
            cwd=ROOT, capture_output=True, text=True,
        )
        if result.returncode != 0:
            failures.append(rel_path)
            print(f"[FAIL] {rel_path}: {result.stderr.strip()}")
        else:
            ingested.add(rel_path)
            print(f"[ok] {rel_path}: {result.stdout.strip()}")

    if failures:
        print(f"\n{len(failures)} doc(s) failed to sync: {failures}", file=sys.stderr)
        return 1
    if prune:
        # Normalize the allowed set the SAME way ingest stores source_path
        # (display_path), not raw manifest keys — otherwise a tracked .md
        # symlink (manifest = link path, stored = resolve()d target) would look
        # out-of-scope and get pruned. Matches _database_source_path exactly.
        allowed = {_database_source_path(ROOT, rel) for rel in tracked_docs}
        conn = db.connect(db.database_path())
        try:
            db.initialize(conn)
            removed = db.prune_documents(conn, allowed)
        finally:
            conn.close()
        print(f"[ok] pruned {removed} out-of-scope indexed document(s)")
    if prune_renames:
        # Clean ONLY the old orphan of a git-detected rename (R); a deleted
        # file (status D) is never in this list, so its snapshot stays. Gate on
        # `ingested` (paths whose ingest actually SUCCEEDED), not set(targets):
        # a target whose working-tree file is missing is skipped (not ingested,
        # not a failure), and deleting the old orphan for a new name that was
        # never re-indexed would lose both.
        #
        # `protected` is the NORMALIZED source_path of every currently-tracked
        # doc (including each rename's new name). The safeguards below compare
        # RAW git path strings, but deletion is by the symlink-resolved
        # source_path — so a symlink rename A.md -> B.md where A.md resolves to
        # B.md's target (a case-only rename on a case-insensitive FS, or a
        # recreated A.md symlink) would otherwise normalize the "old" path onto
        # the just-ingested new document and delete it. Never delete a
        # normalized path that belongs to a tracked doc.
        protected = {_database_source_path(ROOT, rel) for rel in tracked_docs}
        old_paths: list[str] = []
        for old_rel, new_rel in staged_markdown_renames(ROOT):
            if new_rel not in tracked_docs:   # new must be an eligible tracked doc
                continue
            if new_rel not in ingested:       # and its new name must have been re-indexed this run
                continue
            if old_rel in tracked_docs:       # never delete an old name that is somehow still tracked
                continue
            old_norm = _database_source_path(ROOT, old_rel)
            if old_norm in protected:         # resolves onto the new/another tracked doc (symlink/case collision)
                continue
            old_paths.append(old_norm)
        if old_paths:
            conn = db.connect(db.database_path())
            try:
                db.initialize(conn)
                removed = db.delete_documents_by_source_paths(conn, old_paths)
            finally:
                conn.close()
            print(f"[ok] cleaned {removed} renamed-away orphan document(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
