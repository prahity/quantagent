from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from meterless.provider import (
    MeterlessRuntime,
    apply_meterless_dotenv,
    auto_detect,
    load_settings,
    normalize_base_url,
    parse_connect_link,
)
from meterless.trader import NemotronTrader


def test_normalize_base_url_collapses_pasted_forms():
    expected = "http://127.0.0.1:8088/v1"
    for raw in (
        "127.0.0.1:8088",
        "http://127.0.0.1:8088",
        "http://127.0.0.1:8088/",
        "http://127.0.0.1:8088/v1",
        "http://127.0.0.1:8088/v1/",
        "http://127.0.0.1:8088/v1/models",
        "http://127.0.0.1:8088/v1/chat/completions",
        "http://127.0.0.1:8088/healthz",
    ):
        assert normalize_base_url(raw) == expected


def test_connect_link_cases():
    full = parse_connect_link("http://127.0.0.1:8088/connect#k=secret-token")
    assert full["base_url"] == "http://127.0.0.1:8088/v1"
    assert full["meterless_runtime_token"] == "secret-token"
    assert full["model"] == "meterless/auto"
    assert full["health_url"] == "http://127.0.0.1:8088/healthz"

    with_extra = parse_connect_link("https://nightshift.local:8787/connect/#k=a%2Fb&m=meterless/fast&n=ai-trader")
    assert with_extra["base_url"] == "https://nightshift.local:8787/v1"
    assert with_extra["meterless_runtime_token"] == "a/b"
    assert with_extra["model"] == "meterless/fast"
    assert with_extra["app"] == "ai-trader"

    assert parse_connect_link("http://127.0.0.1:8088/v1") is None
    assert parse_connect_link("http://127.0.0.1:8088/connect") is None
    assert parse_connect_link("http://127.0.0.1:8088/other#k=secret") is None


def test_placeholder_token_is_unauthorized_without_calling_offline():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("placeholder must not be sent")

    runtime = MeterlessRuntime("http://127.0.0.1:8088/v1", "rt-local", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert runtime.probe().status == "unauthorized"


def test_wrong_token_is_unauthorized_not_offline():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer wrong"
        assert request.headers["meterless-app"] == "ai-trader"
        return httpx.Response(401, json={"error": "unauthorized"})

    runtime = MeterlessRuntime("http://127.0.0.1:8088/v1", "wrong", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert runtime.probe().status == "unauthorized"


def test_models_with_meterless_ids_are_online_and_healthz_is_not_enough():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, json={"data": [{"id": "meterless/auto"}, {"id": "other"}]})

    runtime = MeterlessRuntime("http://127.0.0.1:8088/healthz", "real", client=httpx.Client(transport=httpx.MockTransport(handler)))
    probe = runtime.probe()
    assert probe.status == "online"
    assert probe.models == ["meterless/auto"]
    assert runtime.base_url == "http://127.0.0.1:8088/v1"


def test_chat_sends_a_string_and_returns_a_string():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert isinstance(body["messages"][0]["content"], str)
        assert body["model"] == "meterless/auto"
        assert request.headers["meterless-scope"] == "user"
        return httpx.Response(200, json={"choices": [{"message": {"content": "confirmed"}}]})

    runtime = MeterlessRuntime("http://127.0.0.1:8088/v1", "real", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert runtime.complete("Reply with one sentence.") == "confirmed"


def test_chat_records_the_served_model_and_forwards_thinking_off():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        return httpx.Response(
            200,
            headers={"x-upstream-model": "nvidia/nemotron-3.5-lightning-30b-a3b", "Authorization": "Bearer hidden"},
            json={
                "model": "nvidia/nemotron-3.5-lightning-30b-a3b",
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
                "meterless": {
                    "requested_model": "nvidia/nemotron-3.5-lightning-30b-a3b",
                    "provider_model": "nvidia/nemotron-3.5-lightning-30b-a3b",
                    "routed_model": "nvidia/nemotron-3.5-lightning-30b-a3b",
                    "winning_route": {
                        "model": "nvidia/nemotron-3.5-lightning-30b-a3b",
                        "label": "Nemotron 3.5 Lightning 30B A3B",
                        "provider": "nvidia",
                        "kind": "cloud",
                    },
                    "usage": {"input_tokens": 99},
                },
            },
        )

    runtime = MeterlessRuntime(
        "http://127.0.0.1:8088/v1",
        "real",
        "nvidia/nemotron-3.5-lightning-30b-a3b",
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert runtime.complete("Reply with the single word ok.") == "ok"
    recorded = runtime.last_completion
    assert recorded is not None
    assert recorded.finish_reason == "stop"
    assert recorded.reasoning is False
    assert recorded.reported_model == "nvidia/nemotron-3.5-lightning-30b-a3b"
    assert recorded.endpoint_host == "127.0.0.1:8088"
    assert recorded.routing_headers == {"x-upstream-model": "nvidia/nemotron-3.5-lightning-30b-a3b"}
    assert recorded.route["winning_route"]["label"] == "Nemotron 3.5 Lightning 30B A3B"
    assert "usage" not in recorded.route


def test_dotenv_meterless_values_replace_a_stale_process_url(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "METERLESS_BASE_URL=http://127.0.0.1:8088/v1\n"
        "METERLESS_RUNTIME_TOKEN=rt-from-file\n"
        "METERLESS_MODEL=nvidia/nemotron-3.5-lightning-30b-a3b\n",
        encoding="utf-8",
    )
    environ = {
        "METERLESS_BASE_URL": "https://integrate.api.nvidia.com/v1",
        "METERLESS_RUNTIME_TOKEN": "stale-cloud-key",
        "METERLESS_MODEL": "other",
    }
    apply_meterless_dotenv(env_file, environ)
    settings = load_settings(environ)
    assert settings["base_url"] == "http://127.0.0.1:8088/v1"
    assert settings["meterless_runtime_token"] == "rt-from-file"
    assert settings["model"] == "nvidia/nemotron-3.5-lightning-30b-a3b"


def test_auto_detect_skips_a_dead_port_and_keeps_an_unauthorized_runtime():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.port != 8090:
            raise httpx.ConnectError("down")
        return httpx.Response(403, json={"error": "forbidden"})

    found = auto_detect("real", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert found is not None
    assert found.status == "unauthorized"
    assert found.base_url == "http://127.0.0.1:8090/v1"


def test_trader_holds_when_the_reply_is_not_json():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "I would buy something."}}]})

    runtime = MeterlessRuntime("http://127.0.0.1:8088/v1", "real", client=httpx.Client(transport=httpx.MockTransport(handler)))
    request = {"run_id": "r", "request_id": "d", "mode": "V1", "window_start": "2025-01-02T14:30:00Z", "window_end": "2025-01-03T14:30:00Z"}
    action = NemotronTrader(runtime).decide(request)
    assert action["orders"] == []
    assert action["run_id"] == "r"


def test_trader_passes_through_a_json_action():
    payload = {
        "schema_version": "1.0",
        "run_id": "r",
        "request_id": "d",
        "mode": "V1",
        "window_start": "2025-01-02T14:30:00Z",
        "window_end": "2025-01-03T14:30:00Z",
        "orders": [],
        "reason": "flat into the print",
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})

    runtime = MeterlessRuntime("http://127.0.0.1:8088/v1", "real", client=httpx.Client(transport=httpx.MockTransport(handler)))
    action = NemotronTrader(runtime).decide({"run_id": "r", "request_id": "d", "mode": "V1", "window_start": payload["window_start"], "window_end": payload["window_end"]})
    assert action["reason"] == "flat into the print"
