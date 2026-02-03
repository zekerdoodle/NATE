"""Utilities for converting raw NinjaOne ticket payloads into Nate-ready data."""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Pattern

logger = logging.getLogger(__name__)


_SIGNATURE_MARKERS = (
	"\nthanks,",
	"\nthank you,",
	"\nbest,",
	"\nregards,",
	"\nsincerely,",
	"\ncheers,",
	"-- \n",
	"__\n",
	"\nsent from my",
)

_SYSTEM_PLACEHOLDERS = {
	"ticket updated",
	"ticket created",
	"time added by automation",
}


def _strip_signature(body: str) -> str:
	"""Remove common email signature markers while keeping core content."""

	lowered = body.lower()
	cutoff = None
	for marker in _SIGNATURE_MARKERS:
		idx = lowered.find(marker)
		if idx != -1:
			cutoff = idx
			break
	if cutoff is not None:
		return body[:cutoff].rstrip()
	return body


def _clean_text(raw_body: Optional[str]) -> str:
	if not raw_body:
		return ""

	text = raw_body.replace("\r", "")
	if "<" in text and ">" in text:
		text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
		text = re.sub(r"</p\s*>", "\n", text, flags=re.IGNORECASE)
		text = re.sub(r"<[^>]+>", "", text)
	text = html.unescape(text)
	text = re.sub(r"\n{3,}", "\n\n", text)
	text = re.sub(r"[ \t]{2,}", " ", text)
	text = text.strip()
	if not text:
		return ""
	text = _strip_signature(text)
	return text.strip()


def _timestamp_to_iso(timestamp: Optional[float]) -> Optional[str]:
	if timestamp in (None, 0):
		return None
	try:
		dt = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
	except (OSError, ValueError, OverflowError) as exc:  # pragma: no cover - defensive guard
		logger.debug("Unable to convert timestamp %s: %s", timestamp, exc)
		return None
	return dt.isoformat()


def _latest_timestamp(entries: Iterable[Dict[str, Any]], *, default: Optional[float] = None) -> Optional[float]:
	latest: Optional[float] = default
	for entry in entries:
		try:
			value = entry.get("createTime")
		except AttributeError:  # pragma: no cover - defensive
			value = None
		if value is None:
			continue
		try:
			numeric = float(value)
		except (TypeError, ValueError):
			continue
		if latest is None or numeric > latest:
			latest = numeric
	return latest


def _simplify_status(status: Any) -> Optional[Dict[str, Any]]:
	if status is None:
		return None
	if isinstance(status, dict):
		return {
			"name": status.get("name"),
			"display_name": status.get("displayName") or status.get("display") or status.get("name"),
			"raw": status,
		}
	return {"name": status, "display_name": status, "raw": status}


def _flatten_attribute_values(attribute_values: Any) -> Dict[str, Any]:
	flattened: Dict[str, Any] = {}
	if not isinstance(attribute_values, list):
		return flattened
	for entry in attribute_values:
		if not isinstance(entry, dict):
			continue
		attribute_id = entry.get("attributeId")
		value = entry.get("value")
		if attribute_id is None:
			continue
		flattened[str(attribute_id)] = value
	return flattened


def _extract_assigned_name(ticket: Dict[str, Any]) -> Optional[str]:
	assigned = ticket.get("assignedAppUser")
	if isinstance(assigned, dict):
		return assigned.get("name") or assigned.get("displayName")
	if isinstance(assigned, str):
		return assigned
	return None


def _derive_author(entry: Dict[str, Any]) -> Dict[str, Any]:
	if entry.get("system"):
		return {"type": "SYSTEM"}
	return {
		"type": entry.get("appUserContactType"),
		"id": entry.get("appUserContactId"),
		"uid": entry.get("appUserContactUid"),
	}


@dataclass
class ParsedLogEntry:
	log_entry_id: Optional[int]
	created_at: Optional[str]
	type: Optional[str]
	public: bool
	body: str
	is_system: bool
	author: Dict[str, Any]
	metadata: Dict[str, Any]
	images: List[str]

	def to_dict(self) -> Dict[str, Any]:
		return {
			"log_entry_id": self.log_entry_id,
			"created_at": self.created_at,
			"type": self.type,
			"public": self.public,
			"body": self.body,
			"is_system": self.is_system,
			"author": self.author,
			"metadata": self.metadata,
			"images": self.images,
		}


