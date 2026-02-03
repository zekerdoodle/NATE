"""Model invocation utilities for Nate's helpdesk workflow."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from dotenv import load_dotenv
try:
    from openai import OpenAI
    from openai import APIError as OpenAIError
except ImportError:  # pragma: no cover - compatibility with legacy SDKs
    from openai import OpenAI
    from openai.error import OpenAIError  # type: ignore[attr-defined]
from tools import search as search_tool
from tools import read_file as read_file_tool
from tools import update_ticket as update_ticket_tool
from tools import get_ticket as get_ticket_tool
from tools.exceptions import ToolExecutionError
from tools.tool_schemas import get_tool_schemas


LOGGER = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("config/nate_model_config.json")
TICKETS_DIR = Path("docs/tickets")
TECH_INFO_PATH = Path("docs/tech_info/techinfo.csv")
TECH_SCHEDULE_PATH = Path("docs/tech_info/technician_schedule.csv")
EMPLOYEE_ROSTER_PATH = Path("docs/emp_info/Active EEs with Dept.csv")
REQUESTER_DIRECTORY_PATH = Path("docs/emp_info/requester_directory.json")

ToolExecutor = Callable[[Dict[str, Any], Path], Dict[str, Any]]


def _search_executor(parameters: Dict[str, Any], repo_root: Path) -> Dict[str, Any]:
    return search_tool.run(parameters, repo_root=repo_root)


def _read_file_executor(parameters: Dict[str, Any], repo_root: Path) -> Dict[str, Any]:
    return read_file_tool.run(parameters, repo_root=repo_root)


def _update_ticket_executor(parameters: Dict[str, Any], repo_root: Path) -> Dict[str, Any]:
    return update_ticket_tool.run(parameters, repo_root=repo_root)


def _get_ticket_executor(parameters: Dict[str, Any], repo_root: Path) -> Dict[str, Any]:
    return get_ticket_tool.run(parameters, repo_root=repo_root)


DEFAULT_TOOL_EXECUTORS: Dict[str, ToolExecutor] = {
    "search": _search_executor,
    "read_file": _read_file_executor,
    "update_ticket": _update_ticket_executor,
    "get_ticket": _get_ticket_executor,
}


@dataclass
class ToolCallRecord:
    tool_call_id: str
    response_id: str
    name: str
    arguments: Dict[str, Any]
    output: Any
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "response_id": self.response_id,
            "name": self.name,
            "arguments": self.arguments,
            "output": self.output,
            "error": self.error,
        }


@dataclass
class ModelRunResult:
    response: Any
    tool_calls: List[ToolCallRecord]
    ticket: Dict[str, Any]
    prompt_metadata: Dict[str, Any]

    @property
    def response_id(self) -> Optional[str]:
        return getattr(self.response, "id", None)

    @property
    def status(self) -> Optional[str]:
        return getattr(self.response, "status", None)

    def tool_calls_as_dicts(self) -> List[Dict[str, Any]]:
        return [record.to_dict() for record in self.tool_calls]

    def to_dict(self) -> Dict[str, Any]:
        response_payload: Any
        if hasattr(self.response, "model_dump"):
            response_payload = self.response.model_dump()
        else:
            response_payload = self.response
        return {
            "response": response_payload,
            "tool_calls": self.tool_calls_as_dicts(),
            "ticket": self.ticket,
            "prompt_metadata": self.prompt_metadata,
        }


@dataclass
class NateModelConfig:
    """Structured configuration for Nate's model calls."""

    model: str
    reasoning_effort: str
    system_instructions: str
    private_mode: bool = False

    @classmethod
    def load(cls, path: Path) -> "NateModelConfig":
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError as exc:
            raise ModelCallError(f"Configuration file not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ModelCallError(f"Configuration file {path} is not valid JSON") from exc

        missing = [key for key in ("model", "reasoning_effort", "system_instructions") if key not in payload]
        if missing:
            raise ModelCallError(f"Configuration file {path} is missing keys: {', '.join(missing)}")

        system_instructions = payload["system_instructions"]
        if system_instructions.strip().lower().endswith(".md"):
            instructions_path = path.parent / system_instructions
            try:
                with instructions_path.open("r", encoding="utf-8") as f:
                    system_instructions = f.read()
            except FileNotFoundError as exc:
                raise ModelCallError(f"System instructions file not found: {instructions_path}") from exc

        private_mode_raw = payload.get("private_mode", False)
        if isinstance(private_mode_raw, str):
            private_mode = private_mode_raw.strip().lower() in {"1", "true", "yes", "on"}
        else:
            private_mode = bool(private_mode_raw)

        return cls(
            model=str(payload["model"]),
            reasoning_effort=str(payload["reasoning_effort"]),
            system_instructions=system_instructions,
            private_mode=private_mode,
        )


class ModelCallError(RuntimeError):
    """Raised when prompt construction or invocation fails."""


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    """Convert an ISO 8601 string into an aware datetime if possible."""

    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _normalize_name(value: str) -> str:
    return value.strip().lower()


class NateModelCaller:
    """Build context for Nate and call the configured OpenAI model."""

    def __init__(
        self,
        repo_root: Path,
        config: NateModelConfig,
        *,
        client: Optional[OpenAI] = None,
        tool_registry: Optional[Mapping[str, ToolExecutor]] = None,
        response_poll_interval: float = 0.5,
    ) -> None:
        self.repo_root = repo_root
        self.config = config
        self._client: Optional[OpenAI] = client
        self._tech_cache: Optional[List[Dict[str, str]]] = None
        self._schedule_cache: Optional[List[Dict[str, Any]]] = None
        self._roster_cache: Optional[List[Dict[str, str]]] = None
        self._requester_directory: Optional[Dict[str, Dict[str, Any]]] = None
        self._tool_registry: Dict[str, ToolExecutor] = dict(DEFAULT_TOOL_EXECUTORS)
        if tool_registry:
            self._tool_registry.update(tool_registry)
        self._response_poll_interval = max(0.1, float(response_poll_interval))

        # Ensure API keys from api_keys.env are available before model instantiation.
        load_dotenv(self.repo_root / "api_keys.env")

    @property
    def tickets_dir(self) -> Path:
        return self.repo_root / TICKETS_DIR

    @property
    def client(self) -> OpenAI:
        if self._client is None:
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise ModelCallError("OPENAI_API_KEY is not set; update api_keys.env or environment variables.")
            self._client = OpenAI()
        return self._client

    # _complete_response_loop removed

    def _run_tool_call(
        self,
        response_id: str,
        tool_call: Any,
        ticket_data: Dict[str, Any],
    ) -> Tuple[ToolCallRecord, str]:
        function_payload = getattr(tool_call, "function", None)
        if function_payload is not None:
            tool_name = getattr(function_payload, "name", None) or "unknown"
            raw_arguments = getattr(function_payload, "arguments", None) or "{}"
        else:
            tool_name = getattr(tool_call, "name", None) or "unknown"
            raw_arguments = getattr(tool_call, "arguments", None) or "{}"
        try:
            arguments_obj = json.loads(raw_arguments) if raw_arguments else {}
        except json.JSONDecodeError as exc:
            error_message = f"Invalid JSON for tool '{tool_name}': {exc}"
            output: Dict[str, Any] = {"error": error_message, "raw_arguments": raw_arguments}
            record = ToolCallRecord(
                tool_call_id=str(getattr(tool_call, "id", getattr(tool_call, "call_id", ""))),
                response_id=str(response_id),
                name=tool_name,
                arguments={},
                output=output,
                error=error_message,
            )
            return record, json.dumps(output)

        if not isinstance(arguments_obj, dict):
            arguments_obj = {"value": arguments_obj}

        if (
            tool_name == "update_ticket"
            and "ticket_id" not in arguments_obj
            and isinstance(ticket_data.get("ticket_id"), (int, str))
        ):
            arguments_obj["ticket_id"] = ticket_data["ticket_id"]

        executor = self._tool_registry.get(tool_name)
        error: Optional[str] = None
        output: Dict[str, Any]

        def _execute(params: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
            if executor is None:
                return {"error": f"Tool '{tool_name}' is not available.", "arguments": params}, f"Tool '{tool_name}' is not available."
            try:
                return executor(params, self.repo_root), None
            except ToolExecutionError as exc:
                return {"error": str(exc)}, str(exc)
            except Exception as exc:  # pragma: no cover - defensive guard
                LOGGER.exception("Tool %s failed: %s", tool_name, exc)
                return {"error": f"Unhandled tool error: {exc}"}, f"Unhandled tool error: {exc}"

        output, error = _execute(arguments_obj)

        if tool_name == "update_ticket" and error:
            fallback_field_sets: List[Tuple[str, ...]] = [
                ("organization", "location"),
                ("ticket_status", "assignee"),
                ("public_comment",),
            ]
            for fields in fallback_field_sets:
                retry_args = dict(arguments_obj)
                removed = False
                for field in fields:
                    if field in retry_args:
                        retry_args.pop(field, None)
                        removed = True
                if not removed:
                    continue
                retry_output, retry_error = _execute(retry_args)
                if not retry_error:
                    arguments_obj = retry_args
                    output = retry_output
                    error = None
                    break
                output = retry_output
                error = retry_error

        record = ToolCallRecord(
            tool_call_id=str(getattr(tool_call, "id", getattr(tool_call, "call_id", ""))),
            response_id=str(response_id),
            name=tool_name,
            arguments=arguments_obj,
            output=output,
            error=error,
        )
        return record, json.dumps(output)

    def _create_response(
        self,
        *,
        input_items: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_output_tokens: Optional[int],
    ) -> Any:
        payload: Dict[str, Any] = {
            "model": self.config.model,
            "input": input_items,
            "tools": tools,
            "reasoning": {"effort": self.config.reasoning_effort},
        }
        if max_output_tokens is not None:
            payload["max_output_tokens"] = max_output_tokens
        try:
            return self.client.responses.create(**payload)
        except OpenAIError as exc:
            raise ModelCallError(f"OpenAI request failed: {exc}") from exc

    def _response_items_for_history(self, response: Any) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for output_item in getattr(response, "output", []) or []:
            item_type = getattr(output_item, "type", None)
            if item_type == "function_call":
                items.append(
                    {
                        "type": "function_call",
                        "id": getattr(output_item, "id", None),
                        "call_id": getattr(output_item, "call_id", None),
                        "name": getattr(output_item, "name", None),
                        "arguments": getattr(output_item, "arguments", None),
                    }
                )
            elif item_type == "reasoning":
                if hasattr(output_item, "model_dump"):
                    payload = output_item.model_dump()
                else:
                    payload = {
                        "type": "reasoning",
                        "id": getattr(output_item, "id", None),
                        "summary": getattr(output_item, "summary", None),
                        "content": getattr(output_item, "content", None),
                    }
                payload.pop("status", None)
                payload.pop("encrypted_content", None)
                items.append(payload)
        return items

    @staticmethod
    def _format_tools_for_responses(schemas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        formatted: List[Dict[str, Any]] = []
        for schema in schemas:
            if schema.get("type") == "function" and isinstance(schema.get("function"), dict):
                fn_schema = dict(schema["function"])
                payload = {"type": "function"}
                payload.update(fn_schema)
                formatted.append(payload)
            else:
                formatted.append(schema)
        return formatted

    def invoke(
        self,
        ticket_reference: str,
        *,
        dry_run: bool = False,
        max_output_tokens: Optional[int] = None,
    ) -> Any:
        ticket_path = self._resolve_ticket_path(ticket_reference)
        ticket_data = self._load_ticket(ticket_path)
        tool_schemas = self._load_tool_schemas()
        messages, prompt_metadata = self._build_messages(ticket_data, tool_schemas=tool_schemas)

        if dry_run:
            dry_payload = {
                "id": None,
                "status": "dry_run",
                "messages": messages,
                "tools": tool_schemas,
            }
            return ModelRunResult(
                response=dry_payload,
                tool_calls=[],
                ticket=json.loads(json.dumps(ticket_data)),
                prompt_metadata=json.loads(json.dumps(prompt_metadata)),
            )

        return self.run_conversation(
            messages=messages,
            tool_schemas=tool_schemas,
            ticket_data=ticket_data,
            prompt_metadata=prompt_metadata,
            max_output_tokens=max_output_tokens,
        )

    def run_conversation(
        self,
        *,
        messages: List[Dict[str, Any]],
        tool_schemas: Optional[List[Dict[str, Any]]] = None,
        ticket_data: Optional[Dict[str, Any]] = None,
        prompt_metadata: Optional[Dict[str, Any]] = None,
        max_output_tokens: Optional[int] = None,
    ) -> ModelRunResult:
        ticket_data = ticket_data or {}
        prompt_metadata = prompt_metadata or {}
        
        input_items: List[Dict[str, Any]] = json.loads(json.dumps(messages))
        tools_payload: List[Dict[str, Any]] = (
            self._format_tools_for_responses(tool_schemas) if tool_schemas else []
        )
        tools_payload.append({"type": "web_search"})

        response = self._create_response(
            input_items=input_items,
            tools=tools_payload,
            max_output_tokens=max_output_tokens,
        )
        tool_runs: List[ToolCallRecord] = []

        while True:
            history_items = self._response_items_for_history(response)
            if history_items:
                input_items.extend(history_items)

            function_calls = [
                item for item in getattr(response, "output", []) if getattr(item, "type", None) == "function_call"
            ]
            if not function_calls:
                break

            appended_outputs: List[Dict[str, Any]] = []
            for function_call in function_calls:
                record, payload = self._run_tool_call(response.id or "", function_call, ticket_data)
                tool_runs.append(record)
                appended_outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": getattr(function_call, "call_id", getattr(function_call, "id", "")),
                        "output": payload,
                    }
                )

            input_items.extend(appended_outputs)
            response = self._create_response(
                input_items=input_items,
                tools=tools_payload,
                max_output_tokens=max_output_tokens,
            )

        return ModelRunResult(
            response=response,
            tool_calls=tool_runs,
            ticket=json.loads(json.dumps(ticket_data)),
            prompt_metadata=json.loads(json.dumps(prompt_metadata)),
        )


    def _build_messages(
        self,
        ticket: Dict[str, Any],
        *,
        tool_schemas: Optional[List[Dict[str, Any]]] = None,
    ) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
        current_utc = datetime.now(timezone.utc)
        local_today = datetime.now().date()

        # Load optional tag metadata so the model can reference the canonical list.
        tags_list: List[str] = []
        tags_path = self.repo_root / "docs" / "tags.json"
        if tags_path.exists():
            try:
                tags_list = json.loads(tags_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                LOGGER.warning("Failed to load tags.json: %s", exc)

        system_instructions = (self.config.system_instructions or "").strip()
        if tags_list:
            tags_str = ", ".join(tags_list)
            system_instructions = f"{system_instructions}\n\n# Valid Tags List\n{tags_str}".strip()
        system_content = system_instructions or "You are Nate, an IT technician. Follow the rules."

        # Gather contextual data for the user portion of the prompt.
        requester_profile = self._build_requester_profile(ticket)
        recent_tickets = self._find_recent_tickets(ticket, window_days=7, now=current_utc)
        technicians = self._load_technician_roster()
        schedule_entries = self._schedule_for_date(local_today)
        knowledge = self._gather_knowledge_hits(ticket)

        context_parts: List[str] = [
            f"Current UTC datetime: {current_utc.isoformat()}",
            f"Current local date: {local_today.isoformat()}",
            "Ticket payload (parsed):\n" + json.dumps(ticket, indent=2, sort_keys=True),
        ]

        if requester_profile:
            context_parts.append("Requester profile:\n" + json.dumps(requester_profile, indent=2, sort_keys=True))
        else:
            context_parts.append(f"Requester profile: unavailable for UID {ticket.get('requester_uid')}")

        if recent_tickets:
            context_parts.append("Recent requester tickets (last 7 days):\n" + json.dumps(recent_tickets, indent=2))
        else:
            context_parts.append("Recent requester tickets: none within the last 7 days.")

        if technicians:
            context_parts.append("Technician roster:\n" + json.dumps(technicians, indent=2))
        else:
            context_parts.append("Technician roster: unavailable (docs/tech_info/techinfo.csv missing).")

        if schedule_entries:
            context_parts.append(
                f"Technician availability for {local_today.isoformat()}:\n" + json.dumps(schedule_entries, indent=2)
            )
        else:
            context_parts.append(
                "Technician availability: no schedule rows found for today. Update docs/tech_info/technician_schedule.csv."
            )

        if knowledge is None:
            context_parts.append("Knowledge base search results: no query generated from ticket content.")
        elif "error" in knowledge:
            context_parts.append("Knowledge base search error:\n" + json.dumps(knowledge, indent=2))
        else:
            context_parts.append("Knowledge base search results:\n" + json.dumps(knowledge, indent=2))

        user_content: List[Dict[str, Any]] = [{"type": "input_text", "text": "\n\n".join(context_parts)}]

        # Append ticket imagery for multimodal-capable models.
        found_images: List[str] = []
        seen_images: set[str] = set()
        for category in ("public_updates", "private_updates", "system_updates"):
            for update in ticket.get(category, []):
                if not isinstance(update, dict):
                    continue
                for image_ref in update.get("images") or []:
                    if not isinstance(image_ref, str):
                        continue
                    if not image_ref or image_ref in seen_images:
                        continue
                    seen_images.add(image_ref)
                    found_images.append(image_ref)

        for image_ref in found_images:
            user_content.append({"type": "input_image", "image_url": image_ref})

        prompt_metadata = {
            "requester_profile_available": bool(requester_profile),
            "recent_ticket_count": len(recent_tickets),
            "technician_count": len(technicians),
            "schedule_count": len(schedule_entries),
            "knowledge_hit_count": len(knowledge.get("results", [])) if isinstance(knowledge, dict) else 0,
            "image_count": len(found_images),
        }

        messages = [
            {"role": "system", "content": [{"type": "input_text", "text": system_content}]},
            {"role": "user", "content": user_content},
        ]
        return messages, prompt_metadata

    def _resolve_ticket_path(self, reference: str) -> Path:
        candidate = Path(reference)
        if candidate.is_file():
            return candidate
        if not candidate.is_absolute():
            absolute = self.repo_root / candidate
            if absolute.is_file():
                return absolute
        if reference.isdigit():
            ticket_file = self.tickets_dir / f"{reference}.json"
            if ticket_file.is_file():
                return ticket_file
        raise ModelCallError(f"Cannot locate ticket payload for reference '{reference}'.")

    def _load_ticket(self, path: Path) -> Dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError as exc:
            raise ModelCallError(f"Ticket file not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ModelCallError(f"Ticket file {path} is not valid JSON") from exc

    # _build_prompt removed in favor of _build_messages

    def _build_requester_profile(self, ticket: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        uid = ticket.get("requester_uid")
        if not uid:
            return None

        directory = self._load_requester_directory()
        profile = directory.get(uid)
        if not profile:
            return None

        profile_copy = dict(profile)
        full_name = profile_copy.get("full_name") or profile_copy.get("name")
        if full_name:
            roster_entry = self._lookup_employee_by_name(full_name)
            if roster_entry:
                profile_copy.setdefault("job_title", roster_entry.get("job_title"))
                profile_copy.setdefault("work_location", roster_entry.get("work_location"))

        return profile_copy

    def _load_requester_directory(self) -> Dict[str, Dict[str, Any]]:
        if self._requester_directory is not None:
            return self._requester_directory

        path = self.repo_root / REQUESTER_DIRECTORY_PATH
        if not path.exists():
            self._requester_directory = {}
            return self._requester_directory

        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except json.JSONDecodeError:
            LOGGER.warning("Requester directory %s is not valid JSON; ignoring", path)
            payload = {}

        directory: Dict[str, Dict[str, Any]]
        if isinstance(payload, dict):
            directory = {str(key): value for key, value in payload.items() if isinstance(value, dict)}
        elif isinstance(payload, list):
            directory = {}
            for item in payload:
                if isinstance(item, dict):
                    uid = item.get("uid") or item.get("requester_uid")
                    if uid:
                        directory[str(uid)] = item
        else:
            directory = {}

        self._requester_directory = directory
        return directory

    def _lookup_employee_by_name(self, full_name: str) -> Optional[Dict[str, str]]:
        roster = self._load_employee_roster()
        normalized = _normalize_name(full_name)
        for entry in roster:
            if entry.get("_normalized") == normalized:
                return entry
        return None

    def _load_employee_roster(self) -> List[Dict[str, str]]:
        if self._roster_cache is not None:
            return self._roster_cache

        path = self.repo_root / EMPLOYEE_ROSTER_PATH
        if not path.exists():
            self._roster_cache = []
            return self._roster_cache

        entries: List[Dict[str, str]] = []
        with path.open("r", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                full_name = row.get("Full Name")
                if not full_name:
                    continue
                entries.append(
                    {
                        "full_name": full_name.strip(),
                        "job_title": (row.get("Job Title") or "").strip(),
                        "work_location": (row.get("Work Location") or "").strip(),
                        "_normalized": _normalize_name(full_name),
                    }
                )
        self._roster_cache = entries
        return entries

    def _find_recent_tickets(
        self,
        ticket: Dict[str, Any],
        *,
        window_days: int,
        now: datetime,
    ) -> List[Dict[str, Any]]:
        uid = ticket.get("requester_uid")
        if not uid:
            return []

        cutoff = now - timedelta(days=window_days)
        results: List[Dict[str, Any]] = []

        if not self.tickets_dir.exists():
            return []

        for path in sorted(self.tickets_dir.glob("*.json")):
            if path.name.startswith("."):
                continue
            try:
                with path.open("r", encoding="utf-8") as handle:
                    candidate = json.load(handle)
            except json.JSONDecodeError:
                continue
            if candidate.get("ticket_id") == ticket.get("ticket_id"):
                continue
            if candidate.get("requester_uid") != uid:
                continue
            last_activity = _parse_iso_datetime(candidate.get("last_activity_at"))
            if last_activity and last_activity >= cutoff:
                results.append(
                    {
                        "ticket_id": candidate.get("ticket_id"),
                        "subject": candidate.get("subject"),
                        "status": (candidate.get("status") or {}).get("display_name")
                        or (candidate.get("status") or {}).get("name"),
                        "last_activity_at": candidate.get("last_activity_at"),
                    }
                )

        results.sort(key=lambda item: item.get("last_activity_at") or "", reverse=True)
        return results

    def _load_technician_roster(self) -> List[Dict[str, str]]:
        if self._tech_cache is not None:
            return self._tech_cache

        path = self.repo_root / TECH_INFO_PATH
        if not path.exists():
            self._tech_cache = []
            return self._tech_cache

        entries: List[Dict[str, str]] = []
        with path.open("r", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                tech = row.get("Technician")
                if not tech:
                    continue
                entries.append(
                    {
                        "technician": tech.strip(),
                        "job_title": (row.get("Job Title") or "").strip(),
                        "specialties": (row.get("Specialties") or "").strip(),
                        "hours_of_operation": (row.get("Hours of Operation") or "").strip(),
                        "work_style": (row.get("Remote/Onsite/Hybrid") or "").strip(),
                        "location": (row.get("Location") or "").strip(),
                    }
                )
        self._tech_cache = entries
        return entries

    def _load_schedule(self) -> List[Dict[str, Any]]:
        if self._schedule_cache is not None:
            return self._schedule_cache

        path = self.repo_root / TECH_SCHEDULE_PATH
        if not path.exists():
            self._schedule_cache = []
            return self._schedule_cache

        entries: List[Dict[str, Any]] = []
        with path.open("r", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                date_str = (row.get("Date") or "").strip()
                tech = (row.get("Technician") or "").strip()
                if not date_str or not tech:
                    continue
                try:
                    entry_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                except ValueError:
                    LOGGER.debug("Skipping schedule row with invalid date '%s'", date_str)
                    continue
                entries.append(
                    {
                        "date": entry_date,
                        "technician": tech,
                        "status": (row.get("Status") or "").strip(),
                        "notes": (row.get("Notes") or "").strip(),
                    }
                )
        self._schedule_cache = entries
        return entries

    def _schedule_for_date(self, target: date) -> List[Dict[str, Any]]:
        schedule = self._load_schedule()
        entries: List[Dict[str, Any]] = []
        for item in schedule:
            if item.get("date") == target:
                entries.append(
                    {
                        "technician": item.get("technician"),
                        "status": item.get("status"),
                        "notes": item.get("notes"),
                    }
                )
        return entries

    def _gather_knowledge_hits(self, ticket: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        query = self._compose_search_query(ticket)
        if not query:
            return None
        try:
            payload = search_tool.run(
                {"query": query, "limit": 5, "return_content": False}, repo_root=self.repo_root
            )
        except ToolExecutionError as exc:
            LOGGER.warning("Knowledge search failed for ticket %s: %s", ticket.get("ticket_id"), exc)
            return {"query": query, "error": str(exc)}
        return self._format_search_payload(payload)

    def _compose_search_query(self, ticket: Dict[str, Any]) -> Optional[str]:
        subject = (ticket.get("subject") or "").strip()
        description = (ticket.get("description") or "").strip()
        latest_comment = self._latest_public_comment(ticket)
        parts = [part for part in (subject, description, latest_comment) if part]
        if not parts:
            ticket_id = ticket.get("ticket_id")
            if ticket_id:
                return f"Ticket {ticket_id}"
            return None
        query = " ".join(parts)
        return query[:600]

    def _latest_public_comment(self, ticket: Dict[str, Any]) -> Optional[str]:
        updates = ticket.get("public_updates") or []
        for update in reversed(updates):
            if isinstance(update, dict):
                for key in ("body", "text", "comment", "content"):
                    value = (update.get(key) or "").strip()
                    if value:
                        return value
        return None

    def _format_search_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        def _trim_snippet(snippet: str) -> str:
            if len(snippet) <= 400:
                return snippet
            return snippet[:397] + "..."

        def _normalise_items(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
            formatted: List[Dict[str, Any]] = []
            for item in items:
                snippet = _trim_snippet(str(item.get("snippet", "")))
                metadata = item.get("metadata") or {}
                formatted.append(
                    {
                        "path": item.get("path"),
                        "score": round(float(item.get("score", 0.0)), 4),
                        "start_line": item.get("start_line"),
                        "end_line": item.get("end_line"),
                        "chunk_id": item.get("chunk_id"),
                        "snippet": snippet,
                        "source": metadata.get("source"),
                        "updated_at": metadata.get("updated_at"),
                    }
                )
            return formatted

        return {
            "query": payload.get("query"),
            "results": _normalise_items(payload.get("results", [])),
            "low_confidence": _normalise_items(payload.get("results_low_confidence", [])),
        }

    def _load_tool_schemas(self) -> List[Dict[str, Any]]:
        return get_tool_schemas()


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Call Nate's model with a parsed ticket file")
    parser.add_argument("ticket", help="Ticket ID or path to parsed ticket JSON")
    parser.add_argument(
        "--config",
        type=str,
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to Nate's model configuration file",
    )
    parser.add_argument("--dry-run", action="store_true", help="Build the prompt but skip the API call")
    parser.add_argument("--max-output-tokens", type=int, help="Optional max_output_tokens limit for the response")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging for troubleshooting")
    return parser.parse_args(argv)


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s")


def main(argv: Optional[Iterable[str]] = None) -> None:
    args = parse_args(argv)
    _configure_logging(args.verbose)

    repo_root = Path(__file__).resolve().parent
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = repo_root / config_path

    try:
        config = NateModelConfig.load(config_path)
        caller = NateModelCaller(repo_root, config)
        run_result = caller.invoke(
            args.ticket,
            dry_run=args.dry_run,
            max_output_tokens=args.max_output_tokens,
        )
    except ModelCallError as exc:
        LOGGER.error("%s", exc)
        raise SystemExit(1) from exc

    if args.dry_run:
        response_payload = run_result.response if isinstance(run_result.response, dict) else {}
        print("--- MESSAGES ---")
        print(json.dumps(response_payload.get("messages", []), indent=2))
        print("\n--- TOOL SCHEMAS ---")
        print(json.dumps(response_payload.get("tools", []), indent=2))
        print("\n--- PROMPT METADATA ---")
        print(json.dumps(run_result.prompt_metadata, indent=2))
        return

    response = run_result.response
    output_text = getattr(response, "output_text", None)
    if output_text:
        print(output_text)
    elif hasattr(response, "model_dump"):
        print(json.dumps(response.model_dump(), indent=2))
    else:
        print(response)
    tool_calls = run_result.tool_calls_as_dicts()
    if tool_calls:
        print("\n--- TOOL CALLS ---")
        print(json.dumps(tool_calls, indent=2))


if __name__ == "__main__":
    main()
