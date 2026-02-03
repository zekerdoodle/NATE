"""Semantic search tool backed by FAISS and the embeddings index."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List

import faiss  # type: ignore
import numpy as np
from sentence_transformers import SentenceTransformer

from .exceptions import ToolExecutionError


LOGGER = logging.getLogger("tools.search")
ALLOWED_SOURCES = {"all", "documents", "tickets", "non-standard"}
DEFAULT_LIMIT = 5
MAX_LIMIT = 8
DEFAULT_MIN_SCORE = 0.25
LOW_CONFIDENCE_FLOOR = 0.18


@dataclass
class SearchResult:
	chunk_id: int
	score: float


class VectorSearcher:
	"""Lazy loader for FAISS + metadata lookups."""

	def __init__(self, repo_root: Path) -> None:
		self.repo_root = repo_root
		self.data_dir = repo_root / "data"
		self.db_path = self.data_dir / "embeddings.sqlite"
		self.index_path = self.data_dir / "vector.index"
		self.vector_ids_path = self.data_dir / "vector_ids.json"
		self._index: faiss.Index | None = None
		self._chunk_ids: List[int] = []
		self._model: SentenceTransformer | None = None
		self._conn: sqlite3.Connection | None = None
		self._model_name: str | None = None

	def _ensure_index(self) -> None:
		if self._index is not None:
			return
		if not self.index_path.exists() or not self.vector_ids_path.exists():
			raise ToolExecutionError(
				"Semantic index not found. Run `python embed.py sync` before searching."
			)
		with self.vector_ids_path.open("r", encoding="utf-8") as handle:
			payload = json.load(handle)
			self._chunk_ids = [int(value) for value in payload.get("chunk_ids", [])]
			self._model_name = payload.get("model")
		if not self._chunk_ids:
			raise ToolExecutionError(
				"Semantic index is empty. Run `python embed.py sync` to create embeddings."
			)
		self._index = faiss.read_index(str(self.index_path))
		if self._index.ntotal != len(self._chunk_ids):
			raise ToolExecutionError("FAISS index is out of sync with metadata; rebuild embeddings.")

	def _ensure_model(self) -> SentenceTransformer:
		if self._model is None:
			from embed import DEFAULT_MODEL_NAME  # local import to avoid cycle at load

			target_model = self._model_name or DEFAULT_MODEL_NAME
			LOGGER.info("Loading query encoder %s", target_model)
			self._model = SentenceTransformer(target_model, trust_remote_code=True, device="cpu")
		return self._model

	def _ensure_connection(self) -> sqlite3.Connection:
		if self._conn is None:
			if not self.db_path.exists():
				raise ToolExecutionError(
					"Embedding database missing. Run `python embed.py sync` to initialise."
				)
			self._conn = sqlite3.connect(self.db_path)
			self._conn.row_factory = sqlite3.Row
		return self._conn

	def _fetch_metadata(self, chunk_ids: Iterable[int]) -> Dict[int, sqlite3.Row]:
		ids = list(dict.fromkeys(chunk_ids))
		if not ids:
			return {}
		conn = self._ensure_connection()
		placeholders = ",".join("?" for _ in ids)
		query = f"""
			SELECT c.*, f.last_indexed_at
			FROM chunks c
			JOIN files f ON f.path = c.file_path
			WHERE c.id IN ({placeholders})
		"""
		rows = conn.execute(query, tuple(ids)).fetchall()
		return {int(row["id"]): row for row in rows}

	def search(
		self,
		*,
		query: str,
		source: str,
		title: str | None,
		limit: int,
		min_score: float,
		return_content: bool,
	) -> Dict[str, Any]:
		self._ensure_index()
		model = self._ensure_model()

		query_vector = model.encode(
			[query],
			batch_size=1,
			show_progress_bar=False,
			convert_to_numpy=True,
			normalize_embeddings=True,
		)[0].astype("float32")

		k = min(len(self._chunk_ids), max(limit * 3, 20))
		distances, indices = self._index.search(np.expand_dims(query_vector, axis=0), k)  # type: ignore[arg-type]
		scores = distances[0]
		chunk_indices = indices[0]

		candidates: List[SearchResult] = []
		for score, chunk_idx in zip(scores, chunk_indices):
			if chunk_idx == -1:
				continue
			candidates.append(SearchResult(chunk_id=self._chunk_ids[chunk_idx], score=float(score)))

		metadata = self._fetch_metadata(result.chunk_id for result in candidates)

		filtered: List[Dict[str, Any]] = []
		low_confidence: List[Dict[str, Any]] = []
		title_filter = title.strip().lower() if title else None
		source_filter = source if source != "all" else None
		query_terms = _normalise_terms(query)

		for result in candidates:
			row = metadata.get(result.chunk_id)
			if row is None:
				continue
			if source_filter and row["source"] != source_filter:
				continue
			if title_filter:
				relative = row["file_path"].lower()
				filename = Path(relative).name.lower()
				if title_filter not in relative and title_filter != filename:
					continue

			payload = _format_result(row, result.score, query_terms, return_content)
			if result.score >= min_score:
				filtered.append(payload)
			elif result.score >= LOW_CONFIDENCE_FLOOR:
				low_confidence.append(payload)
			if len(filtered) >= limit:
				break

		return {
			"query": query,
			"source": source,
			"title": title,
			"limit": limit,
			"min_score": min_score,
			"result_count": len(filtered),
			"results": filtered,
			"results_low_confidence": low_confidence,
		}

	def close(self) -> None:
		if self._conn is not None:
			self._conn.close()
		self._conn = None
		self._index = None
		self._chunk_ids = []
		self._model = None


_SEARCHERS: Dict[Path, VectorSearcher] = {}


def run(parameters: Dict[str, Any], *, repo_root: Path) -> Dict[str, Any]:
	query = parameters.get("query")
	if not query or not isinstance(query, str):
		raise ToolExecutionError("`query` must be provided as a non-empty string")

	source = (parameters.get("source") or "all").lower()
	if source not in ALLOWED_SOURCES:
		raise ToolExecutionError("Invalid `source`; choose one of all/documents/tickets/non-standard")

	limit_value = parameters.get("limit", DEFAULT_LIMIT)
	try:
		limit = max(1, min(int(limit_value), MAX_LIMIT))
	except (TypeError, ValueError) as exc:
		raise ToolExecutionError("`limit` must be an integer") from exc

	min_score_value = parameters.get("min_score", DEFAULT_MIN_SCORE)
	try:
		min_score = float(min_score_value)
		min_score = max(0.0, min(1.0, min_score))
	except (TypeError, ValueError) as exc:
		raise ToolExecutionError("`min_score` must be a float between 0 and 1") from exc

	return_content = bool(parameters.get("return_content", False))

	searcher = _SEARCHERS.get(repo_root)
	if searcher is None:
		searcher = VectorSearcher(repo_root)
		_SEARCHERS[repo_root] = searcher

	return searcher.search(
		query=query.strip(),
		source=source,
		title=parameters.get("title"),
		limit=limit,
		min_score=min_score,
		return_content=return_content,
	)


def _normalise_terms(query: str) -> List[str]:
	return [token for token in re.findall(r"\w+", query.lower()) if token]


def _format_result(
	row: sqlite3.Row,
	score: float,
	query_terms: List[str],
	return_content: bool,
) -> Dict[str, Any]:
	path = row["file_path"].replace("\\", "/")
	text = row["text"]
	snippet = _build_snippet(text, query_terms)
	result: Dict[str, Any] = {
		"path": path,
		"chunk_id": row["text_hash"][:8],
		"score": round(float(score), 4),
		"start_line": row["start_line"],
		"end_line": row["end_line"],
		"snippet": snippet,
		"metadata": {
			"source": row["source"],
			"chunk_index": row["chunk_idx"],
			"updated_at": row["last_indexed_at"],
		},
	}
	if return_content:
		result["content"] = text
	return result


def _build_snippet(text: str, query_terms: List[str], window: int = 200) -> str:
	if not text:
		return ""
	terms = set(query_terms)
	sentences = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
	best_sentence = None
	best_score = -1
	for sentence in sentences:
		if not sentence:
			continue
		words = [w.lower() for w in re.findall(r"\w+", sentence)]
		if not words:
			continue
		match_score = sum(1 for word in words if word in terms)
		if match_score > best_score:
			best_sentence = sentence
			best_score = match_score
	if best_sentence is None:
		best_sentence = sentences[0]

	start_idx = text.find(best_sentence)
	if start_idx == -1:
		return (text[: window].strip() + "...") if len(text) > window else text.strip()

	window_half = max(40, window // 2)
	snippet_start = max(0, start_idx - window_half)
	snippet_end = min(len(text), start_idx + len(best_sentence) + window_half)
	snippet = text[snippet_start:snippet_end].strip()
	if snippet_start > 0:
		snippet = "..." + snippet
	if snippet_end < len(text):
		snippet = snippet + "..."
	return snippet


__all__ = ["run"]
