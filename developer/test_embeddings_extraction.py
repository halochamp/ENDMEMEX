from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import embeddings
import endeavor_db


class EmbeddingsExtractionTest(unittest.TestCase):
    def test_imports_are_one_way(self):
        endmemex = Path(__file__).resolve().parent.parent
        script = (
            "import embeddings\n"
            "if 'endeavor_db' in __import__('sys').modules:\n"
            "    raise SystemExit('endeavor_db imported as a side effect of importing embeddings')\n"
            "import endeavor_db\n"
        )
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(endmemex))
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=str(endmemex), env=env,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_endeavor_db_imports_embeddings_in_production(self):
        endmemex = Path(__file__).resolve().parent.parent
        importer_pattern = re.compile(r"^\s*(?:from|import)\s+embeddings\b", re.MULTILINE)
        importers = sorted(
            path.relative_to(endmemex).as_posix()
            for path in endmemex.glob("*.py")
            if path.name != "embeddings.py" and importer_pattern.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(importers, ["endeavor_db.py"])

    def test_facade_functions_are_the_identical_objects(self):
        self.assertIs(endeavor_db.embedding_hash, embeddings.embedding_hash)
        self.assertIs(endeavor_db.pack_embedding, embeddings.pack_embedding)
        self.assertIs(endeavor_db.unpack_embedding, embeddings.unpack_embedding)
        self.assertIs(endeavor_db._embed_identity_matches, embeddings.embed_identity_matches)
        self.assertIs(endeavor_db._localhost_permission_denied, embeddings.localhost_permission_denied)
        self.assertIs(endeavor_db.valid_embedding_blob, embeddings.valid_embedding_blob)

    def test_pack_unpack_roundtrip_and_hash_stability(self):
        vector = [0.1, -0.2, 0.3] * (embeddings.EMBED_DIM // 3)
        packed = endeavor_db.pack_embedding(vector)
        unpacked = endeavor_db.unpack_embedding(packed)
        for original, restored in zip(vector, unpacked):
            self.assertAlmostEqual(original, restored, places=2)  # struct 'e' is half-precision
        self.assertEqual(endeavor_db.embedding_hash("x"), endeavor_db.embedding_hash("x"))
        self.assertNotEqual(endeavor_db.embedding_hash("x"), endeavor_db.embedding_hash("y"))

    def test_deferred_companion_client_chain_stayed_in_endeavor_db(self):
        # Confirms the deliberate scoping decision, not just describes it:
        # the deeply-coupled health/spawn chain must still be defined in
        # endeavor_db.py, not silently relocated in a later edit.
        for name in (
            "_embed_health_probe", "_embed_health", "_embed_ready",
            "embed_companion_ready", "embed_failure_reason", "embed_start_lock",
            "_embed_python_candidates", "_embed_python_has_dependencies",
            "resolve_embed_python", "embedding_diagnostics", "_spawn_embed_server",
            "ensure_embed_server", "set_embed_keep_warm", "embed_texts",
        ):
            with self.subTest(name=name):
                self.assertIn(
                    name, endeavor_db.__dict__,
                    f"{name} must remain defined in endeavor_db.py (not imported from embeddings.py)",
                )
                self.assertEqual(
                    getattr(endeavor_db.__dict__[name], "__module__", None), "endeavor_db",
                    f"{name} must be DEFINED in endeavor_db.py, not imported and re-exported from elsewhere",
                )
                self.assertFalse(
                    hasattr(embeddings, name),
                    f"{name} must not exist in embeddings.py in this slice",
                )


if __name__ == "__main__":
    unittest.main()
