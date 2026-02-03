"""Automation worker that runs Nate's end-to-end ticket workflow."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from model_call import (
    DEFAULT_CONFIG_PATH,
    TICKETS_DIR,
    ModelCallError,
    ModelRunResult,
    NateModelCaller,
    NateModelConfig,
)


LOGGER = logging.getLogger(__name__)


@dataclass
class AutomationResult:
    ticket_path: Path
    ticket_id: Optional[int]
    run: ModelRunResult
    processed_at: datetime
    log_path: Optional[Path]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticket_path": str(self.ticket_path),
            "ticket_id": self.ticket_id,
            "processed_at": self.processed_at.isoformat(),
            "log_path": str(self.log_path) if self.log_path else None,
            "response_id": self.run.response_id,
            "status": self.run.status,
            "tool_calls": self.run.tool_calls_as_dicts(),
            "output_text": getattr(self.run.response, "output_text", None),
            "prompt_metadata": self.run.prompt_metadata,
        }


class AutomationState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._records: Dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Unable to read automation state %s: %s", self.path, exc)
            return
        records = payload.get("tickets", {}) if isinstance(payload, dict) else {}
        for key, value in records.items():
            try:
                self._records[str(key)] = float(value)
            except (TypeError, ValueError):
                continue

    def save(self) -> None:
        if not self.path.parent.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"tickets": self._records}
        tmp_path = self.path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        tmp_path.replace(self.path)

    def reset(self) -> None:
        self._records.clear()
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                LOGGER.debug("Unable to remove automation state file %s", self.path)

    def needs_processing(self, key: str, mtime: float) -> bool:
        previous = self._records.get(key)
        if previous is None:
            return True
        # Allow for filesystem timestamp rounding differences.
        return mtime > previous + 1e-6

    def mark_processed(self, key: str, mtime: float) -> None:
        self._records[key] = mtime


CallerFactory = Callable[[Path, NateModelConfig], NateModelCaller]


class TicketAutomationWorker:
    def __init__(
        self,
        repo_root: Path,
        *,
        tickets_dir: Optional[Path] = None,
        state_filename: str = ".automation_state.json",
        config_path: Optional[Path] = None,
        caller_factory: Optional[CallerFactory] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.repo_root = repo_root
        self.tickets_dir = (tickets_dir or (repo_root / TICKETS_DIR)).resolve()
        self.tickets_dir.mkdir(parents=True, exist_ok=True)
        self._logger = logger or logging.getLogger(f"{__name__}.worker")

        config_path = config_path or DEFAULT_CONFIG_PATH
        if not config_path.is_absolute():
            config_path = self.repo_root / config_path
        self._config_path = config_path
        self._config: Optional[NateModelConfig] = None

        state_path = self.tickets_dir / state_filename
        self._state = AutomationState(state_path)

        self._caller_factory: CallerFactory = caller_factory or self._default_caller_factory
        self._caller: Optional[NateModelCaller] = None
        self.startup_time = datetime.now(timezone.utc)

    @property
    def state_path(self) -> Path:
        return self._state.path

    def reset_state(self) -> None:
        self._state.reset()

    def process_pending(self) -> List[AutomationResult]:
        pending = self._pending_ticket_paths()
        results: List[AutomationResult] = []
        if not pending:
            return results

        for ticket_path, mtime in pending:
            if os.getenv("NATE_TEST_MODE"):
                if not self._is_test_mode_eligible(ticket_path):
                    continue

            try:
                result = self._process_ticket(ticket_path)
            except ModelCallError as exc:
                self._logger.error("Automation failed for %s: %s", ticket_path.name, exc)
                continue
            except Exception as exc:  # pragma: no cover - defensive guard
                self._logger.exception("Unexpected automation error for %s: %s", ticket_path, exc)
                continue

            results.append(result)
            self._state.mark_processed(self._relative_key(ticket_path), mtime)

        if results:
            try:
                self._state.save()
            except OSError as exc:
                self._logger.warning("Unable to persist automation state: %s", exc)
        return results

    def _process_ticket(self, ticket_path: Path) -> AutomationResult:
        caller = self._get_caller()
        run_result = caller.invoke(str(ticket_path))
        ticket_id = self._coerce_ticket_id(run_result.ticket.get("ticket_id"))
        processed_at = datetime.now(timezone.utc)
        log_path = self._write_run_log(ticket_path, run_result, processed_at)
        self._logger.info(
            "Automated ticket %s (response %s)",
            ticket_id or ticket_path.name,
            run_result.response_id,
        )
        return AutomationResult(
            ticket_path=ticket_path,
            ticket_id=ticket_id,
            run=run_result,
            processed_at=processed_at,
            log_path=log_path,
        )

    def _pending_ticket_paths(self) -> List[Tuple[Path, float]]:
        if not self.tickets_dir.exists():
            return []
        pending: List[Tuple[Path, float]] = []
        for path in self.tickets_dir.glob("*.json"):
            if path.name.startswith("."):
                continue
            if path.name.endswith(".automation.json"):
                continue
            try:
                stat_result = path.stat()
            except OSError:
                continue
            key = self._relative_key(path)
            if self._state.needs_processing(key, stat_result.st_mtime):
                pending.append((path, stat_result.st_mtime))
        pending.sort(key=lambda item: (item[1], item[0].name))
        return pending

    def _relative_key(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.repo_root))
        except ValueError:
            return str(path.resolve())

    def _get_caller(self) -> NateModelCaller:
        if self._caller is None:
            config = self._load_config()
            self._caller = self._caller_factory(self.repo_root, config)
        return self._caller

    def _load_config(self) -> NateModelConfig:
        if self._config is None:
            self._config = NateModelConfig.load(self._config_path)
        return self._config

    @staticmethod
    def _default_caller_factory(repo_root: Path, config: NateModelConfig) -> NateModelCaller:
        return NateModelCaller(repo_root, config)

    @staticmethod
    def _coerce_ticket_id(value: Any) -> Optional[int]:
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
        return None

    def _write_run_log(
        self,
        ticket_path: Path,
        run_result: ModelRunResult,
        processed_at: datetime,
    ) -> Optional[Path]:
        log_path = ticket_path.with_suffix(".automation.json")
        resolved = ticket_path.resolve()
        try:
            ticket_path_str = str(resolved.relative_to(self.repo_root))
        except ValueError:
            ticket_path_str = str(resolved)

        response_payload: Any
        if hasattr(run_result.response, "model_dump"):
            response_payload = run_result.response.model_dump()
        elif isinstance(run_result.response, dict):
            response_payload = run_result.response
        elif hasattr(run_result.response, "__dict__"):
            response_payload = dict(run_result.response.__dict__)
        else:
            response_payload = str(run_result.response)

        payload = {
            "processed_at": processed_at.isoformat(),
            "ticket_path": ticket_path_str,
            "ticket_id": self._coerce_ticket_id(run_result.ticket.get("ticket_id")),
            "response_id": run_result.response_id,
            "status": run_result.status,
            "output_text": getattr(run_result.response, "output_text", None),
            "tool_calls": run_result.tool_calls_as_dicts(),
            "prompt_metadata": run_result.prompt_metadata,
            "model_response": response_payload,
        }
        tmp_path = log_path.with_suffix(".tmp")
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with tmp_path.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
            tmp_path.replace(log_path)
            return log_path
        except OSError as exc:
            self._logger.warning("Unable to write automation log %s: %s", log_path, exc)
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            return None

    def _is_test_mode_eligible(self, ticket_path: Path) -> bool:
        try:
            with ticket_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            
            # Check for specific technician ID (configure as needed)
            # Handle both raw NinjaOne format and parsed format
            assignee_id = data.get("assigned_technician_id")
            if assignee_id is None:
                assignee_id = data.get("assignedAppUserId")
                if assignee_id is None:
                    assignee = data.get("assignedTo")
                    if isinstance(assignee, dict):
                        assignee_id = assignee.get("id")
            
            if assignee_id != 5:
                return False
            
            # Check creation time against startup time
            created_dt = None
            
            # Try parsed format first (ISO string)
            created_at = data.get("created_at")
            if created_at:
                try:
                    created_dt = datetime.fromisoformat(created_at)
                    if created_dt.tzinfo is None:
                        created_dt = created_dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    pass
            
            # Try raw format (timestamp)
            if created_dt is None:
                create_time = data.get("createTime")
                if create_time:
                    try:
                        created_dt = datetime.fromtimestamp(float(create_time), tz=timezone.utc)
                    except (ValueError, TypeError):
                        pass
            
            if created_dt:
                # Strict check: must be created AFTER this worker started
                if created_dt < self.startup_time:
                    return False
            
            return True
        except (OSError, json.JSONDecodeError):
            return False


__all__ = ["TicketAutomationWorker", "AutomationResult"]