class TicketParser:
	"""Parse and persist NinjaOne ticket payloads into Nate's ticket store."""

	def __init__(self, output_dir: Path | str, *, ensure_ascii: bool = False) -> None:
		self.output_dir = Path(output_dir)
		self.output_dir.mkdir(parents=True, exist_ok=True)
		self.ensure_ascii = ensure_ascii

	def parse_and_save(
		self,
		ticket: Dict[str, Any],
		*,
		board: Optional[str] = None,
		image_downloader: Optional[Callable[[str], Optional[str]]] = None,
	) -> Path:
		parsed = self.parse_ticket(ticket, board=board, image_downloader=image_downloader)
		filepath = self.output_dir / f"{parsed['ticket_id']}.json"
		with filepath.open("w", encoding="utf-8") as handle:
			json.dump(parsed, handle, indent=2, ensure_ascii=self.ensure_ascii)
		return filepath

	def parse_ticket(
		self,
		ticket: Dict[str, Any],
		*,
		board: Optional[str] = None,
		image_downloader: Optional[Callable[[str], Optional[str]]] = None,
	) -> Dict[str, Any]:
		raw_logs = ticket.get("log_entries") or []
		cleaned_logs = self._parse_logs(raw_logs, image_downloader=image_downloader)

		created_at = _timestamp_to_iso(ticket.get("createTime"))
		latest_activity_iso = cleaned_logs[-1].get("created_at") if cleaned_logs else created_at

		description = self._select_description(cleaned_logs)
		public_updates = [entry.copy() for entry in cleaned_logs if entry.get("public") and entry.get("type") != "DESCRIPTION"]
		private_updates = [entry.copy() for entry in cleaned_logs if not entry.get("public") and entry.get("type") != "DESCRIPTION"]
		system_updates = [entry.copy() for entry in cleaned_logs if entry.get("is_system")]

		parsed_ticket: Dict[str, Any] = {
			"ticket_id": ticket.get("id"),
			"subject": ticket.get("subject"),
			"status": _simplify_status(ticket.get("status")),
			"type": ticket.get("type"),
			"priority": ticket.get("priority"),
			"severity": ticket.get("severity"),
			"source": ticket.get("source"),
			"client_id": ticket.get("clientId"),
			"location_id": ticket.get("locationId"),
			"assigned_technician_id": ticket.get("assignedAppUserId"),
			"assigned_technician_name": _extract_assigned_name(ticket),
			"requester_uid": ticket.get("requesterUid"),
			"tags": ticket.get("tags", []),
			"cc_list": ticket.get("ccList", {}),
			"attribute_values": _flatten_attribute_values(ticket.get("attributeValues")),
			"board": board,
			"created_at": created_at,
			"last_activity_at": latest_activity_iso,
			"description": description,
			"public_updates": public_updates,
			"private_updates": private_updates,
			"system_updates": system_updates,
			"raw_log_entry_count": len(raw_logs),
			"parsed_log_entry_count": len(cleaned_logs),
		}

		return parsed_ticket

	def get_latest_activity_timestamp(self, ticket: Dict[str, Any]) -> Optional[float]:
		raw_logs = ticket.get("log_entries") or []
		latest = _latest_timestamp(raw_logs, default=ticket.get("createTime"))
		return latest

	def _parse_logs(
		self,
		log_entries: Iterable[Dict[str, Any]],
		*,
		image_downloader: Optional[Callable[[str], Optional[str]]] = None,
	) -> List[Dict[str, Any]]:
		ordered_entries = sorted(
			(entry for entry in log_entries if isinstance(entry, dict)),
			key=lambda item: item.get("createTime") or 0,
		)

		parsed: List[Dict[str, Any]] = []
		for entry in ordered_entries:
			raw_body = entry.get("htmlBody") or entry.get("body") or ""
			images: List[str] = []
			if image_downloader and raw_body:
				images = self._extract_images(raw_body, image_downloader)

			body_text = _clean_text(raw_body)
			if not body_text and not entry.get("timeTracked"):
				continue

			if entry.get("system"):
				placeholder = (body_text or "").lower()
				if any(term in placeholder for term in _SYSTEM_PLACEHOLDERS):
					continue

			parsed_entry = ParsedLogEntry(
				log_entry_id=entry.get("id"),
				created_at=_timestamp_to_iso(entry.get("createTime")),
				type=entry.get("type"),
				public=bool(entry.get("publicEntry", True)),
				body=body_text,
				is_system=bool(entry.get("system")),
				author=_derive_author(entry),
				metadata=self._extract_metadata(entry),
				images=images,
			)
			parsed.append(parsed_entry.to_dict())

		return parsed

	def _select_description(self, log_entries: List[Dict[str, Any]]) -> Optional[str]:
		descriptions = [entry for entry in log_entries if entry.get("type") == "DESCRIPTION" and entry.get("body")]
		if descriptions:
			return descriptions[-1]["body"]
		return None

	def _extract_metadata(self, entry: Dict[str, Any]) -> Dict[str, Any]:
		metadata_fields = {
			"changeDiff",
			"timeTracked",
			"ticketTimeEntry",
			"technicianTagged",
			"techniciansTaggedMetadata",
			"automation",
		}
		metadata: Dict[str, Any] = {}
		for field in metadata_fields:
			if field in entry:
				metadata[field] = entry[field]
		return metadata

	def _extract_images(
		self,
		html_content: str,
		downloader: Callable[[str], Optional[str]],
	) -> List[str]:
		"""Extract img src URLs and download them as base64 strings."""
		images: List[str] = []
		# Simple regex to find src attributes in img tags
		# Handles src="url" and src='url'
		matches = re.finditer(r'<img[^>]+src=["\']([^"\']+)["\']', html_content, re.IGNORECASE)
		
		for match in matches:
			url = match.group(1)
			# Skip empty or data URIs that are already present (though we could validate them)
			if not url or url.startswith("data:"):
				if url and url.startswith("data:"):
					images.append(url)
				continue
				
			try:
				base64_image = downloader(url)
				if base64_image:
					images.append(base64_image)
			except Exception as exc:
				logger.warning("Failed to download image from %s: %s", url, exc)
				
		return images


__all__ = ["TicketParser"]
