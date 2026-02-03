"""Tests for ticket_parser and ticket_listener modules using archived sample data."""

from __future__ import annotations

import json
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from typing import Any, Dict, List

from ticket_listener import TicketListener, PollResult
from ticket_parser import TicketParser


_ARCHIVE_SAMPLE = Path("archive/nate_tickets.json")


def _load_sample_ticket(index: int = 0) -> Dict[str, Any]:
    with _ARCHIVE_SAMPLE.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    return payload["tickets"][index]


class TicketPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdirs: List[TemporaryDirectory] = []
        self.addCleanup(self._cleanup_tmpdirs)

    def _temp_path(self) -> Path:
        tmpdir = TemporaryDirectory()
        self._tmpdirs.append(tmpdir)
        return Path(tmpdir.name)

    def _cleanup_tmpdirs(self) -> None:
        for tmpdir in self._tmpdirs:
            tmpdir.cleanup()

    def test_ticket_parser_parses_sample_ticket(self) -> None:
        tmp_path = self._temp_path()
        parser = TicketParser(tmp_path)
        ticket = _load_sample_ticket()

        output_path = parser.parse_and_save(ticket, board="Test Board")
        self.assertTrue(output_path.exists())

        parsed = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(parsed["ticket_id"], ticket["id"])
        self.assertEqual(parsed["subject"], ticket["subject"])
        self.assertEqual(parsed["board"], "Test Board")
        self.assertGreaterEqual(parsed["parsed_log_entry_count"], 1)
        description = parsed["description"]
        self.assertTrue(description is None or isinstance(description, str))

    def test_ticket_listener_poll_once_processes_ticket(self) -> None:
        tmp_path = self._temp_path()
        ticket = _load_sample_ticket()
        tickets_dir = tmp_path / "tickets"
        parser = TicketParser(tickets_dir)
        client = _StubClient(ticket)
        state_path = tickets_dir / ".listener_state.json"

        listener = TicketListener(client, parser, state_path, poll_interval=60 * 60 * 24 * 365 * 50, page_size=200)
        listener.state.last_polled_at = None

        result: PollResult = listener.poll_once()
        self.assertEqual(result.processed, 1)

        output_path = tickets_dir / f"{ticket['id']}.json"
        self.assertTrue(output_path.exists())
        parsed = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(parsed["board"], "QA Board")
        self.assertIn(str(ticket["id"]), listener.state.ticket_activity)

        repeat = listener.poll_once()
        self.assertEqual(repeat.processed, 0)


class _StubClient:
    """Minimal stub that mimics NinjaOneClient behaviour for unit tests."""

    def __init__(self, ticket: Dict[str, Any]):
        self._ticket = json.loads(json.dumps(ticket))
        self._boards = [{"id": 101, "name": "QA Board"}]

    def close(self) -> None:  # pragma: no cover - compatibility no-op
        return

    def get_boards(self) -> List[Dict[str, Any]]:
        return self._boards

    def run_board(self, board_id: int, *, page_size: int) -> List[Dict[str, Any]]:  # pragma: no cover - signature match
        _ = (board_id, page_size)
        last_log_time = None
        if self._ticket.get("log_entries"):
            last_entry = max(self._ticket["log_entries"], key=lambda entry: entry.get("createTime") or 0)
            last_log_time = last_entry.get("createTime")
        return [
            {
                "id": self._ticket["id"],
                "createTime": self._ticket.get("createTime"),
                "lastLogEntryCreationTime": last_log_time,
            }
        ]

    def get_ticket_with_logs(self, ticket_id: int) -> Dict[str, Any]:
        assert ticket_id == self._ticket["id"]
        return json.loads(json.dumps(self._ticket))

    def download_image(self, url: str) -> None:  # pragma: no cover - test double helper
        _ = url
        return None


if __name__ == "__main__":
    unittest.main()
