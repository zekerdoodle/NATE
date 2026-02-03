"""Application entrypoint wiring Nate's ticket listener into a simple scheduler."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, List, Optional

from dotenv import load_dotenv

from ticket_listener import NinjaOneClient, TicketListener, configure_logging
from ticket_parser import TicketParser
from ticket_worker import TicketAutomationWorker
from embed import EmbeddingPipeline, DEFAULT_MODEL_NAME


@dataclass
class ScheduledJob:
    """Definition for a recurring asynchronous job."""

    name: str
    interval_seconds: int
    runner: Callable[[], Awaitable[None]]
    initial_delay: int = 0


@dataclass
class AppConfig:
    poll_interval: int = 60
    page_size: int = 200
    state_filename: str = ".listener_state.json"
    automation_interval: int = 45
    automation_state_filename: str = ".automation_state.json"
    automation_enabled: bool = True
    verbose: bool = False
    run_once: bool = False
    reset_state: bool = False
    test_mode: bool = False


class SchedulerApp:
    """Lightweight scheduler that repeatedly runs asynchronous jobs."""

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._jobs: List[ScheduledJob] = []
        self._stop_event = asyncio.Event()
        self._logger = logger or logging.getLogger(__name__)

    def add_job(self, job: ScheduledJob) -> None:
        self._jobs.append(job)
        self._logger.debug("Registered job %s (interval=%ss)", job.name, job.interval_seconds)

    async def run(self) -> None:
        if not self._jobs:
            self._logger.warning("Scheduler started with no jobs")
            return

        tasks = [asyncio.create_task(self._run_job(job), name=job.name) for job in self._jobs]
        self._logger.info("Scheduler running with %d job(s)", len(tasks))
        try:
            await self._stop_event.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._logger.info("Scheduler stopped")

    async def _run_job(self, job: ScheduledJob) -> None:
        if job.initial_delay:
            self._logger.debug("Job %s initial delay %ss", job.name, job.initial_delay)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=job.initial_delay)
                return
            except asyncio.TimeoutError:
                pass

        while not self._stop_event.is_set():
            start = time.monotonic()
            try:
                await job.runner()
            except asyncio.CancelledError:  # pragma: no cover - cancelled during shutdown
                raise
            except Exception as exc:  # pragma: no cover - defensive guard
                self._logger.exception("Job %s failed: %s", job.name, exc)

            elapsed = time.monotonic() - start
            sleep_for = max(0.0, job.interval_seconds - elapsed)
            self._logger.debug("Job %s finished in %.2fs; sleeping %.2fs", job.name, elapsed, sleep_for)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_for)
                return
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        if not self._stop_event.is_set():
            self._stop_event.set()
            self._logger.info("Stop signal received")


async def _ticket_poll_runner(
    listener: TicketListener, 
    pipeline: EmbeddingPipeline, 
    lock: asyncio.Lock
) -> None:
    result = await asyncio.to_thread(listener.poll_once)
    logging.getLogger(__name__).info("Ticket poll processed %s ticket(s)", result.processed)
    
    if result.processed > 0:
        async with lock:
            logging.getLogger(__name__).info("Triggering embedding sync due to new tickets...")
            await asyncio.to_thread(pipeline.sync)
            logging.getLogger(__name__).info("Embedding sync complete")


async def _automation_runner(worker: TicketAutomationWorker) -> None:
    results = await asyncio.to_thread(worker.process_pending)
    if results:
        logging.getLogger(__name__).info("Automation processed %s ticket(s)", len(results))


async def _embedding_sync_runner(pipeline: EmbeddingPipeline, lock: asyncio.Lock) -> None:
    async with lock:
        # We run sync periodically to catch any manual file updates
        # that might have been missed by the watchdog or if watchdog isn't running.
        await asyncio.to_thread(pipeline.sync)
        logging.getLogger(__name__).debug("Scheduled embedding sync complete")


def _load_dotenv(repo_root: Path) -> None:
    env_path = repo_root / "api_keys.env"
    if env_path.exists():
        load_dotenv(env_path)
    else:
        load_dotenv()


def _build_ticket_listener(repo_root: Path, config: AppConfig) -> TicketListener:
    client_id = os.getenv("NinjaOne_ClientID")
    client_secret = os.getenv("NinjaOne_ClientSecret")
    base_url = os.getenv("NinjaOne_BaseURL", "https://app.ninjarmm.com")
    refresh_token = os.getenv("NINJA_REFRESH_TOKEN")

    if not client_id or not client_secret:
        raise RuntimeError("NinjaOne credentials missing; set NinjaOne_ClientID and NinjaOne_ClientSecret in api_keys.env")

    tickets_dir = repo_root / "docs" / "tickets"
    state_path = tickets_dir / config.state_filename

    client = NinjaOneClient(
        base_url=base_url, 
        client_id=client_id, 
        client_secret=client_secret,
        refresh_token=refresh_token
    )
    parser = TicketParser(tickets_dir)
    listener = TicketListener(
        client,
        parser,
        state_path,
        poll_interval=config.poll_interval,
        page_size=config.page_size,
        test_mode=config.test_mode,
    )
    if config.reset_state:
        listener.reset_state()
    return listener


def parse_args(argv: Optional[List[str]] = None) -> AppConfig:
    parser = argparse.ArgumentParser(description="Run Nate's background automations")
    parser.add_argument("--poll-interval", type=int, default=60, help="Seconds between ticket polling runs")
    parser.add_argument("--page-size", type=int, default=200, help="Number of tickets to request per board call")
    parser.add_argument("--state-filename", default=".listener_state.json", help="State file name stored alongside ticket outputs")
    parser.add_argument("--automation-interval", type=int, default=45, help="Seconds between automation worker runs")
    parser.add_argument(
        "--automation-state-filename",
        default=".automation_state.json",
        help="State file name for the automation worker",
    )
    parser.add_argument("--disable-automation", action="store_true", help="Disable the automation worker")
    parser.add_argument("--run-once", action="store_true", help="Process a single ticket poll run then exit")
    parser.add_argument("--reset-state", action="store_true", help="Reset persisted poll state before starting")
    parser.add_argument("--test-mode", action="store_true", help="Only process tickets assigned to a specific technician from today")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    return AppConfig(
        poll_interval=args.poll_interval,
        page_size=args.page_size,
        state_filename=args.state_filename,
        automation_interval=args.automation_interval,
        automation_state_filename=args.automation_state_filename,
        automation_enabled=not args.disable_automation,
        verbose=args.verbose,
        run_once=args.run_once,
        reset_state=args.reset_state,
        test_mode=args.test_mode,
    )


def main(argv: Optional[List[str]] = None) -> None:
    config = parse_args(argv)
    repo_root = Path(__file__).resolve().parent
    configure_logging(config.verbose, log_file=repo_root / "logs" / "nate.log")

    _load_dotenv(repo_root)

    if config.test_mode:
        os.environ["NATE_TEST_MODE"] = "1"
        logging.getLogger(__name__).info("Running in TEST MODE: Only processing specified technician's tickets from today")

    listener = _build_ticket_listener(repo_root, config)
    worker: Optional[TicketAutomationWorker] = None
 
    if config.automation_enabled:
        worker = TicketAutomationWorker(
            repo_root,
            state_filename=config.automation_state_filename,
        )
        if config.reset_state:
            worker.reset_state()

    # Initialize embedding pipeline
    pipeline = EmbeddingPipeline(repo_root, model_name=DEFAULT_MODEL_NAME)
    # Prepare the pipeline (load model, check DB)
    pipeline.prepare()
    pipeline_lock = asyncio.Lock()

    if config.run_once:
        try:
            result = listener.poll_once()
            logging.getLogger(__name__).info("Single poll processed %s ticket(s)", result.processed)
            if result.processed > 0:
                pipeline.sync()
                logging.getLogger(__name__).info("Embedding sync complete")

            if worker:
                automation_results = worker.process_pending()
                logging.getLogger(__name__).info(
                    "Automation processed %s ticket(s)",
                    len(automation_results),
                )
        finally:
            listener.client.close()
        return

    # --- Web Server & Scheduler Integration ---
    import uvicorn
    from server import app as fastapi_app
    from embedding_watchdog import start_watchdog

    scheduler = SchedulerApp(logger=logging.getLogger("scheduler"))
    scheduler.add_job(
        ScheduledJob(
            name="ticket_poll",
            interval_seconds=config.poll_interval,
            runner=lambda: _ticket_poll_runner(listener, pipeline, pipeline_lock),
        )
    )
    if worker:
        scheduler.add_job(
            ScheduledJob(
                name="ticket_automation",
                interval_seconds=max(5, config.automation_interval),
                runner=lambda: _automation_runner(worker),
            )
        )
    
    # Define startup/shutdown events for the scheduler
    @fastapi_app.on_event("startup")
    async def start_scheduler():
        # Run scheduler in the background
        asyncio.create_task(scheduler.run())
        # Start watchdog
        fastapi_app.state.watchdog = start_watchdog(repo_root)

    @fastapi_app.on_event("shutdown")
    async def stop_scheduler():
        scheduler.stop()
        listener.client.close()
        if hasattr(fastapi_app.state, "watchdog"):
            fastapi_app.state.watchdog.stop()
            fastapi_app.state.watchdog.join()

    logging.getLogger(__name__).info("Starting Web Server on port 8000...")
    uvicorn.run(fastapi_app, host="0.0.0.0", port=8000, log_level="info" if config.verbose else "warning")


if __name__ == "__main__":
    main()
