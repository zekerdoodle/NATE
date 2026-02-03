"""Tests for knowledge-base integration within the model prompt."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import List
from unittest import TestCase
from unittest.mock import patch

from model_call import NateModelCaller, NateModelConfig


class ModelPromptSearchTests(TestCase):
    @staticmethod
    def _extract_user_text(messages: List[dict]) -> str:
        for message in messages:
            if message.get("role") != "user":
                continue
            text_parts = [
                part.get("text", "")
                for part in message.get("content", [])
                if isinstance(part, dict) and part.get("type") == "input_text"
            ]
            return "\n".join(text_parts)
        return ""

    def test_prompt_includes_semantic_hits(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config = NateModelConfig(
                model="dummy-model",
                reasoning_effort="medium",
                system_instructions="Do your best",
            )
            caller = NateModelCaller(repo_root, config)

            ticket = {
                "ticket_id": 1234,
                "subject": "VPN not working",
                "description": "User reports Duo enrollment failure when connecting to VPN.",
                "requester_uid": "abc",
            }

            fake_payload = {
                "query": "VPN not working User reports Duo enrollment failure when connecting to VPN.",
                "results": [
                    {
                        "path": "docs/it_docs/How-To- Set-up DuoMobile for VPN and Unity Users.txt",
                        "score": 0.71234,
                        "start_line": 10,
                        "end_line": 34,
                        "chunk_id": "abcd1234",
                        "snippet": "Step-by-step process for Duo activation.",
                        "metadata": {
                            "source": "documents",
                            "updated_at": "2025-11-11T12:00:00Z",
                        },
                    }
                ],
                "results_low_confidence": [],
            }

            with patch("model_call.search_tool.run", return_value=fake_payload):
                messages, metadata = caller._build_messages(ticket)

        user_text = self._extract_user_text(messages)
        self.assertIn("Knowledge base search results", user_text)
        self.assertIn("How-To- Set-up DuoMobile for VPN and Unity Users.txt", user_text)
        self.assertEqual(metadata.get("knowledge_hit_count"), 1)

    def test_prompt_includes_ticket_images(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config = NateModelConfig(
                model="dummy-model",
                reasoning_effort="medium",
                system_instructions="Do your best",
            )
            caller = NateModelCaller(repo_root, config)

            ticket = {
                "ticket_id": 4321,
                "subject": "User uploaded screenshot",
                "description": "See attached screenshot for the error message.",
                "requester_uid": "abc",
                "public_updates": [
                    {
                        "type": "COMMENT",
                        "body": "Screenshot attached",
                        "images": ["data:image/png;base64,AAA111"],
                    }
                ],
                "private_updates": [],
                "system_updates": [],
            }

            fake_payload = {
                "query": "User uploaded screenshot See attached screenshot for the error message.",
                "results": [],
                "results_low_confidence": [],
            }

            with patch("model_call.search_tool.run", return_value=fake_payload):
                messages, metadata = caller._build_messages(ticket)

        self.assertEqual(metadata.get("image_count"), 1)
        user_content = messages[1]["content"]
        self.assertTrue(
            any(item.get("type") == "input_image" for item in user_content if isinstance(item, dict)),
            "Expected at least one image attachment in the user message",
        )
