"""Pinned Nemotron serving adapter.

OpenAI-compatible chat completions. The agent's ONLY outbound network call is here.
Identity (base URL, key, model id) comes from .env via python-dotenv. Inference settings
(temperature, max_tokens, ...) are pinned in agent.config so every agent decides alike.

Safety invariants:
  * The API key is read from the environment and sent only in the Authorization header.
    It is never logged, never echoed, and never placed in a prompt.
  * 429 and 5xx responses are retried with capped exponential backoff.
  * Any <think>...</think> block the model emits is stripped before the text is used.
"""
from __future__ import annotations

import os
import re
import time

import requests
from dotenv import load_dotenv

from agent.config import ModelConfig

load_dotenv()  # read .env if present; never prints anything

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
# Also drop a dangling, unclosed <think> ... (some models stream reasoning without a close).
_OPEN_THINK_RE = re.compile(r"<think>.*\Z", re.DOTALL | re.IGNORECASE)


def strip_think(text: str) -> str:
    """Remove reasoning blocks the model is not asked to disclose."""
    text = _THINK_RE.sub("", text)
    text = _OPEN_THINK_RE.sub("", text)
    return text.strip()


class ModelError(RuntimeError):
    """Raised when the model cannot be reached or returns an unusable response.

    The message never contains the API key.
    """


class NemotronModel:
    """Thin chat client. `chat(messages) -> str` returns the (think-stripped) content."""

    def __init__(self, config: ModelConfig | None = None, session: requests.Session | None = None):
        self.config = config or ModelConfig()
        self._base_url = (os.environ.get(self.config.base_url_env) or "").rstrip("/")
        self._api_key = os.environ.get(self.config.api_key_env) or ""
        self.model_id = os.environ.get(self.config.model_id_env) or ""
        self._session = session or requests.Session()

    # The repr must never leak the key.
    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"NemotronModel(model_id={self.model_id!r}, base_url={self._base_url!r})"

    def available(self) -> bool:
        return bool(self._base_url and self._api_key and self.model_id)

    def _require_config(self) -> None:
        missing = [
            name
            for name, val in (
                (self.config.base_url_env, self._base_url),
                (self.config.api_key_env, self._api_key),
                (self.config.model_id_env, self.model_id),
            )
            if not val
        ]
        if missing:
            raise ModelError(f"missing environment configuration: {', '.join(missing)}")

    def chat(self, messages: list[dict], *, response_json: bool = False) -> str:
        """POST messages to {base_url}/chat/completions and return stripped content."""
        self._require_config()
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        body: dict = {
            "model": self.model_id,
            "messages": messages,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "max_tokens": self.config.max_tokens,
            "stream": False,
        }
        if response_json:
            body["response_format"] = {"type": "json_object"}

        last_err: str | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                resp = self._session.post(url, headers=headers, json=body, timeout=self.config.timeout_s)
            except requests.RequestException as exc:
                last_err = f"request failed: {type(exc).__name__}"
                self._sleep(attempt)
                continue

            if resp.status_code == 200:
                try:
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    raise ModelError(f"unexpected response shape: {type(exc).__name__}") from None
                return strip_think(content or "")

            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                last_err = f"HTTP {resp.status_code}"
                self._sleep(attempt, retry_after=resp.headers.get("Retry-After"))
                continue

            # Non-retryable (e.g. 400/401/403). Never include the body in case it echoes input.
            raise ModelError(f"model endpoint returned HTTP {resp.status_code}")

        raise ModelError(f"model endpoint unavailable after {self.config.max_retries + 1} attempts ({last_err})")

    def _sleep(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                time.sleep(min(float(retry_after), self.config.backoff_cap_s))
                return
            except (TypeError, ValueError):
                pass
        delay = min(self.config.backoff_base_s * (2**attempt), self.config.backoff_cap_s)
        time.sleep(delay)
