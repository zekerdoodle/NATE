"""Update NinjaOne tickets with assignments, statuses, and comments."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

from .exceptions import ToolExecutionError


LOGGER = logging.getLogger("tools.update_ticket")
CONFIG_PATH = Path("config/nate_model_config.json")
DEFAULT_TIMEOUT = 30.0
AUTH_SCOPE = "monitoring management"
TOKEN_CACHE_FILENAME = ".ninjaone_token.json"


def _utcnow() -> datetime:
	return datetime.now(timezone.utc)


def _normalise_text(value: Any) -> Optional[str]:
	if value is None:
		return None
	if not isinstance(value, str):
		value = str(value)
	text = value.strip()
	return text or None


def _is_truthy(value: Any) -> bool:
	if isinstance(value, str):
		return value.strip().lower() in {"1", "true", "yes", "on"}
	return bool(value)


def _load_private_mode(repo_root: Path) -> bool:
	config_path = repo_root / CONFIG_PATH
	if not config_path.exists():
		return False
	try:
		with config_path.open("r", encoding="utf-8") as handle:
			payload = json.load(handle)
	except (OSError, json.JSONDecodeError) as exc:
		LOGGER.warning("Unable to read config for private mode: %s", exc)
		return False
	return _is_truthy(payload.get("private_mode", False))


def _load_token_cache(repo_root: Path) -> Dict[str, Any]:
	cache_path = repo_root / TOKEN_CACHE_FILENAME
	if not cache_path.exists():
		return {}
	try:
		with cache_path.open("r", encoding="utf-8") as handle:
			return json.load(handle)
	except (OSError, json.JSONDecodeError):
		return {}


def _save_token_cache(repo_root: Path, payload: Dict[str, Any]) -> None:
	cache_path = repo_root / TOKEN_CACHE_FILENAME
	cache_path.parent.mkdir(parents=True, exist_ok=True)
	tmp = cache_path.with_suffix(".tmp")
	with tmp.open("w", encoding="utf-8") as handle:
		json.dump(payload, handle, indent=2)
	tmp.replace(cache_path)


class NinjaOneAuthenticationError(ToolExecutionError):
	"""Raised when NinjaOne authentication fails."""


class NinjaOneAPI:
	"""Minimal NinjaOne REST client for ticket updates."""

	def __init__(
		self,
		*,
		base_url: str,
		client_id: str,
		client_secret: str,
		repo_root: Optional[Path] = None,
		access_token: Optional[str] = None,
		refresh_token: Optional[str] = None,
		token_expiry: Optional[datetime] = None,
		token_cache_path: Optional[Path] = None,
		auth_code: Optional[str] = None,
		session: Optional[requests.Session] = None,
		timeout: float = DEFAULT_TIMEOUT,
	) -> None:
		self.base_url = base_url.rstrip("/")
		self.client_id = client_id
		self.client_secret = client_secret
		self.token = access_token
		self.refresh_token = refresh_token
		self.session = session or requests.Session()
		self.timeout = timeout
		self.token_expiry: Optional[datetime] = token_expiry
		self.token_cache_path = token_cache_path
		self.repo_root = repo_root
		self.auth_code = auth_code
		self._persist_token()
		self._status_cache: Optional[List[Dict[str, Any]]] = None
		self._org_cache: Optional[List[Dict[str, Any]]] = None
		self._app_user_cache: Optional[List[Dict[str, Any]]] = None
		self._location_cache: Dict[int, List[Dict[str, Any]]] = {}

	def close(self) -> None:
		self.session.close()

	def _persist_token(self) -> None:
		if not self.token_cache_path or not self.token:
			return
		payload: Dict[str, Any] = {"access_token": self.token}
		if self.refresh_token:
			payload["refresh_token"] = self.refresh_token
		if self.token_expiry:
			payload["expires_at"] = self.token_expiry.isoformat()
		try:
			_save_token_cache(self.token_cache_path.parent, payload)
		except Exception:
			LOGGER.debug("Unable to persist token cache", exc_info=True)

	def _authenticate(self) -> None:
		url = f"{self.base_url}/ws/oauth/token"

		headers = {"Content-Type": "application/x-www-form-urlencoded"}

		# Try refresh token first if available.
		if self.refresh_token:
			payload = {
				"grant_type": "refresh_token",
				"refresh_token": self.refresh_token,
				"client_id": self.client_id,
				"client_secret": self.client_secret,
				"redirect_uri": "http://localhost:8080",
			}
			response = self.session.post(url, data=payload, headers=headers, timeout=self.timeout)
			if response.status_code == 200:
				self._process_token_response(response.json())
				return
			LOGGER.warning("Refresh token failed (%s); trying authorization_code if present", response.status_code)

		# Next try user auth code if provided.
		if self.auth_code:
			payload = {
				"grant_type": "authorization_code",
				"code": self.auth_code,
				"client_id": self.client_id,
				"client_secret": self.client_secret,
				"redirect_uri": "http://localhost:8080",
			}
			response = self.session.post(url, data=payload, headers=headers, timeout=self.timeout)
			if response.status_code == 200:
				self._process_token_response(response.json())
				# Clear auth code to avoid reuse.
				self.auth_code = None
				return
			LOGGER.warning("Auth code exchange failed (%s); falling back to client_credentials", response.status_code)

		payload = {
			"grant_type": "client_credentials",
			"client_id": self.client_id,
			"client_secret": self.client_secret,
			"redirect_uri": "http://localhost:8080",
			"scope": AUTH_SCOPE,
		}
		response = self.session.post(url, data=payload, headers=headers, timeout=self.timeout)
		if response.status_code != 200:
			raise NinjaOneAuthenticationError(
				f"Authentication failed with status {response.status_code}: {response.text[:200]}"
			)
		self._process_token_response(response.json())

	def _process_token_response(self, body: Dict[str, Any]) -> None:
		token = body.get("access_token")
		if not token:
			raise NinjaOneAuthenticationError("Authentication response missing access_token")
		self.token = token
		new_refresh = body.get("refresh_token")
		if new_refresh:
			self.refresh_token = new_refresh
		expires_in = body.get("expires_in")
		buffer_seconds = 60
		if isinstance(expires_in, (int, float)) and expires_in > buffer_seconds:
			self.token_expiry = _utcnow() + timedelta(seconds=float(expires_in) - buffer_seconds)
		else:
			self.token_expiry = _utcnow() + timedelta(minutes=10)
		self._persist_token()

	def _ensure_token(self) -> None:
		if not self.token or not self.token_expiry or _utcnow() >= self.token_expiry:
			self._authenticate()

	def _headers(self, *, content_type: Optional[str] = "application/json") -> Dict[str, str]:
		self._ensure_token()
		assert self.token  # guaranteed by _ensure_token
		headers = {"Authorization": f"Bearer {self.token}"}
		if content_type:
			headers["Content-Type"] = content_type
		return headers

	def _get(self, path: str) -> Any:
		url = f"{self.base_url}{path}"
		response = self.session.get(url, headers=self._headers(), timeout=self.timeout)
		if response.status_code == 401:
			LOGGER.warning("Token expired (401). Refreshing...")
			self._authenticate()
			response = self.session.get(url, headers=self._headers(), timeout=self.timeout)
		response.raise_for_status()
		return response.json()

	def _put(self, path: str, payload: Dict[str, Any]) -> Any:
		url = f"{self.base_url}{path}"
		response = self.session.put(url, headers=self._headers(), json=payload, timeout=self.timeout)
		if response.status_code == 401:
			LOGGER.warning("Token expired (401). Refreshing...")
			self._authenticate()
			response = self.session.put(url, headers=self._headers(), json=payload, timeout=self.timeout)
		
		if not response.ok:
			raise ToolExecutionError(f"NinjaOne PUT failed ({response.status_code}): {response.text}")
			
		if response.content:
			return response.json()
		return {}

	def _post_multipart(self, path: str, fields: Dict[str, Tuple[Optional[str], str, str]]) -> Any:
		url = f"{self.base_url}{path}"
		headers = self._headers(content_type=None)
		response = self.session.post(url, headers=headers, files=fields, timeout=self.timeout)
		if response.status_code == 401:
			LOGGER.warning("Token expired (401). Refreshing...")
			self._authenticate()
			headers = self._headers(content_type=None)
			response = self.session.post(url, headers=headers, files=fields, timeout=self.timeout)
		response.raise_for_status()
		if response.content:
			return response.json()
		return {}

	def get_ticket(self, ticket_id: int) -> Dict[str, Any]:
		payload = self._get(f"/v2/ticketing/ticket/{ticket_id}")
		if not isinstance(payload, dict):
			raise ToolExecutionError("Unexpected ticket payload from NinjaOne")
		return payload

	def update_ticket(self, ticket_id: int, payload: Dict[str, Any]) -> Any:
		return self._put(f"/v2/ticketing/ticket/{ticket_id}", payload)

	def add_comment(self, ticket_id: int, *, body: str, public: bool) -> Any:
		# NinjaOne API v2 expects multipart/form-data.
		# Providing a filename (e.g. 'comment.json') ensures it's treated correctly by the parser.
		# We also populate htmlBody as a fallback.
		html_body = body.replace("\n", "<br/>")
		comment_payload = {"public": public, "body": body, "htmlBody": html_body}
		fields = {"comment": ("comment.json", json.dumps(comment_payload), "application/json")}
		return self._post_multipart(f"/v2/ticketing/ticket/{ticket_id}/comment", fields)

	def list_statuses(self) -> List[Dict[str, Any]]:
		if self._status_cache is None:
			data = self._get("/v2/ticketing/statuses")
			if not isinstance(data, list):
				raise ToolExecutionError("Unexpected status list from NinjaOne")
			self._status_cache = data
		return self._status_cache

	def list_organizations(self) -> List[Dict[str, Any]]:
		if self._org_cache is None:
			data = self._get("/v2/organizations")
			if not isinstance(data, list):
				raise ToolExecutionError("Unexpected organizations payload from NinjaOne")
			self._org_cache = data
		return self._org_cache

	def list_locations(self, organization_id: int) -> List[Dict[str, Any]]:
		if organization_id not in self._location_cache:
			data = self._get(f"/v2/organization/{organization_id}/locations")
			if not isinstance(data, list):
				raise ToolExecutionError("Unexpected locations payload from NinjaOne")
			self._location_cache[organization_id] = data
		return self._location_cache[organization_id]

	def list_app_users(self) -> List[Dict[str, Any]]:
		if self._app_user_cache is None:
			data = self._get("/v2/ticketing/app-user-contact")
			if not isinstance(data, list):
				raise ToolExecutionError("Unexpected technician list from NinjaOne")
			self._app_user_cache = data
		return self._app_user_cache

	def resolve_status_id(self, label: str) -> Tuple[str, str]:
		target = label.strip().lower()
		for status in self.list_statuses():
			display = _normalise_text(status.get("displayName"))
			name = _normalise_text(status.get("name"))
			if display and display.lower() == target:
				status_id = status.get("statusId")
				if status_id is None:
					raise ToolExecutionError(f"Status '{label}' is missing statusId in NinjaOne data")
				return str(status_id), display
			if name and name.lower() == target:
				status_id = status.get("statusId")
				if status_id is None:
					raise ToolExecutionError(f"Status '{label}' is missing statusId in NinjaOne data")
				return str(status_id), display or name
		raise ToolExecutionError(f"Unknown ticket status '{label}'")

	def resolve_organization(self, label: str) -> Tuple[int, str]:
		target = label.strip().lower()
		candidates: List[Tuple[int, str]] = []
		for org in self.list_organizations():
			name = _normalise_text(org.get("name"))
			if name and name.lower() == target:
				org_id = org.get("id")
				if org_id is None:
					continue
				candidates.append((int(org_id), name))
		if not candidates:
			raise ToolExecutionError(f"Organization '{label}' not found in NinjaOne")
		if len(candidates) > 1:
			raise ToolExecutionError(f"Organization name '{label}' is ambiguous; refine the request")
		return candidates[0]

	def organization_name(self, organization_id: int) -> Optional[str]:
		for org in self.list_organizations():
			if org.get("id") == organization_id:
				name = _normalise_text(org.get("name"))
				return name or str(organization_id)
		return None

	def resolve_location(self, label: str, *, organization_id: Optional[int]) -> Tuple[int, int, str]:
		target = label.strip().lower()
		if organization_id is not None:
			org_ids_raw = [organization_id]
		else:
			org_ids_raw = [org.get("id") for org in self.list_organizations()]
		org_ids: List[int] = []
		for value in org_ids_raw:
			if value is None:
				continue
			try:
				org_ids.append(int(value))
			except (TypeError, ValueError):
				continue
		matches: List[Tuple[int, int, str]] = []
		for org_id in org_ids:
			for location in self.list_locations(org_id):
				name = _normalise_text(location.get("name"))
				if name and name.lower() == target:
					loc_id = location.get("id")
					if loc_id is None:
						continue
					matches.append((org_id, int(loc_id), name))
		if not matches:
			raise ToolExecutionError(f"Location '{label}' not found in NinjaOne")
		if len(matches) > 1:
			raise ToolExecutionError(f"Location name '{label}' is ambiguous; specify the organization")
		return matches[0]

	def resolve_assignee(self, label: str) -> Tuple[Optional[int], Optional[str]]:
		target = label.strip().lower()
		if target in {"unassigned", "none", ""}:
			return None, None
		for contact in self.list_app_users():
			user_type = _normalise_text(contact.get("userType"))
			if user_type and user_type.lower() != "technician":
				continue
			first = _normalise_text(contact.get("firstName"))
			last = _normalise_text(contact.get("lastName"))
			full_name_parts = [part for part in (first, last) if part]
			full_name = " ".join(full_name_parts)
			email = _normalise_text(contact.get("email"))
			natural = _normalise_text(contact.get("naturalId"))
			candidates = [candidate for candidate in (full_name, email, natural) if candidate]
			if first and last:
				candidates.append(f"{last}, {first}")
			display = _normalise_text(contact.get("displayName"))
			if display:
				candidates.append(display)
			for candidate in candidates:
				if candidate.lower() == target:
					contact_id = contact.get("id")
					if contact_id is None:
						break
					return int(contact_id), full_name or email or natural
		raise ToolExecutionError(f"Technician '{label}' not found in NinjaOne")


@dataclass
class ToolParameters:
	ticket_id: int
	public_comment: Optional[str]
	private_comment: Optional[str]
	ticket_status: Optional[str]
	assignee: Optional[str]
	organization: Optional[str]
	location: Optional[str]
	tags: Optional[List[str]]


def _extract_ticket_id(parameters: Dict[str, Any]) -> int:
	for key in ("ticket_id", "ticketId", "id"):
		value = parameters.get(key)
		if value is None:
			continue
		try:
			numeric = int(str(value).strip())
		except (TypeError, ValueError) as exc:
			raise ToolExecutionError("`ticket_id` must be an integer") from exc
		return numeric
	raise ToolExecutionError("`ticket_id` must be provided to update a ticket")


def _parse_parameters(parameters: Dict[str, Any], *, private_mode: bool) -> ToolParameters:
	ticket_id = _extract_ticket_id(parameters)
	public_comment = _normalise_text(parameters.get("public_comment"))
	private_comment = _normalise_text(parameters.get("private_comment"))
	ticket_status = _normalise_text(parameters.get("ticket_status"))
	assignee = _normalise_text(parameters.get("assignee"))
	organization = _normalise_text(parameters.get("organization"))
	location = _normalise_text(parameters.get("location"))
	tags = parameters.get("tags")
	if tags is not None and not isinstance(tags, list):
		# Fallback if model sends a string
		if isinstance(tags, str):
			tags = [t.strip() for t in tags.split(",") if t.strip()]
		else:
			tags = None

	if private_mode:
		if ticket_status and ticket_status.lower() == "resolved":
			ticket_status = "Open"

	return ToolParameters(
		ticket_id=ticket_id,
		public_comment=public_comment,
		private_comment=private_comment,
		ticket_status=ticket_status,
		assignee=assignee,
		organization=organization,
		location=location,
		tags=tags,
	)


def _extract_status_id(ticket: Dict[str, Any], api: NinjaOneAPI) -> Tuple[str, Optional[str]]:
	status = ticket.get("status")
	if isinstance(status, dict):
		status_id = status.get("statusId")
		display = _normalise_text(status.get("displayName") or status.get("name"))
		if status_id is not None:
			return str(status_id), display
		display_name = status.get("displayName") or status.get("name")
		if display_name:
			return api.resolve_status_id(str(display_name))
	elif status is not None:
		return str(status), None
	raise ToolExecutionError("Ticket status is missing from NinjaOne payload")


def _convert_attributes(ticket: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
	values = ticket.get("attributeValues")
	if not isinstance(values, list):
		return None
	result: List[Dict[str, Any]] = []
	for entry in values:
		if not isinstance(entry, dict):
			continue
		attribute_id = entry.get("attributeId")
		if attribute_id is None:
			continue
		
		val = entry.get("value")
		# API rejects null values for attributes, so we skip them.
		if val is None:
			continue
			
		payload = {"attributeId": int(attribute_id), "value": val}
		if entry.get("id") is not None:
			payload["id"] = entry.get("id")
		result.append(payload)
	return result or None


def _convert_cc_list(ticket: Dict[str, Any]) -> Optional[Dict[str, Any]]:
	cc = ticket.get("ccList")
	if not isinstance(cc, dict):
		return None
	result: Dict[str, Any] = {}
	if isinstance(cc.get("uids"), list):
		result["uids"] = cc["uids"]
	if isinstance(cc.get("emails"), list):
		result["emails"] = cc["emails"]
	return result or None


def _prepare_update_payload(
	api: NinjaOneAPI,
	ticket: Dict[str, Any],
	params: ToolParameters,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
	status_id, status_label = _extract_status_id(ticket, api)
	client_id = ticket.get("clientId")
	ticket_form_id = ticket.get("ticketFormId")
	requester_uid = ticket.get("requesterUid")
	subject = ticket.get("subject")
	version = ticket.get("version")
	if None in (client_id, ticket_form_id, requester_uid, subject, version):
		raise ToolExecutionError("Ticket payload is missing required fields for update")

	payload: Dict[str, Any] = {
		"clientId": int(client_id),
		"ticketFormId": int(ticket_form_id),
		"requesterUid": requester_uid,
		"subject": str(subject),
		"status": status_id,
		"version": int(version),
	}

	if ticket.get("locationId") is not None:
		payload["locationId"] = ticket.get("locationId")
	if ticket.get("nodeId") is not None:
		payload["nodeId"] = ticket.get("nodeId")
	if ticket.get("type") is not None:
		payload["type"] = ticket.get("type")
	if ticket.get("severity") is not None:
		payload["severity"] = ticket.get("severity")
	if ticket.get("priority") is not None:
		payload["priority"] = ticket.get("priority")
	if ticket.get("parentTicketId") is not None:
		payload["parentTicketId"] = ticket.get("parentTicketId")
	if ticket.get("tags") is not None:
		payload["tags"] = ticket.get("tags")
	attributes = _convert_attributes(ticket)
	if attributes is not None:
		payload["attributes"] = attributes
	cc = _convert_cc_list(ticket)
	if cc is not None:
		payload["cc"] = cc
	if ticket.get("assignedAppUserId") is not None:
		payload["assignedAppUserId"] = ticket.get("assignedAppUserId")
	if ticket.get("additionalAssignedTechnicianIds") is not None:
		payload["additionalAssignedTechnicianIds"] = ticket.get("additionalAssignedTechnicianIds")

	changes: Dict[str, Any] = {}

	if params.ticket_status:
		resolved_status_id, resolved_label = api.resolve_status_id(params.ticket_status)
		payload["status"] = resolved_status_id
		changes["ticket_status"] = resolved_label or params.ticket_status

	if params.organization:
		resolved_org_id, resolved_org_name = api.resolve_organization(params.organization)
		payload["clientId"] = resolved_org_id
		changes["organization"] = resolved_org_name

	if params.location:
		org_hint = payload.get("clientId") if params.organization else None
		org_id_hint = None
		if org_hint is not None:
			try:
				org_id_hint = int(org_hint)
			except (TypeError, ValueError):
				org_id_hint = None
		org_id, location_id, location_name = api.resolve_location(params.location, organization_id=org_id_hint)
		payload["clientId"] = int(org_id)
		payload["locationId"] = int(location_id)
		if "organization" not in changes:
			resolved_name = api.organization_name(int(org_id))
			if resolved_name:
				changes["organization"] = resolved_name
		changes["location"] = location_name

	if params.assignee:
		assignee_id, assignee_label = api.resolve_assignee(params.assignee)
		if os.getenv("NATE_TEST_MODE"):
			LOGGER.info("Test mode enabled: Ignoring reassignment to %s", params.assignee)
		else:
			payload["assignedAppUserId"] = assignee_id
		changes["assignee"] = assignee_label or "Unassigned"

	if params.tags is not None:
		payload["tags"] = params.tags
		changes["tags"] = params.tags

	if "assignee" in changes and changes["assignee"] is None:
		changes["assignee"] = "Unassigned"

	return payload, {key: value for key, value in changes.items() if value is not None}


def _summarise_changes(ticket: Dict[str, Any], payload: Dict[str, Any]) -> List[str]:
	fields: List[str] = []

	def _normalise_numeric(value: Any) -> Any:
		if value is None:
			return None
		if isinstance(value, bool):
			return value
		try:
			return int(value)
		except (TypeError, ValueError):
			return value

	status_current = None
	status_value = ticket.get("status")
	if isinstance(status_value, dict) and status_value.get("statusId") is not None:
		status_current = str(status_value.get("statusId"))
	elif status_value is not None:
		status_current = str(status_value)

	if payload.get("status") != status_current:
		fields.append("ticket_status")

	if _normalise_numeric(ticket.get("clientId")) != _normalise_numeric(payload.get("clientId")):
		fields.append("organization")
	if _normalise_numeric(ticket.get("locationId")) != _normalise_numeric(payload.get("locationId")):
		fields.append("location")
	if _normalise_numeric(ticket.get("assignedAppUserId")) != _normalise_numeric(payload.get("assignedAppUserId")):
		fields.append("assignee")

	if "tags" in payload:
		current_tags = set(ticket.get("tags") or [])
		new_tags = set(payload["tags"] or [])
		if current_tags != new_tags:
			fields.append("tags")

	return fields


def run(parameters: Dict[str, Any], *, repo_root: Path) -> Dict[str, Any]:
	private_mode = _load_private_mode(repo_root)
	params = _parse_parameters(parameters, private_mode=private_mode)

	load_dotenv(repo_root / "api_keys.env")
	base_url = os.getenv("NinjaOne_BaseURL", "https://app.ninjarmm.com")
	client_id = os.getenv("NinjaOne_ClientID")
	client_secret = os.getenv("NinjaOne_ClientSecret")
	if not client_id or not client_secret:
		raise ToolExecutionError("NinjaOne credentials are missing from api_keys.env or environment variables")

	token_cache = _load_token_cache(repo_root)
	env_access = os.getenv("NinjaOne_AccessToken")
	env_refresh = os.getenv("NINJA_REFRESH_TOKEN") or os.getenv("NinjaOne_RefreshToken")
	env_auth_code = os.getenv("NinjaOne_AuthCode")
	
	# Prefer cached tokens as they are likely more recent (due to rotation)
	access_token = token_cache.get("access_token") or env_access
	refresh_token = token_cache.get("refresh_token") or env_refresh
	
	expires_at_raw = token_cache.get("expires_at")
	token_expiry = None
	if isinstance(expires_at_raw, str):
		try:
			token_expiry = datetime.fromisoformat(expires_at_raw)
		except ValueError:
			token_expiry = None

	api = NinjaOneAPI(
		base_url=base_url,
		client_id=client_id,
		client_secret=client_secret,
		repo_root=repo_root,
		access_token=access_token,
		refresh_token=refresh_token,
		token_expiry=token_expiry,
		token_cache_path=repo_root / TOKEN_CACHE_FILENAME,
		auth_code=env_auth_code,
	)
	try:
		ticket = api.get_ticket(params.ticket_id)
		existing_status_id, _ = _extract_status_id(ticket, api)
		payload, declared_changes = _prepare_update_payload(api, ticket, params)
		delta_fields = _summarise_changes(ticket, payload)
		did_update = False
		if delta_fields:
			try:
				api.update_ticket(params.ticket_id, payload)
			except requests.RequestException as exc:
				raise ToolExecutionError(f"Failed to update ticket {params.ticket_id}: {exc}") from exc
			did_update = True

		comment_count = 0
		comment_errors: List[str] = []
		for body, intended_public in (
			(params.private_comment, False),
			(params.public_comment, True),
		):
			if not body:
				continue
			
			is_public = intended_public and not private_mode
			
			try:
				api.add_comment(params.ticket_id, body=body, public=is_public)
			except requests.RequestException as exc:
				comment_errors.append(str(exc))
				continue
			comment_count += 1

		if comment_errors:
			raise ToolExecutionError("; ".join(f"Failed to add comment: {err}" for err in comment_errors))

		result_changes = {field: declared_changes[field] for field in delta_fields if field in declared_changes}
		
		# In test mode, we want to report the assignee change as successful even if we didn't apply it
		if os.getenv("NATE_TEST_MODE") and "assignee" in declared_changes:
			result_changes["assignee"] = declared_changes["assignee"]

		if "ticket_status" not in result_changes and payload.get("status") != existing_status_id:
			new_status_id = str(payload.get("status"))
			resolved_label = None
			for status in api.list_statuses():
				status_id = status.get("statusId")
				if status_id is None:
					continue
				if str(status_id) == new_status_id:
					resolved_label = _normalise_text(status.get("displayName") or status.get("name"))
					break
			result_changes["ticket_status"] = resolved_label or params.ticket_status or new_status_id
		return {
			"ticket_id": params.ticket_id,
			"updated": did_update,
			"changed_fields": delta_fields,
			"applied_changes": result_changes,
			"comments_added": comment_count,
		}
	except requests.RequestException as exc:
		raise ToolExecutionError(f"NinjaOne request failed: {exc}") from exc
	finally:
		api.close()


__all__ = ["run"]
