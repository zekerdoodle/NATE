"""Tests for the ticket automation worker and multi-turn workflow."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List
from unittest import TestCase
from unittest.mock import patch

from model_call import NateModelConfig, NateModelCaller
from ticket_worker import TicketAutomationWorker


class FakeFunctionCall:
    def __init__(self, tool_id: str, call_id: str, name: str, arguments: Dict[str, Any]) -> None:
        self.id = tool_id
        self.call_id = call_id
        self.name = name
        self.arguments = json.dumps(arguments)
        self.type = "function_call"


class FakeReasoningItem:
    def __init__(self, item_id: str) -> None:
        self.id = item_id
        self.type = "reasoning"
        self.summary: List[Any] = []
        self.content: List[Any] | None = None

    def model_dump(self) -> Dict[str, Any]:
        return {"type": "reasoning", "id": self.id, "summary": self.summary, "content": self.content}


class FakeFinalMessage:
    def __init__(self, text: str) -> None:
        self.type = "message"
        self.role = "assistant"
        self.content = [{"type": "output_text", "text": text}]


class FakeResponse:
    def __init__(self, response_id: str, output: List[Any], *, output_text: str | None = None) -> None:
        self.id = response_id
        self.status = "completed"
        self.output = output
        self.output_text = output_text

    def model_dump(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "output_text": self.output_text,
        }


class FakeResponsesAPI:
    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.tool_outputs: List[Dict[str, Any]] = []
        self._response_id = "resp-123"
        self._call_id = "call-abc"

    def create(self, **kwargs: Any) -> FakeResponse:
        self.requests.append(kwargs)
        input_items = kwargs.get("input", []) or []
        has_output = any(isinstance(item, dict) and item.get("type") == "function_call_output" for item in input_items)
        if not has_output:
            reasoning = FakeReasoningItem("reason-1")
            tool_call = FakeFunctionCall("tool-1", self._call_id, "update_ticket", {"public_comment": "Test message"})
            return FakeResponse(self._response_id, [reasoning, tool_call])

        outputs = [item for item in input_items if isinstance(item, dict) and item.get("type") == "function_call_output"]
        self.tool_outputs.extend(outputs)
        message = FakeFinalMessage("All done")
        return FakeResponse(self._response_id, [message], output_text="All done")


class FakeOpenAI:
    def __init__(self) -> None:
        self.responses = FakeResponsesAPI()


class TicketAutomationWorkerTests(TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.repo_root = Path(self._tempdir.name)
        (self.repo_root / "docs" / "tickets").mkdir(parents=True)
        (self.repo_root / "config").mkdir(parents=True)
        (self.repo_root / "api_keys.env").write_text("OPENAI_API_KEY=dummy", encoding="utf-8")

        config_payload = {
            "model": "gpt-test",
            "reasoning_effort": "medium",
            "system_instructions": "Be helpful",
        }
        config_path = self.repo_root / "config" / "nate_model_config.json"
        config_path.write_text(json.dumps(config_payload), encoding="utf-8")

        self.ticket_payload = {
            "ticket_id": 42,
            "subject": "Printer jam",
            "description": "Paper stuck in tray",
            "requester_uid": "user-1",
            "last_activity_at": "2025-11-11T12:00:00+00:00",
            "public_updates": [],
        }
        self.ticket_path = self.repo_root / "docs" / "tickets" / "42.json"
        self.ticket_path.write_text(json.dumps(self.ticket_payload), encoding="utf-8")

        self.fake_client = FakeOpenAI()
        self.tool_invocations: List[Dict[str, Any]] = []

        def update_ticket_stub(params: Dict[str, Any], repo_root: Path) -> Dict[str, Any]:
            _ = repo_root
            self.tool_invocations.append(dict(params))
            return {"updated": True, "params": dict(params)}

        self.tool_registry = {"update_ticket": update_ticket_stub}

        config = NateModelConfig(
            model="gpt-test",
            reasoning_effort="medium",
            system_instructions="Be helpful",
        )

        def caller_factory(repo_root: Path, _: NateModelConfig) -> NateModelCaller:
            return NateModelCaller(
                repo_root,
                config,
                client=self.fake_client,
                tool_registry=self.tool_registry,
            )

        self.caller_factory = caller_factory
        self.worker = TicketAutomationWorker(
            self.repo_root,
            caller_factory=self.caller_factory,
            state_filename=".automation_state.json",
        )

    def test_worker_processes_ticket_and_logs_results(self) -> None:
        fake_search_payload = {
            "query": "Printer jam Paper stuck in tray",
            "results": [],
            "results_low_confidence": [],
        }

        with patch("model_call.search_tool.run", return_value=fake_search_payload):
            results = self.worker.process_pending()

        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.ticket_id, 42)
        self.assertIsNotNone(result.log_path)
        self.assertEqual(len(self.tool_invocations), 1)
        self.assertIn("ticket_id", self.tool_invocations[0])
        self.assertEqual(self.tool_invocations[0]["ticket_id"], 42)
        self.assertTrue(self.fake_client.responses.tool_outputs)
        tool_output_payload = json.loads(self.fake_client.responses.tool_outputs[0]["output"])
        self.assertTrue(tool_output_payload.get("updated"))

        log_data = json.loads(result.log_path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        self.assertEqual(log_data["ticket_id"], 42)
        self.assertEqual(log_data["status"], "completed")
        self.assertEqual(len(log_data["tool_calls"]), 1)

        with patch("model_call.search_tool.run", return_value=fake_search_payload):
            repeat = self.worker.process_pending()
        self.assertEqual(repeat, [])

        with patch("model_call.search_tool.run", return_value=fake_search_payload):
            new_worker = TicketAutomationWorker(self.repo_root, caller_factory=self.caller_factory)
            again = new_worker.process_pending()
        self.assertEqual(again, [])
