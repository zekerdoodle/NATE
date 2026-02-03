"""Poll NinjaOne for recent tickets and persist Nate-ready payloads."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

import requests
from dotenv import load_dotenv, set_key

from ticket_parser import TicketParser


logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 60
DEFAULT_PAGE_SIZE = 200


class AuthenticationError(RuntimeError):
	"""Raised when the NinjaOne API cannot authenticate."""


def _utcnow() -> datetime:
	return datetime.now(timezone.utc)


def _max_ignore_none(values: Iterable[Optional[float]]) -> Optional[float]:
	numeric_values = [value for value in values if value is not None]
	if not numeric_values:
		return None
	return max(numeric_values)


class NinjaOneClient:
	"""Thin wrapper around the NinjaOne REST API."""

	def __init__(
		self,
		*,
		base_url: str,
		client_id: str,
		client_secret: str,
		refresh_token: Optional[str] = None,
		session: Optional[requests.Session] = None,
		timeout: float = 30.0,
		on_token_refresh: Optional[Callable[[str], None]] = None,
	) -> None:
		self.base_url = base_url.rstrip("/")
		self.client_id = client_id
		self.client_secret = client_secret
		self.refresh_token = refresh_token
		self.session = session or requests.Session()
		self.timeout = timeout
		self.on_token_refresh = on_token_refresh
		self.token: Optional[str] = None
		self.token_expiry: Optional[datetime] = None

	def close(self) -> None:
		self.session.close()

	def authenticate(self) -> None:
		url = f"{self.base_url}/ws/oauth/token"
		
		if self.refresh_token:
			payload = {
				"grant_type": "refresh_token",
				"client_id": self.client_id,
				"client_secret": self.client_secret,
				"refresh_token": self.refresh_token,
			}
		else:
			payload = {
				"grant_type": "client_credentials",
				"client_id": self.client_id,
				"client_secret": self.client_secret,
				"redirect_uri": "http://localhost",
				"scope": "monitoring management ticketing",
			}
		headers = {"Content-Type": "application/x-www-form-urlencoded"}

		response = self.session.post(url, data=payload, headers=headers, timeout=self.timeout)
		if response.status_code != 200:
			raise AuthenticationError(f"Authentication failed with status {response.status_code}: {response.text}")

		token_data = response.json()
		token = token_data.get("access_token")
		if not token:
			raise AuthenticationError("Authentication response missing access token")
		self.token = token
		
		new_refresh_token = token_data.get("refresh_token")
		if new_refresh_token:
			self.refresh_token = new_refresh_token
			if self.on_token_refresh:
				try:
					self.on_token_refresh(new_refresh_token)
				except Exception as exc:
					logger.warning("Failed to execute token refresh callback: %s", exc)

		expires_in = token_data.get("expires_in")
		buffer_seconds = 60
		if isinstance(expires_in, (int, float)) and expires_in > buffer_seconds:
			self.token_expiry = _utcnow() + timedelta(seconds=float(expires_in) - buffer_seconds)
		else:
			self.token_expiry = _utcnow() + timedelta(minutes=10)

	def _ensure_token(self) -> None:
		if not self.token or not self.token_expiry or _utcnow() >= self.token_expiry:
			self.authenticate()

	def _headers(self) -> Dict[str, str]:
		self._ensure_token()
		assert self.token  # nosec - guaranteed by _ensure_token()
		return {
			"Authorization": f"Bearer {self.token}",
			"Content-Type": "application/json",
		}

	def _request(self, method: str, url: str, **kwargs) -> requests.Response:
		response = self.session.request(method, url, **kwargs)

		if response.status_code == 401:
			logger.info("Received 401 from %s, refreshing token and retrying...", url)
			try:
				self.authenticate()
			except Exception as exc:
				logger.error("Failed to refresh token during retry: %s", exc)
				return response

			# Update Authorization header
			if "headers" in kwargs:
				kwargs["headers"].update(self._headers())
			else:
				kwargs["headers"] = self._headers()

			response = self.session.request(method, url, **kwargs)

		return response

	def get_boards(self) -> List[Dict[str, Any]]:
		url = f"{self.base_url}/v2/ticketing/trigger/boards"
		response = self._request("GET", url, headers=self._headers(), timeout=self.timeout)
		response.raise_for_status()
		payload = response.json()
		if isinstance(payload, list):
			return payload
		logger.debug("Unexpected boards payload type %s", type(payload))
		return []

	def run_board(self, board_id: int, *, page_size: int = DEFAULT_PAGE_SIZE) -> List[Dict[str, Any]]:
		url = f"{self.base_url}/v2/ticketing/trigger/board/{board_id}/run"
		payload = {"pageSize": page_size}
		response = self._request("POST", url, headers=self._headers(), json=payload, timeout=self.timeout)
		response.raise_for_status()
		data = response.json()
		if isinstance(data, dict):
			for key in ("data", "results"):
				if key in data and isinstance(data[key], list):
					return data[key]
		if isinstance(data, list):
			return data
		logger.debug("Unexpected board response for %s: %s", board_id, type(data))
		return []

	def get_ticket_details(self, ticket_id: int) -> Dict[str, Any]:
		url = f"{self.base_url}/v2/ticketing/ticket/{ticket_id}"
		response = self._request("GET", url, headers=self._headers(), timeout=self.timeout)
		response.raise_for_status()
		return response.json()

	def get_ticket_log_entries(self, ticket_id: int) -> List[Dict[str, Any]]:
		url = f"{self.base_url}/v2/ticketing/ticket/{ticket_id}/log-entry"
		response = self._request("GET", url, headers=self._headers(), timeout=self.timeout)
		response.raise_for_status()
		data = response.json()
		if isinstance(data, list):
			return data
		logger.debug("Unexpected log entries payload for %s: %s", ticket_id, type(data))
		return []

	def get_ticket_with_logs(self, ticket_id: int) -> Dict[str, Any]:
		ticket = self.get_ticket_details(ticket_id)
		ticket["log_entries"] = self.get_ticket_log_entries(ticket_id)
		return ticket

	def download_image(self, url: str) -> Optional[str]:
		"""Download an image and return it as a base64 data URI."""
		try:
			# If the URL is relative or missing scheme, might need handling, 
			# but usually they are full URLs or relative to base.
			# NinjaOne images might be authenticated.
			response = self.session.get(url, headers=self._headers(), timeout=self.timeout)
			if response.status_code != 200:
				# Try without headers if it's external? 
				# But usually ticket images are internal.
				# If it fails with auth, maybe it's external.
				if response.status_code in (401, 403):
					response = self.session.get(url, timeout=self.timeout)
			
			if response.status_code == 200:
				content_type = response.headers.get("Content-Type", "image/jpeg")
				encoded = base64.b64encode(response.content).decode("utf-8")
				return f"data:{content_type};base64,{encoded}"
			
			logger.warning("Failed to download image %s: status %s", url, response.status_code)
			return None
		except Exception as exc:
			logger.warning("Error downloading image %s: %s", url, exc)
			return None


class ListenerState:
	"""Persist listener progress to disk so we avoid duplicate work."""

	def __init__(self, state_path: Path) -> None:
		self.path = state_path
		self.last_polled_at: Optional[datetime] = None
		self.ticket_activity: Dict[str, float] = {}
		self._load()

	def _load(self) -> None:
		if not self.path.exists():
			return
		try:
			with self.path.open("r", encoding="utf-8") as handle:
				data = json.load(handle)
		except json.JSONDecodeError:
			logger.warning("Listener state file %s is not valid JSON; starting fresh", self.path)
			return
		except OSError as exc:
			logger.warning("Unable to read listener state %s: %s", self.path, exc)
			return

		last_polled = data.get("last_polled_at")
		if isinstance(last_polled, str):
			try:
				self.last_polled_at = datetime.fromisoformat(last_polled)
				if self.last_polled_at.tzinfo is None:
					self.last_polled_at = self.last_polled_at.replace(tzinfo=timezone.utc)
			except ValueError:
				logger.debug("Invalid last_polled_at in state: %s", last_polled)

		for ticket_id, timestamp in (data.get("ticket_activity") or {}).items():
			try:
				self.ticket_activity[str(ticket_id)] = float(timestamp)
			except (TypeError, ValueError):
				logger.debug("Invalid timestamp for ticket %s in state", ticket_id)

	def save(self) -> None:
		payload = {
			"last_polled_at": self.last_polled_at.isoformat() if self.last_polled_at else None,
			"ticket_activity": self.ticket_activity,
		}
		tmp_path = self.path.with_suffix(".tmp")
		try:
			self.path.parent.mkdir(parents=True, exist_ok=True)
			with tmp_path.open("w", encoding="utf-8") as handle:
				json.dump(payload, handle, indent=2)
			tmp_path.replace(self.path)
		except OSError as exc:
			logger.error("Failed to persist listener state to %s: %s", self.path, exc)

	def reset(self) -> None:
		self.last_polled_at = None
		self.ticket_activity.clear()
		if self.path.exists():
			try:
				self.path.unlink()
			except OSError:
				logger.debug("Unable to remove state file %s", self.path)

	def get_ticket_last_activity(self, ticket_id: int) -> Optional[float]:
		return self.ticket_activity.get(str(ticket_id))

	def update_ticket(self, ticket_id: int, timestamp: float) -> None:
		self.ticket_activity[str(ticket_id)] = float(timestamp)


@dataclass
class PollResult:
	processed: int
	since: Optional[datetime]


class TicketListener:
	"""Coordinate NinjaOne polling and ticket parsing."""

	def __init__(
		self,
		client: NinjaOneClient,
		parser: TicketParser,
		state_path: Path,
		*,
		poll_interval: int = DEFAULT_POLL_INTERVAL,
		page_size: int = DEFAULT_PAGE_SIZE,
		test_mode: bool = False,
	) -> None:
		self.client = client
		self.parser = parser
		self.state = ListenerState(state_path)
		self.poll_interval = poll_interval
		self.page_size = page_size
		self.test_mode = test_mode
		self.startup_time = _utcnow()

	def poll_once(self) -> PollResult:
		now = _utcnow()
		minimum_since = now - timedelta(seconds=self.poll_interval)
		last_polled = self.state.last_polled_at
		since = last_polled if last_polled and last_polled > minimum_since else minimum_since

		since_epoch = since.timestamp() if since else None
		processed = 0
		seen_ticket_ids: set[int] = set()

		try:
			boards = self.client.get_boards()
		except requests.RequestException as exc:
			logger.error("Could not retrieve NinjaOne boards: %s", exc)
			return PollResult(processed=0, since=since)

		for board in boards:
			board_id = board.get("id")
			if board_id is None:
				continue
			board_name = board.get("name") or f"Board {board_id}"

			try:
				ticket_summaries = self.client.run_board(board_id, page_size=self.page_size)
			except requests.RequestException as exc:
				logger.error("Board %s (%s) query failed: %s", board_name, board_id, exc)
				continue

			for summary in ticket_summaries:
				ticket_id = summary.get("id")
				if ticket_id is None:
					continue
				if ticket_id in seen_ticket_ids:
					continue
				seen_ticket_ids.add(ticket_id)

				summary_ts = self._summary_timestamp(summary)
				last_processed_ts = self.state.get_ticket_last_activity(ticket_id)
				baseline = _max_ignore_none([since_epoch, last_processed_ts])
				
				if self.test_mode:
					# In test mode, filter by creation date from summary to avoid fetching details for old tickets.
					# We ONLY want tickets created AFTER the listener started.
					create_time = summary.get("createTime")
					if create_time:
						try:
							created_dt = datetime.fromtimestamp(float(create_time), tz=timezone.utc)
							if created_dt < self.startup_time:
								continue
						except (ValueError, TypeError):
							pass
				elif summary_ts is not None and baseline is not None and summary_ts <= baseline:
					continue
				
				if not self.test_mode and summary_ts is None and last_processed_ts is not None and since_epoch is not None and last_processed_ts >= since_epoch:
					continue

				try:
					detailed_ticket = self.client.get_ticket_with_logs(ticket_id)
				except requests.RequestException as exc:
					logger.error("Unable to fetch ticket %s details: %s", ticket_id, exc)
					continue

				if self.test_mode:
					# Filter for tickets assigned to a specific technician (configure ID below) and created after startup
					assignee_id = detailed_ticket.get("assignedAppUserId")
					if assignee_id is None:
						assignee = detailed_ticket.get("assignedTo")
						# assignedTo can be None or a dict
						if isinstance(assignee, dict):
							assignee_id = assignee.get("id")
					
					if assignee_id != 5:
						continue

					create_time = detailed_ticket.get("createTime")
					if create_time:
						try:
							created_dt = datetime.fromtimestamp(float(create_time), tz=timezone.utc)
							if created_dt < self.startup_time:
								continue
						except (ValueError, TypeError):
							continue
					else:
						continue

				latest_activity = self.parser.get_latest_activity_timestamp(detailed_ticket)
				if latest_activity is None:
					continue

				baseline = _max_ignore_none([since_epoch, last_processed_ts])
				if not self.test_mode and baseline is not None and latest_activity <= baseline:
					continue

				try:
					output_path = self.parser.parse_and_save(
						detailed_ticket, 
						board=board_name,
						image_downloader=self.client.download_image
					)
				except Exception as exc:  # pragma: no cover - defensive
					logger.error("Failed to parse ticket %s: %s", ticket_id, exc)
					continue

				self.state.update_ticket(ticket_id, latest_activity)
				processed += 1
				logger.info("Ticket %s parsed to %s", ticket_id, output_path)

		self.state.last_polled_at = now
		self.state.save()
		return PollResult(processed=processed, since=since)

	def run_forever(self) -> None:
		logger.info("Ticket listener started; interval=%ss page_size=%s", self.poll_interval, self.page_size)
		try:
			while True:
				start = time.monotonic()
				try:
					result = self.poll_once()
					logger.info("Poll complete: %s ticket(s) processed", result.processed)
				except AuthenticationError as exc:
					logger.error("Authentication failed: %s", exc)
					time.sleep(self.poll_interval)
					continue
				except Exception as exc:  # pragma: no cover - defensive guard
					logger.exception("Unexpected error while polling: %s", exc)
				elapsed = time.monotonic() - start
				sleep_for = max(0.0, self.poll_interval - elapsed)
				time.sleep(sleep_for)
		finally:
			self.client.close()

	def reset_state(self) -> None:
		self.state.reset()

	@staticmethod
	def _summary_timestamp(summary: Dict[str, Any]) -> Optional[float]:
		for field in (
			"lastLogEntryCreationTime",
			"updateTime",
			"createTime",
			"lastCommentTime",
		):
			value = summary.get(field)
			if value in (None, 0, ""):
				continue
			try:
				return float(value)
			except (TypeError, ValueError):
				continue
		return None


def configure_logging(verbose: bool, log_file: Optional[Path] = None) -> None:
	level = logging.DEBUG if verbose else logging.INFO
	handlers: List[logging.Handler] = [logging.StreamHandler()]
	if log_file:
		log_file.parent.mkdir(parents=True, exist_ok=True)
		handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

	logging.basicConfig(
		level=level, 
		format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
		handlers=handlers,
		force=True
	)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Poll NinjaOne for recent tickets")
	parser.add_argument("--interval", type=int, default=DEFAULT_POLL_INTERVAL, help="Polling interval in seconds")
	parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE, help="Number of tickets to request per board call")
	parser.add_argument("--run-once", action="store_true", help="Run a single poll and exit")
	parser.add_argument("--reset-state", action="store_true", help="Reset listener tracking state before polling")
	parser.add_argument("--test-mode", action="store_true", help="Only process tickets assigned to a specific technician from today")
	parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
	return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> None:
	args = parse_args(argv)
	configure_logging(args.verbose)

	repo_root = Path(__file__).resolve().parent
	env_path = repo_root / "api_keys.env"
	load_dotenv(env_path)

	client_id = os.getenv("NinjaOne_ClientID")
	client_secret = os.getenv("NinjaOne_ClientSecret")
	base_url = os.getenv("NinjaOne_BaseURL", "https://app.ninjarmm.com")
	refresh_token = os.getenv("NINJA_REFRESH_TOKEN")

	if not client_id or not client_secret:
		raise AuthenticationError("NinjaOne credentials are missing from api_keys.env or environment variables")

	tickets_dir = repo_root / "docs" / "tickets"
	state_path = tickets_dir / ".listener_state.json"

	def save_refresh_token(token: str) -> None:
		set_key(env_path, "NINJA_REFRESH_TOKEN", token)
		logger.info("Updated NINJA_REFRESH_TOKEN in %s", env_path)

	client = NinjaOneClient(
		base_url=base_url, 
		client_id=client_id, 
		client_secret=client_secret,
		refresh_token=refresh_token,
		on_token_refresh=save_refresh_token,
	)
	parser = TicketParser(tickets_dir)
	listener = TicketListener(
		client, 
		parser, 
		state_path, 
		poll_interval=args.interval, 
		page_size=args.page_size,
		test_mode=args.test_mode
	)

	if args.reset_state:
		listener.reset_state()

	if args.run_once:
		try:
			result = listener.poll_once()
			logger.info("Single poll processed %s ticket(s)", result.processed)
		finally:
			client.close()
		return

	listener.run_forever()


if __name__ == "__main__":
	main()
