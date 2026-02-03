"""Embedding pipeline and CLI for Nate's semantic search index."""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator, List, Sequence, Tuple

import faiss  # type: ignore
import numpy as np
import nltk
from sentence_transformers import SentenceTransformer
from tqdm import tqdm


LOGGER = logging.getLogger("embed")

DEFAULT_MODEL_NAME = "nomic-ai/nomic-embed-text-v1.5"
ALLOWED_SUFFIXES = {".md", ".txt", ".json", ".csv", ".log"}
MAX_TICKET_AGE_DAYS = 90
TARGET_TOKENS = 450
OVERLAP_TOKENS = 80
DEFAULT_MIN_SCORE = 0.25


@dataclass
class Chunk:
	"""Chunked view of a document ready for embedding."""

	chunk_idx: int
	text: str
	start_line: int
	end_line: int
	token_count: int
	text_hash: str
	source: str


@dataclass
class Document:
	"""Normalised document text with positional metadata."""

	path: Path
	text: str
	line_offsets: List[int]

	def char_to_line(self, offset: int) -> int:
		if not self.line_offsets:
			return 1
		index = bisect.bisect_right(self.line_offsets, max(0, offset)) - 1
		return max(1, index + 1)


@dataclass
class IndexPlan:
	"""Summary of work required for a sync or rebuild operation."""

	to_index: List[str]
	to_delete: List[str]
	skipped_tickets: List[str]

	def describe(self) -> str:
		return (
			f"{len(self.to_index)} file(s) to index, "
			f"{len(self.to_delete)} file(s) to delete, "
			f"{len(self.skipped_tickets)} ticket(s) skipped due to age"
		)


class EmbeddingStore:
	"""Persistence layer for chunk metadata and embeddings."""

	def __init__(self, path: Path) -> None:
		self.path = path
		self.conn = sqlite3.connect(path)
		self.conn.row_factory = sqlite3.Row
		self.conn.execute("PRAGMA foreign_keys=ON")
		self._ensure_schema()

	def close(self) -> None:
		self.conn.close()

	def _ensure_schema(self) -> None:
		self.conn.executescript(
			"""
			CREATE TABLE IF NOT EXISTS files (
				path TEXT PRIMARY KEY,
				checksum TEXT NOT NULL,
				mtime REAL NOT NULL,
				size_bytes INTEGER NOT NULL,
				last_indexed_at TEXT
			);

			CREATE TABLE IF NOT EXISTS chunks (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				file_path TEXT NOT NULL,
				chunk_idx INTEGER NOT NULL,
				start_line INTEGER NOT NULL,
				end_line INTEGER NOT NULL,
				token_count INTEGER NOT NULL,
				text_hash TEXT NOT NULL,
				text TEXT NOT NULL,
				source TEXT NOT NULL,
				FOREIGN KEY (file_path) REFERENCES files(path) ON DELETE CASCADE
			);

			CREATE TABLE IF NOT EXISTS embeddings (
				chunk_id INTEGER PRIMARY KEY,
				vector BLOB NOT NULL,
				FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE
			);
			"""
		)

	# file metadata -----------------------------------------------------

	def list_files(self) -> dict[str, sqlite3.Row]:
		rows = self.conn.execute("SELECT * FROM files").fetchall()
		return {row["path"]: row for row in rows}

	def upsert_file(self, path: str, checksum: str, mtime: float, size: int) -> None:
		now_iso = datetime.now(timezone.utc).isoformat()
		with self.conn:
			self.conn.execute(
				"""
				INSERT INTO files (path, checksum, mtime, size_bytes, last_indexed_at)
				VALUES (?, ?, ?, ?, ?)
				ON CONFLICT(path) DO UPDATE SET
					checksum=excluded.checksum,
					mtime=excluded.mtime,
					size_bytes=excluded.size_bytes,
					last_indexed_at=excluded.last_indexed_at
				""",
				(path, checksum, mtime, size, now_iso),
			)

	def delete_file(self, path: str) -> None:
		with self.conn:
			self.conn.execute("DELETE FROM files WHERE path = ?", (path,))

	# chunk + embedding persistence -------------------------------------

	def replace_chunks(self, path: str, chunks: Sequence[Chunk]) -> List[int]:
		chunk_ids: List[int] = []
		with self.conn:
			self.conn.execute("DELETE FROM chunks WHERE file_path = ?", (path,))
			for chunk in chunks:
				cursor = self.conn.execute(
					"""
					INSERT INTO chunks (file_path, chunk_idx, start_line, end_line, token_count, text_hash, text, source)
					VALUES (?, ?, ?, ?, ?, ?, ?, ?)
					""",
					(
						path,
						chunk.chunk_idx,
						chunk.start_line,
						chunk.end_line,
						chunk.token_count,
						chunk.text_hash,
						chunk.text,
						chunk.source,
					),
				)
				chunk_ids.append(int(cursor.lastrowid))
		return chunk_ids

	def upsert_embeddings(self, chunk_vectors: Sequence[Tuple[int, bytes]]) -> None:
		with self.conn:
			self.conn.executemany(
				"""
				INSERT INTO embeddings (chunk_id, vector)
				VALUES (?, ?)
				ON CONFLICT(chunk_id) DO UPDATE SET vector=excluded.vector
				""",
				chunk_vectors,
			)

	def fetch_all_embeddings(self) -> List[Tuple[int, np.ndarray]]:
		rows = self.conn.execute(
			"SELECT chunk_id, vector FROM embeddings ORDER BY chunk_id"
		).fetchall()
		payload: List[Tuple[int, np.ndarray]] = []
		for row in rows:
			vec = np.frombuffer(row["vector"], dtype="float32")
			payload.append((int(row["chunk_id"]), vec))
		return payload

	def chunk_metadata(self, chunk_ids: Sequence[int]) -> dict[int, sqlite3.Row]:
		if not chunk_ids:
			return {}
		placeholders = ",".join(["?"] * len(chunk_ids))
		query = f"""
			SELECT c.*, f.last_indexed_at
			FROM chunks c
			JOIN files f ON f.path = c.file_path
			WHERE c.id IN ({placeholders})
		"""
		rows = self.conn.execute(query, tuple(chunk_ids)).fetchall()
		return {int(row["id"]): row for row in rows}


