"""Watchdog script to monitor docs/ directory and trigger embedding updates."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from embed import EmbeddingPipeline, DEFAULT_MODEL_NAME

LOGGER = logging.getLogger("watchdog")


class EmbeddingEventHandler(FileSystemEventHandler):
    """Handles file system events and triggers embedding sync."""

    def __init__(self, repo_root: Path, debounce_seconds: float = 5.0) -> None:
        self.repo_root = repo_root
        self.debounce_seconds = debounce_seconds
        self.last_trigger: float = 0.0
        self.pipeline = EmbeddingPipeline(repo_root, model_name=DEFAULT_MODEL_NAME)
        self.pipeline.prepare()

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        
        # Only care about modifications, creations, moves, or deletions
        if event.event_type not in ("modified", "created", "moved", "deleted"):
            return

        # Filter by allowed extensions (from embed.py logic, though we can just let sync handle it)
        # But to avoid noise, let's check extension.
        filename = str(event.src_path)
        if not any(filename.endswith(ext) for ext in (".md", ".txt", ".json", ".csv", ".log")):
            return

        now = time.time()
        if now - self.last_trigger < self.debounce_seconds:
            return

        self.last_trigger = now
        LOGGER.info("Detected change in %s; triggering sync...", event.src_path)
        try:
            # We run a full sync (incremental) to catch all changes
            self.pipeline.sync()
        except Exception as exc:
            LOGGER.error("Sync failed: %s", exc)


def start_watchdog(repo_root: Path) -> Observer:
    """Start the watchdog observer and return it."""
    event_handler = EmbeddingEventHandler(repo_root)
    observer = Observer()
    docs_dir = repo_root / "docs"
    
    if not docs_dir.exists():
        LOGGER.warning("Docs directory %s does not exist; creating it.", docs_dir)
        docs_dir.mkdir(parents=True, exist_ok=True)

    observer.schedule(event_handler, str(docs_dir), recursive=True)
    observer.start()
    LOGGER.info("Embedding watchdog started; monitoring %s", docs_dir)
    return observer


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    repo_root = Path(__file__).resolve().parent
    observer = start_watchdog(repo_root)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
