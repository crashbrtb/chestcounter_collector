"""Client for the chestcounter site's uploader API (/api/v1).

The token goes in both `Authorization: Bearer` and `X-Api-Token`: some hosts
running PHP as FastCGI drop the first one before the site sees it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

# How the site knows this client; kept from the standalone uploader.
CLIENT_NAME = "EventUploader"

TIMEOUT = 25


class ApiError(Exception):
    def __init__(self, status: int, message: str, errors: Optional[Dict[str, str]] = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.errors = errors or {}

    def describe(self) -> str:
        text = f"{self.message} (HTTP {self.status})" if self.status else self.message
        if self.errors:
            text += "\n" + "\n".join(f"- {v}" for v in list(self.errors.values())[:10])
        return text


def normalize_site_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "https://" + url
    for suffix in ("/api/v1", "/api"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


class SiteClient:
    def __init__(self, site_url: str, token: str, client_version: str = "", session: Optional[requests.Session] = None):
        self.base = normalize_site_url(site_url) + "/api/v1"
        self.token = (token or "").strip()
        self.client_version = client_version
        self.http = session or requests.Session()

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "X-Api-Token": self.token,
            "Accept": "application/json",
            "User-Agent": f"{CLIENT_NAME}/{self.client_version}",
        }

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        if not self.token:
            raise ApiError(0, "The API token is missing.")
        try:
            response = self.http.request(
                method, self.base + path, json=body, headers=self._headers(), timeout=TIMEOUT
            )
        except requests.exceptions.SSLError as exc:
            raise ApiError(0, f"HTTPS certificate failure: {exc}") from exc
        except requests.exceptions.RequestException as exc:
            raise ApiError(0, f"Could not reach the site: {exc}") from exc

        try:
            data = response.json()
        except ValueError:
            data = None

        if response.status_code >= 400:
            if isinstance(data, dict):
                raise ApiError(response.status_code, str(data.get("error") or data.get("message") or "Error"), data.get("errors"))
            raise ApiError(response.status_code, "The site answered with an error. Check the site address.")
        if not isinstance(data, dict):
            raise ApiError(response.status_code, "Unexpected answer: does the address really point to the chestcounter site?")
        return data

    def me(self) -> dict:
        return self._request("GET", "/me")

    def known_tournaments(self) -> List[dict]:
        return self._request("GET", "/tournaments/known").get("tournaments", [])

    def send_tournament(self, payload: dict) -> dict:
        return self._request("POST", "/tournaments", payload)

    def send_catalog(self, entries: List[dict]) -> List[dict]:
        return self._request("POST", "/tournament-catalog", {"entries": entries}).get("results", [])

    def send_members(self, payload: dict) -> dict:
        """The clan's Members list: who is in it becomes active on the site, everyone else inactive."""
        return self._request("POST", "/members", payload)

    def lookup(self, result_uids: List[str]) -> Dict[str, dict]:
        """What the site already has for these game results: {uid: {event_id, published, ...}}."""
        found: Dict[str, dict] = {}
        for start in range(0, len(result_uids), 500):
            chunk = result_uids[start:start + 500]
            found.update(self._request("POST", "/tournaments/lookup", {"result_uids": chunk}).get("tournaments") or {})
        return found

    def search_state(self) -> dict:
        """Where the automatic search has to look back to: {covered_until, latest_ended_at, last_run}."""
        return self._request("GET", "/searches/last")

    def report_search(self, report: dict) -> dict:
        return self._request("POST", "/searches", report)