class DocumentProcessor:
	"""Transforms repository files into embedding-ready chunks."""

	def __init__(self, repo_root: Path) -> None:
		self.repo_root = repo_root
		self._sentence_tokenizer = self._ensure_sentence_tokenizer()

	@staticmethod
	def _ensure_sentence_tokenizer() -> nltk.tokenize.PunktSentenceTokenizer:
		try:
			tokenizer = nltk.data.load("tokenizers/punkt/english.pickle")
		except LookupError:
			nltk.download("punkt", quiet=True)
			nltk.download("punkt_tab", quiet=True)
			tokenizer = nltk.data.load("tokenizers/punkt/english.pickle")
		return tokenizer

	def load_document(self, path: Path) -> Document:
		suffix = path.suffix.lower()
		if suffix == ".json":
			text = self._load_json(path)
		elif suffix == ".csv":
			text = self._load_csv(path)
		else:
			text = self._load_text(path)

		text = text.replace("\r\n", "\n").replace("\r", "\n")
		if not text.endswith("\n"):
			text = text + "\n"
		line_offsets: List[int] = [0]
		for idx, char in enumerate(text):
			if char == "\n":
				line_offsets.append(idx + 1)
		return Document(path=path, text=text, line_offsets=line_offsets)

	def _load_text(self, path: Path) -> str:
		try:
			with path.open("r", encoding="utf-8") as handle:
				return handle.read()
		except UnicodeDecodeError:
			with path.open("r", encoding="utf-8", errors="ignore") as handle:
				return handle.read()

	def _load_json(self, path: Path) -> str:
		try:
			with path.open("r", encoding="utf-8") as handle:
				payload = json.load(handle)
		except json.JSONDecodeError:
			return self._load_text(path)
		lines = []
		for key, value in self._flatten_json(payload):
			lines.append(f"{key}: {value}")
		return "\n".join(lines)

	def _flatten_json(self, value: object, prefix: str = "") -> Iterator[Tuple[str, str]]:
		if isinstance(value, dict):
			for key, inner in value.items():
				new_prefix = f"{prefix}.{key}" if prefix else str(key)
				yield from self._flatten_json(inner, new_prefix)
		elif isinstance(value, list):
			for index, inner in enumerate(value):
				new_prefix = f"{prefix}[{index}]" if prefix else f"[{index}]"
				yield from self._flatten_json(inner, new_prefix)
		else:
			display = "" if value is None else str(value)
			label = prefix or "value"
			yield (label, display)

	def _load_csv(self, path: Path) -> str:
		lines: List[str] = []
		with path.open("r", encoding="utf-8", newline="") as handle:
			reader = csv.reader(handle)
			rows = list(reader)
		if not rows:
			return ""
		header = rows[0]
		lines.append(" | ".join(header))
		for row_index, row in enumerate(rows[1:], start=1):
			columns = [f"{header[idx]}={value}" for idx, value in enumerate(row) if str(value).strip()]
			if not columns:
				continue
			lines.append(f"Row {row_index}: " + ", ".join(columns))
		return "\n".join(lines)

	def chunk_document(self, document: Document, source: str) -> List[Chunk]:
		spans = list(self._sentence_tokenizer.span_tokenize(document.text))
		if not spans:
			spans = [(0, len(document.text))]
		chunks: List[Chunk] = []
		current: List[Tuple[int, int, int]] = []  # (start, end, token_count)
		current_tokens = 0
		chunk_counter = 0

		for idx, (start, end) in enumerate(spans):
			segment = document.text[start:end]
			token_count = max(1, len(segment.split()))
			if not segment.strip():
				continue
			current.append((start, end, token_count))
			current_tokens += token_count
			is_last_sentence = idx == len(spans) - 1

			if current_tokens >= TARGET_TOKENS or is_last_sentence:
				chunk_start = current[0][0]
				chunk_end = current[-1][1]
				chunk_text = document.text[chunk_start:chunk_end].strip()
				if not chunk_text:
					current = []
					current_tokens = 0
					continue
				chunk = Chunk(
					chunk_idx=chunk_counter,
					text=chunk_text,
					start_line=document.char_to_line(chunk_start),
					end_line=document.char_to_line(chunk_end - 1),
					token_count=sum(item[2] for item in current),
					text_hash=hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
					source=source,
				)
				chunks.append(chunk)
				chunk_counter += 1

				if is_last_sentence:
					break

				overlap = []
				overlap_tokens = 0
				for entry in reversed(current):
					overlap.append(entry)
					overlap_tokens += entry[2]
					if overlap_tokens >= OVERLAP_TOKENS:
						break
				overlap = list(reversed(overlap))
				if len(overlap) == len(current):
					overlap = overlap[-1:]
				current = overlap
				current_tokens = sum(item[2] for item in current)

		return chunks


