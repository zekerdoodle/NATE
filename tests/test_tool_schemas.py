"""Tests for shared tool schema definitions and consumers."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase

from model_call import DEFAULT_TOOL_EXECUTORS, NateModelCaller, NateModelConfig
from tools.tool_schemas import get_tool_schemas


class ToolSchemaIntegrationTests(TestCase):
    def test_schema_names_match_executors(self) -> None:
        schemas = get_tool_schemas()
        schema_names = {schema["function"]["name"] for schema in schemas}
        self.assertEqual(schema_names, set(DEFAULT_TOOL_EXECUTORS.keys()))
        self.assertNotIn("native_web_search", schema_names)

    def test_update_ticket_requires_ticket_id(self) -> None:
        schemas_by_name = {schema["function"]["name"]: schema for schema in get_tool_schemas()}
        update_params = schemas_by_name["update_ticket"]["function"]["parameters"]
        self.assertIn("ticket_id", update_params["required"])

    def test_search_schema_exposes_return_content_flag(self) -> None:
        schemas_by_name = {schema["function"]["name"]: schema for schema in get_tool_schemas()}
        search_properties = schemas_by_name["search"]["function"]["parameters"]["properties"]
        self.assertIn("return_content", search_properties)

    def test_model_caller_uses_shared_schema_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            config = NateModelConfig(
                model="dummy-model",
                reasoning_effort="medium",
                system_instructions="Do great work",
            )
            caller = NateModelCaller(repo_root, config)
            self.assertEqual(caller._load_tool_schemas(), get_tool_schemas())
