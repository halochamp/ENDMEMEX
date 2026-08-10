from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import embed_server
except ModuleNotFoundError as exc:
    if exc.name != "fastapi":
        raise
    embed_server = None


@unittest.skipIf(embed_server is None, "embedding companion dependencies are optional in this interpreter")
class EmbedRequestLimitTest(unittest.TestCase):
    def test_rejects_too_many_texts_at_validation(self):
        with self.assertRaises(Exception):
            embed_server.EmbedRequest(texts=["x"] * (embed_server.MAX_TEXTS_PER_REQUEST + 1))

    def test_rejects_oversized_text_before_model_access(self):
        request = embed_server.EmbedRequest(texts=["x" * (embed_server.MAX_CHARS_PER_TEXT + 1)])
        with self.assertRaises(embed_server.HTTPException) as raised:
            embed_server.embed(request)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(raised.exception.detail["reason"], "text_too_large")

    def test_rejects_oversized_aggregate_before_model_access(self):
        # Each text remains valid on its own; only their aggregate is too big.
        request = embed_server.EmbedRequest(
            texts=["x" * embed_server.MAX_CHARS_PER_TEXT] * 11
        )
        with self.assertRaises(embed_server.HTTPException) as raised:
            embed_server.embed(request)
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(raised.exception.detail["reason"], "request_too_large")


if __name__ == "__main__":
    unittest.main()