class EmbeddingPipeline:
	"""Coordinates scanning, embedding, and index rebuilds."""

	def __init__(self, repo_root: Path, model_name: str = DEFAULT_MODEL_NAME) -> None:
		self.repo_root = repo_root
		self.model_name = model_name
		self.data_dir = repo_root / "data"
		self.db_path = self.data_dir / "embeddings.sqlite"
		self.index_path = self.data_dir / "vector.index"
		self.vector_ids_path = self.data_dir / "vector_ids.json"
		self.backup_dir = self.data_dir / "backups"
		self.processor = DocumentProcessor(repo_root)
		self.model: SentenceTransformer | None = None

	# directory helpers -------------------------------------------------

	def prepare(self) -> None:
		self.data_dir.mkdir(parents=True, exist_ok=True)
		self.backup_dir.mkdir(parents=True, exist_ok=True)

	def snapshot(self) -> None:
		timestamp = datetime.now(timezone.utc).strftime("%Y%m%d")
		target = self.backup_dir / timestamp
		if target.exists():
			return
		target.mkdir(parents=True, exist_ok=True)
		for artifact in (self.db_path, self.index_path, self.vector_ids_path):
			if artifact.exists():
				shutil.copy2(artifact, target / artifact.name)
		self._prune_old_backups()

	def _prune_old_backups(self) -> None:
		backup_dirs = sorted(d for d in self.backup_dir.iterdir() if d.is_dir())
		while len(backup_dirs) > 3:
			victim = backup_dirs.pop(0)
			shutil.rmtree(victim, ignore_errors=True)

	# model --------------------------------------------------------------

	def _ensure_model(self) -> SentenceTransformer:
		if self.model is None:
			LOGGER.info("Loading embedding model %s", self.model_name)
			self.model = SentenceTransformer(self.model_name, trust_remote_code=True, device="cpu")
		return self.model

	# plan computation ---------------------------------------------------

	def build_plan(self, include: set[str] | None = None) -> IndexPlan:
		store = EmbeddingStore(self.db_path)
		try:
			existing = store.list_files()
		finally:
			store.close()

		include = include or set()
		candidate_paths, skipped_tickets = self._collect_repository_paths()

		if include:
			candidate_paths = [path for path in candidate_paths if path in include]

		to_index: List[str] = []
		for path in candidate_paths:
			absolute = self.repo_root / path
			meta = absolute.stat()
			checksum = sha256_file(absolute)
			record = existing.get(path)
			if not record:
				to_index.append(path)
				continue
			if (
				abs(record["mtime"] - meta.st_mtime) > 1e-6
				or record["checksum"] != checksum
				or record["size_bytes"] != meta.st_size
			):
				to_index.append(path)
		to_index.extend(path for path in include if path not in to_index)

		to_delete = [path for path in existing if (self.repo_root / path).exists() is False]
		to_delete.extend(path for path in existing if path in skipped_tickets)
		return IndexPlan(sorted(set(to_index)), sorted(set(to_delete)), skipped_tickets)

	def _collect_repository_paths(self) -> Tuple[List[str], List[str]]:
		cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_TICKET_AGE_DAYS)
		candidate: List[str] = []
		skipped: List[str] = []
		for root_name in ("docs", "archive"):
			root = self.repo_root / root_name
			if not root.exists():
				continue
			for path in root.rglob("*"):
				if path.is_dir():
					continue
				if path.suffix.lower() not in ALLOWED_SUFFIXES:
					continue
				relative = path.relative_to(self.repo_root).as_posix()
				if relative.startswith("docs/tickets/"):
					mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
					if mtime < cutoff:
						skipped.append(relative)
						continue
				candidate.append(relative)
		return candidate, skipped

	# operations ---------------------------------------------------------

	def rebuild(self, *, plan_only: bool = False) -> None:
		self.prepare()
		self.snapshot()
		for artifact in (self.db_path, self.index_path, self.vector_ids_path):
			if artifact.exists() and not plan_only:
				artifact.unlink()
		plan = self.build_plan(include=None)
		LOGGER.info("Rebuild plan: %s", plan.describe())
		if plan_only:
			return
		self._execute_index(plan)

	def sync(self, include: set[str] | None = None, *, plan_only: bool = False) -> None:
		self.prepare()
		self.snapshot()
		plan = self.build_plan(include=include)
		LOGGER.info("Sync plan: %s", plan.describe())
		if plan_only:
			return
		self._execute_index(plan)

	def drop(self, paths: Iterable[str]) -> None:
		self.prepare()
		store = EmbeddingStore(self.db_path)
		try:
			for relative in paths:
				LOGGER.info("Dropping %s", relative)
				store.delete_file(relative)
		finally:
			store.close()
		self._rebuild_faiss_index()

	def _execute_index(self, plan: IndexPlan) -> None:
		store = EmbeddingStore(self.db_path)
		try:
			for path in plan.to_delete:
				LOGGER.info("Removing stale entry %s", path)
				store.delete_file(path)

			if not plan.to_index:
				LOGGER.info("No files require embedding")
			else:
				model = self._ensure_model()
				for relative in tqdm(plan.to_index, desc="Embedding", unit="file"):
					absolute = self.repo_root / relative
					if not absolute.exists():
						LOGGER.warning("Skipping missing file %s", relative)
						continue
					checksum = sha256_file(absolute)
					stat = absolute.stat()
					document = self.processor.load_document(absolute)
					source = classify_source(relative)
					chunks = self.processor.chunk_document(document, source=source)
					if not chunks:
						LOGGER.info("No chunks produced for %s; removing from index", relative)
						store.delete_file(relative)
						continue

					embeddings = model.encode(
						[chunk.text for chunk in chunks],
						batch_size=256,
						show_progress_bar=False,
						convert_to_numpy=True,
						normalize_embeddings=True,
					)
					vectors = np.asarray(embeddings, dtype="float32")
					store.upsert_file(relative, checksum, stat.st_mtime, stat.st_size)
					chunk_ids = store.replace_chunks(relative, chunks)
					payload = [
						(chunk_id, vectors[idx].tobytes())
						for idx, chunk_id in enumerate(chunk_ids)
					]
					store.upsert_embeddings(payload)
		finally:
			store.close()

		self._rebuild_faiss_index()

	def _rebuild_faiss_index(self) -> None:
		store = EmbeddingStore(self.db_path)
		try:
			embeddings = store.fetch_all_embeddings()
		finally:
			store.close()

		if not embeddings:
			LOGGER.info("No embeddings available; removing FAISS index")
			for artifact in (self.index_path, self.vector_ids_path):
				if artifact.exists():
					artifact.unlink()
			return

		chunk_ids = [item[0] for item in embeddings]
		matrix = np.vstack([item[1] for item in embeddings]).astype("float32")
		dim = matrix.shape[1]

		index = faiss.IndexFlatIP(dim)
		index.add(matrix)
		faiss.write_index(index, str(self.index_path))

		metadata = {
			"chunk_ids": chunk_ids,
			"dimension": dim,
			"built_at": datetime.now(timezone.utc).isoformat(),
			"model": self.model_name,
		}
		with self.vector_ids_path.open("w", encoding="utf-8") as handle:
			json.dump(metadata, handle, indent=2)
		LOGGER.info("FAISS index rebuilt with %s vectors", len(chunk_ids))


