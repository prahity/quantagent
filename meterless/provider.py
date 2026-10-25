"""Meterless Runtime provider (contract v4, Runtime 1.4).

The app talks only to Nightshift. The NVIDIA key stays in Nightshift's Providers
tab and is never read here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse, urlunparse

import httpx

APP_ID = "ai-trader"
DEFAULT_BASE_URL = "http://127.0.0.1:8088/v1"
DEFAULT_MODEL = "meterless/auto"
PLACEHOLDER_TOKEN = "rt-local"
TOKEN_SETTING = "meterless_runtime_token"
DETECT_PORTS = (8088, 8090, 8787, 11434)
DETECT_HOSTS = ("127.0.0.1", "localhost", "[::1]")
CHAT_TIMEOUT = 180.0

_STRIP_SUFFIXES = ("/v1/chat/completions", "/v1/models", "/healthz", "/v1")


class ProviderError(Exception):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def normalize_base_url(raw: str) -> str:
    """Accept a pasted runtime URL and return ``<origin>/v1`` exactly once."""
    text = raw.strip()
    if not text:
        raise ValueError("base url is empty")
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    if not parsed.netloc:
        raise ValueError(f"base url has no host: {raw}")
    path = parsed.path.rstrip("/")
    for suffix in _STRIP_SUFFIXES:
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    path = path.rstrip("/") + "/v1"
    if path.endswith("/v1/v1"):
        raise ValueError("refusing to produce /v1/v1")
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def parse_connect_link(text: str) -> dict[str, str] | None:
    """A connect link is an http(s) URL whose path is ``/connect`` and whose fragment has ``k``.

    Anything else, including a bare base URL, is not a connect link.
    """
    candidate = text.strip()
    if "://" not in candidate:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.path.rstrip("/") != "/connect":
        return None
    fragment = parse_qs(parsed.fragment, keep_blank_values=False)
    if "k" not in fragment or not fragment["k"][0]:
        return None
    key = unquote(fragment["k"][0])
    model = unquote(fragment["m"][0]) if fragment.get("m") else DEFAULT_MODEL
    app = unquote(fragment["n"][0]) if fragment.get("n") else APP_ID
    origin = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    return {
        "base_url": origin + "/v1",
        "meterless_runtime_token": key,
        "model": model,
        "app": app,
        "health_url": origin + "/healthz",
    }


def load_settings(environ: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ if environ is None else environ
    return {
        "base_url": normalize_base_url(env.get("METERLESS_BASE_URL") or DEFAULT_BASE_URL),
        TOKEN_SETTING: env.get("METERLESS_RUNTIME_TOKEN") or env.get(TOKEN_SETTING) or "",
        "model": env.get("METERLESS_MODEL") or DEFAULT_MODEL,
    }


@dataclass
class Probe:
    status: str  # online | unauthorized | offline
    base_url: str
    models: list[str]
    detail: str = ""


def _meterless_ids(payload: Any) -> list[str]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    ids = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].startswith("meterless/"):
            ids.append(item["id"])
    return ids


class MeterlessRuntime:
    """OpenAI-compatible client for one Nightshift runtime."""

    def __init__(self, base_url: str, token: str, model: str = DEFAULT_MODEL, app_id: str = APP_ID, client: httpx.Client | None = None,
                 extra_body: dict[str, Any] | None = None):
        self.base_url = normalize_base_url(base_url)
        self.token = token.strip()
        self.model = model or DEFAULT_MODEL
        self.app_id = app_id
        self.extra_body = extra_body or {}
        self._client = client or httpx.Client(timeout=CHAT_TIMEOUT)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _headers(self, chat: bool) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "meterless-app": self.app_id,
        }
        if chat:
            headers["meterless-scope"] = "user"
        return headers

    def probe(self, *, local_placeholder: bool = True) -> Probe:
        """``/v1/models`` decides the state. ``/healthz`` is not consulted.

        The placeholder token is unauthorized on a configured provider. Auto Detect
        passes ``local_placeholder=False`` so a guess is not treated as a running runtime.
        """
        if local_placeholder and self.token == PLACEHOLDER_TOKEN:
            return Probe("unauthorized", self.base_url, [], "placeholder token rt-local is rejected")
        try:
            response = self._client.get(f"{self.base_url}/models", headers=self._headers(chat=False), timeout=10.0)
        except httpx.HTTPError as exc:
            return Probe("offline", self.base_url, [], f"{type(exc).__name__}: {exc}")
        snippet = response.text[:180].replace("\n", " ")
        if response.status_code in (401, 403):
            return Probe("unauthorized", self.base_url, [], f"HTTP {response.status_code}")
        if response.status_code != 200:
            return Probe("offline", self.base_url, [], f"HTTP {response.status_code} {snippet}")
        try:
            ids = _meterless_ids(response.json())
        except ValueError:
            return Probe("offline", self.base_url, [], f"HTTP 200 was not JSON: {snippet}")
        if not ids:
            return Probe("offline", self.base_url, [], f"HTTP 200 had no meterless ids: {snippet}")
        return Probe("online", self.base_url, ids, "")

    def complete(self, content: str, model: str | None = None, max_tokens: int | None = None) -> str:
        if not isinstance(content, str):
            raise TypeError("chat content must be a plain string")
        if not self.token or self.token == PLACEHOLDER_TOKEN:
            raise ProviderError("unauthorized", "runtime token is missing or the placeholder rt-local")
        chosen = model or self.model
        try:
            body = {**self.extra_body, "model": chosen, "messages": [{"role": "user", "content": content}]}
            if max_tokens is not None:
                body["max_tokens"] = max_tokens
            response = self._client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(chat=True),
                json=body,
                timeout=CHAT_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ProviderError("offline", "runtime did not answer") from exc
        if response.status_code in (401, 403):
            raise ProviderError("unauthorized", "runtime rejected the token")
        if response.status_code != 200:
            raise ProviderError("offline", f"runtime returned HTTP {response.status_code}")
        payload = response.json()
        message = payload["choices"][0]["message"]["content"]
        if not isinstance(message, str):
            raise ProviderError("offline", "runtime content was not a string")
        return message


def auto_detect(token: str, client: httpx.Client | None = None) -> Probe | None:
    """Try the documented local ports and return the first online or unauthorized runtime."""
    for host in DETECT_HOSTS:
        for port in DETECT_PORTS:
            runtime = MeterlessRuntime(f"http://{host}:{port}/v1", token, client=client)
            probe = runtime.probe(local_placeholder=False)
            if probe.status in ("online", "unauthorized"):
                return probe
    return None
