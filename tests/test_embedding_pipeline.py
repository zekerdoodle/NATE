"""Tests for the embedding pipeline and semantic search helpers."""

from __future__ import annotations

import gc
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterable, Tuple
from unittest import TestCase
from unittest.mock import patch

import numpy as np

from embed import DocumentProcessor, EmbeddingPipeline, classify_source
from tools import search as search_tool


class _StubTokenizer:
    def span_tokenize(self, text: str) -> Iterable[Tuple[int, int]]:
        yield (0, len(text))


class _StubModel:
    def __init__(self, *args, **kwargs) -> None:
        self._vector = np.array([0.5, 0.5, 0.5, 0.5], dtype="float32")
        self._vector /= np.linalg.norm(self._vector)

    def encode(self, texts, **kwargs):  # type: ignore[override]
        count = len(texts)
        return np.tile(self._vector, (count, 1))


class EmbeddingPipelineTests(TestCase):
    def test_classify_source(self) -> None:
        self.assertEqual(classify_source("docs/it_docs/example.txt"), "documents")
        self.assertEqual(classify_source("docs/tickets/123.json"), "tickets")
        self.assertEqual(classify_source("archive/folder/file.txt"), "non-standard")

    def test_chunk_document_preserves_basic_metadata(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            repo_root = Path(tmpdir)
            docs_dir = repo_root / Path("docs") / "it_docs"
            docs_dir.mkdir(parents=True)
            sample_path = docs_dir / "sample.txt"
            sample_text = "First sentence. Second sentence follows."
            sample_path.write_text(sample_text, encoding="utf-8")

            with patch.object(DocumentProcessor, "_ensure_sentence_tokenizer", return_value=_StubTokenizer()):
                processor = DocumentProcessor(repo_root)
                document = processor.load_document(sample_path)
                chunks = processor.chunk_document(document, source="documents")

        self.assertEqual(len(chunks), 1)
        chunk = chunks[0]
        self.assertEqual(chunk.start_line, 1)
        self.assertGreater(chunk.token_count, 0)
        self.assertTrue(chunk.text.startswith("First sentence"))

    def test_pipeline_sync_writes_index_and_search_returns_snippet(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            repo_root = Path(tmpdir)
            doc_dir = repo_root / Path("docs") / "it_docs"
            doc_dir.mkdir(parents=True)
            doc_path = doc_dir / "guide.txt"
            doc_path.write_text(
                "Resetting passwords requires Active Directory access. Follow the reset steps carefully.",
                encoding="utf-8",
            )

            with patch.object(DocumentProcessor, "_ensure_sentence_tokenizer", return_value=_StubTokenizer()):
                pipeline = EmbeddingPipeline(repo_root)
                with patch("embed.SentenceTransformer", _StubModel):
                    pipeline.sync()

            db_path = repo_root / "data/embeddings.sqlite"
            self.assertTrue(db_path.exists())
            with sqlite3.connect(db_path) as conn:
                count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                self.assertGreaterEqual(count, 1)

            with patch("tools.search.SentenceTransformer", _StubModel):
                result = search_tool.run({"query": "reset passwords", "limit": 3}, repo_root=repo_root)
            searcher = search_tool._SEARCHERS.pop(repo_root, None)
            if searcher is not None:
                searcher.close()
        pipeline = None  # release handles before tempfile cleanup
        gc.collect()

        self.assertGreaterEqual(result["result_count"], 1)
        first = result["results"][0]
        self.assertTrue(first["snippet"])
        self.assertEqual(first["metadata"]["source"], "documents")


if __name__ == "__main__":  # pragma: no cover - direct test execution
    import unittest

    unittest.main()