def classify_source(relative_path: str) -> str:
	if relative_path.startswith("docs/tickets/"):
		return "tickets"
	if relative_path.startswith("archive/"):
		return "non-standard"
	return "documents"


def sha256_file(path: Path) -> str:
	digest = hashlib.sha256()
	with path.open("rb") as handle:
		for chunk in iter(lambda: handle.read(1024 * 1024), b""):
			digest.update(chunk)
	return digest.hexdigest()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Embedding maintenance utilities")
	parser.add_argument("command", choices=["rebuild", "sync", "plan", "drop"], help="Operation to run")
	parser.add_argument("paths", nargs="*", help="Optional file or directory paths for select commands")
	parser.add_argument("--model", dest="model", default=DEFAULT_MODEL_NAME, help="SentenceTransformer model name")
	parser.add_argument("--plan", dest="plan", action="store_true", help="Preview changes without writing state")
	return parser.parse_args(argv)


def expand_paths(repo_root: Path, inputs: Sequence[str]) -> set[str]:
	resolved: set[str] = set()
	for raw in inputs:
		absolute = (repo_root / raw).resolve()
		if absolute.is_dir():
			for path in absolute.rglob("*"):
				if path.is_file() and path.suffix.lower() in ALLOWED_SUFFIXES:
					resolved.add(path.relative_to(repo_root).as_posix())
		elif absolute.is_file():
			if absolute.suffix.lower() in ALLOWED_SUFFIXES:
				resolved.add(absolute.relative_to(repo_root).as_posix())
		else:
			LOGGER.warning("Path %s does not exist", raw)
	return resolved


