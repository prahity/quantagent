"""Transport-agnostic view of the simulator from the agent's side.

``FeedReader`` is the released-data interface (what Zetaris will serve later).
``SimClient`` wraps the fixed JSON request/action exchange over HTTP.
"""
from __future__ import annotations

from typing import Any, Protocol

import httpx


class FeedReader(Protocol):
    def prices(self, symbol: str, limit: int | None = None) -> list[dict]: ...
    def news(self, symbols: list[str] | None = None) -> list[dict]: ...
    def marks(self) -> dict[str, dict]: ...


class HttpFeedReader:
    def __init__(self, base_url: str, run_id: str, client: httpx.Client):
        self._base = base_url.rstrip("/")
        self._run_id = run_id
        self._client = client

    def prices(self, symbol: str, limit: int | None = None) -> list[dict]:
        params = {"symbol": symbol}
        if limit is not None:
            params["limit"] = str(limit)
        resp = self._client.get(f"{self._base}/api/runs/{self._run_id}/feed/prices", params=params)
        resp.raise_for_status()
        return resp.json()["bars"]

    def news(self, symbols: list[str] | None = None) -> list[dict]:
        params = {"symbols": ",".join(symbols)} if symbols else {}
        resp = self._client.get(f"{self._base}/api/runs/{self._run_id}/feed/news", params=params)
        resp.raise_for_status()
        return resp.json()["records"]

    def marks(self) -> dict[str, dict]:
        resp = self._client.get(f"{self._base}/api/runs/{self._run_id}/feed/marks")
        resp.raise_for_status()
        return resp.json()["marks"]


class SimClient:
    """HTTP client for the runner-side API."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=60)

    def reset(self, run_config: dict[str, Any]) -> dict:
        resp = self._client.post(f"{self.base_url}/api/runs", json=run_config)
        resp.raise_for_status()
        return resp.json()

    def submit_action(self, run_id: str, payload: Any) -> dict:
        resp = self._client.post(f"{self.base_url}/api/runs/{run_id}/actions", json=payload)
        resp.raise_for_status()
        return resp.json()

    def feed(self, run_id: str) -> HttpFeedReader:
        return HttpFeedReader(self.base_url, run_id, self._client)

    def close(self) -> None:
        self._client.close()
