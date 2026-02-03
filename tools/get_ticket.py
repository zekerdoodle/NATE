"""Fetch ticket details directly from NinjaOne."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

# Ensure we can import from repo root
sys.path.append(str(Path(__file__).resolve().parent.parent))

from ticket_parser import TicketParser
from tools.exceptions import ToolExecutionError

LOGGER = logging.getLogger("tools.get_ticket")
TOKEN_CACHE_FILENAME = ".ninjaone_token.json"
DEFAULT_TIMEOUT = 30.0

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

class NinjaOneClient:
    """Minimal NinjaOne client for fetching tickets."""

    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        client_secret: str,
        repo_root: Path,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.repo_root = repo_root
        self.timeout = timeout
        self.session = requests.Session()
        self.token: Optional[str] = None
        
        # Load initial token
        self._load_token()

    def _load_token(self) -> None:
        cache = _load_token_cache(self.repo_root)
        self.token = cache.get("access_token")
        # We could check expiry, but simple retry on 401 is robust enough

    def _authenticate(self) -> None:
        url = f"{self.base_url}/ws/oauth/token"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        
        # Try refresh token from cache/env
        cache = _load_token_cache(self.repo_root)
        refresh_token = cache.get("refresh_token") or os.getenv("NINJA_REFRESH_TOKEN")
        
        if refresh_token:
            payload = {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
            resp = self.session.post(url, data=payload, headers=headers, timeout=self.timeout)
            if resp.status_code == 200:
                self._handle_token_response(resp.json())
                return

        # Fallback to client credentials
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "monitoring management ticketing",
        }
        resp = self.session.post(url, data=payload, headers=headers, timeout=self.timeout)
        resp.raise_for_status()
        self._handle_token_response(resp.json())

    def _handle_token_response(self, data: Dict[str, Any]) -> None:
        self.token = data.get("access_token")
        # Persist
        _save_token_cache(self.repo_root, data)

    def _request(self, method: str, path: str, **kwargs) -> Any:
        if not self.token:
            self._authenticate()
        
        url = f"{self.base_url}{path}"
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.token}"
        
        resp = self.session.request(method, url, headers=headers, timeout=self.timeout, **kwargs)
        
        if resp.status_code == 401:
            self._authenticate()
            headers["Authorization"] = f"Bearer {self.token}"
            resp = self.session.request(method, url, headers=headers, timeout=self.timeout, **kwargs)
            
        resp.raise_for_status()
        return resp.json()

    def get_ticket_details(self, ticket_id: int) -> Dict[str, Any]:
        return self._request("GET", f"/v2/ticketing/ticket/{ticket_id}")

    def get_ticket_logs(self, ticket_id: int) -> list[Dict[str, Any]]:
        return self._request("GET", f"/v2/ticketing/ticket/{ticket_id}/log-entry")

    def download_image(self, url: str) -> Optional[str]:
        # Placeholder: we might not strictly need image downloading for this tool 
        # unless we want full parity. For now, return None to skip images.
        return None

def run(parameters: Dict[str, Any], *, repo_root: Path) -> Dict[str, Any]:
    ticket_id_raw = parameters.get("ticket_id")
    if not ticket_id_raw:
        raise ToolExecutionError("ticket_id is required")
    
    try:
        ticket_id = int(str(ticket_id_raw).strip())
    except ValueError:
        raise ToolExecutionError("ticket_id must be an integer")

    load_dotenv(repo_root / "api_keys.env")
    
    client = NinjaOneClient(
        base_url=os.getenv("NinjaOne_BaseURL", "https://app.ninjarmm.com"),
        client_id=os.getenv("NinjaOne_ClientID", ""),
        client_secret=os.getenv("NinjaOne_ClientSecret", ""),
        repo_root=repo_root,
    )

    try:
        ticket = client.get_ticket_details(ticket_id)
        logs = client.get_ticket_logs(ticket_id)
        ticket["log_entries"] = logs
        
        # Parse using the standard parser
        parser = TicketParser(repo_root / "docs" / "tickets")
        parsed = parser.parse_ticket(ticket, image_downloader=client.download_image)
        
        # Optionally save it to disk so it's cached for future 'read_file' calls
        # This mimics the listener's behavior
        output_path = repo_root / "docs" / "tickets" / f"{ticket_id}.json"
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(parsed, f, indent=2)
            
        return parsed

    except Exception as exc:
        raise ToolExecutionError(f"Failed to fetch ticket {ticket_id}: {exc}") from exc