def configure_logging() -> None:
	logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")


def main(argv: Sequence[str] | None = None) -> int:
	configure_logging()
	args = parse_args(argv)
	repo_root = Path(__file__).resolve().parent
	pipeline = EmbeddingPipeline(repo_root, model_name=args.model)

	if args.command == "rebuild":
		pipeline.rebuild(plan_only=args.plan)
	elif args.command == "sync":
		include = expand_paths(repo_root, args.paths)
		pipeline.sync(include=include or None, plan_only=args.plan)
	elif args.command == "plan":
		include = expand_paths(repo_root, args.paths)
		plan = pipeline.build_plan(include=include or None)
		LOGGER.info("Plan: %s", plan.describe())
		if plan.to_index:
			LOGGER.info("Files to index:\n%s", "\n".join(f"  - {path}" for path in plan.to_index))
		if plan.to_delete:
			LOGGER.info("Files to delete:\n%s", "\n".join(f"  - {path}" for path in plan.to_delete))
		if plan.skipped_tickets:
			LOGGER.info(
				"Tickets skipped (>%s days old):\n%s",
				MAX_TICKET_AGE_DAYS,
				"\n".join(f"  - {path}" for path in plan.skipped_tickets),
			)
	elif args.command == "drop":
		if not args.paths:
			LOGGER.error("drop command requires at least one path")
			return 1
		include = expand_paths(repo_root, args.paths)
		if not include:
			LOGGER.warning("No matching files to drop")
			return 0
		pipeline.drop(include)
	else:  # pragma: no cover - defensive
		LOGGER.error("Unknown command %s", args.command)
		return 1
	return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
	sys.exit(main())
