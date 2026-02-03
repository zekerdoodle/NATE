"""Centralised tool schema declarations for Nate's model calls."""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, List, Sequence


_TOOL_SCHEMA_ORDER: Sequence[str] = ("search", "read_file", "update_ticket")


_TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
	"search": {
		"type": "function",
		"function": {
			"name": "search",
			"description": (
				"Search Nate's indexed knowledge sources for documents, historical tickets, or other "
				"reference material relevant to the query."
			),
			"parameters": {
				"type": "object",
				"properties": {
					"query": {
						"type": "string",
						"description": "Natural language search query text.",
					},
					"source": {
						"type": "string",
						"enum": ["all", "documents", "tickets", "non-standard"],
						"description": (
							"Optional filter limiting results to a specific corpus. Defaults to 'all'."
						),
					},
					"title": {
						"type": "string",
						"description": "Optional filename or ticket identifier to scope results.",
					},
					"limit": {
						"type": "integer",
						"minimum": 1,
						"maximum": 8,
						"description": "Maximum number of high-confidence snippets to return.",
					},
					"min_score": {
						"type": "number",
						"minimum": 0.0,
						"maximum": 1.0,
						"description": "Minimum similarity score threshold (0-1) for primary results.",
					},
					"return_content": {
						"type": "boolean",
						"description": "Include full chunk text when true (defaults to false).",
					},
				},
				"required": ["query"],
				"additionalProperties": False,
			},
		},
	},
	"read_file": {
		"type": "function",
		"function": {
			"name": "read_file",
			"description": "Read a documentation file from the repository for targeted context.",
			"parameters": {
				"type": "object",
				"properties": {
					"path": {
						"type": "string",
						"description": "Repository-relative path to the desired file (e.g., docs/it_docs/...).",
					},
					"start_line": {
						"type": "integer",
						"minimum": 1,
						"description": "Optional starting line number for a partial read.",
					},
					"end_line": {
						"type": "integer",
						"minimum": 1,
						"description": "Optional ending line number (inclusive) for a partial read.",
					},
				},
				"required": ["path"],
				"additionalProperties": False,
			},
		},
	},
	"update_ticket": {
		"type": "function",
		"function": {
			"name": "update_ticket",
			"description": "Update a NinjaOne ticket with comments, status changes, or assignments.",
			"parameters": {
				"type": "object",
				"properties": {
					"ticket_id": {
						"type": ["integer", "string"],
						"description": "Identifier of the ticket to update.",
					},
					"public_comment": {
						"type": "string",
						"description": "Comment shared with the ticket requester.",
					},
					"private_comment": {
						"type": "string",
						"description": "Internal-only note for technicians.",
					},
					"ticket_status": {
						"type": "string",
						"description": "Status label to apply (e.g., Open, Waiting, Resolved).",
					},
					"assignee": {
						"type": "string",
						"description": "Technician name or 'Unassigned'.",
					},
					"organization": {
						"type": "string",
						"enum": ["Service", "Corporate", "Division C", "Automation"],
						"description": "Organization/client to associate with the ticket. Must be one of the allowed enum values.",
					},
					"location": {
						"type": "string",
						"description": "Location linked to the organization.",
					},
					"tags": {
						"type": "array",
						"items": {"type": "string"},
						"description": "List of tags to apply to the ticket (e.g. ['0-Administrative', '0-Documentation']).",
					},
				},
				"required": ["ticket_id"],
				"additionalProperties": False,
			},
		},
	},
	"get_ticket": {
		"type": "function",
		"function": {
			"name": "get_ticket",
			"description": "Fetch full details and logs for a specific ticket from NinjaOne. Use this when you need to read a ticket that is not in your context.",
			"parameters": {
				"type": "object",
				"properties": {
					"ticket_id": {
						"type": ["integer", "string"],
						"description": "Identifier of the ticket to fetch.",
					},
				},
				"required": ["ticket_id"],
				"additionalProperties": False,
			},
		},
	},
}


def get_tool_schema(name: str) -> Dict[str, Any]:
	"""Return a deep copy of the schema for the requested tool."""

	try:
		schema = _TOOL_SCHEMAS[name]
	except KeyError as exc:
		raise KeyError(f"Unknown tool schema '{name}'") from exc
	return copy.deepcopy(schema)


def get_tool_schemas(names: Iterable[str] | None = None) -> List[Dict[str, Any]]:
	"""Return schemas for the requested tool names (or all by default)."""

	if names is None:
		names = _TOOL_SCHEMA_ORDER
	result: List[Dict[str, Any]] = []
	for name in names:
		result.append(get_tool_schema(name))
	return result


__all__ = ["get_tool_schema", "get_tool_schemas"]
