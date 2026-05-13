"""Tests for the Sendblue iMessage gateway adapter."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig


def _make_adapter(monkeypatch, **extra):
    monkeypatch.setenv("SENDBLUE_API_KEY_ID", "test-key-id")
    monkeypatch.setenv("SENDBLUE_API_SECRET", "test-secret")
    monkeypatch.setenv("SENDBLUE_NUMBER", "+15555550100")
    monkeypatch.setenv("SENDBLUE_WEBHOOK_SECRET", "test-webhook-secret")
    from gateway.platforms.sendblue import SendblueAdapter

    cfg = PlatformConfig(
        enabled=True,
        extra={
            "api_key_id": "test-key-id",
            "api_secret": "test-secret",
            "sendblue_number": "+15555550100",
            "webhook_secret": "test-webhook-secret",
            **extra,
        },
    )
    return SendblueAdapter(cfg)


class _MockRequest:
    """Minimal aiohttp.Request surface for _handle_webhook tests.

    Exposes .headers, .json() async, and .remote. Enough for the handler
    to do signature check, body parse, and logging — no more.
    """
    def __init__(self, body, headers=None, remote="127.0.0.1"):
        self._body = body
        self.headers = headers or {}
        self.remote = remote

    async def json(self):
        return self._body


async def _drain_background_tasks(adapter):
    """Wait for fire-and-forget asyncio.create_task() to settle.

    _handle_webhook does asyncio.create_task(self.handle_message(event))
    and returns immediately. To assert on handle_message side effects,
    we need to yield the event loop once for the task to actually run.
    """
    if adapter._background_tasks:
        await asyncio.gather(*adapter._background_tasks, return_exceptions=True)
    else:
        await asyncio.sleep(0)


class TestSendblueSignatureVerification:
    def test_correct_secret_passes(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, webhook_secret="abc123")
        assert adapter._verify_signature("abc123") is True

    def test_wrong_secret_fails(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, webhook_secret="abc123")
        assert adapter._verify_signature("wrong") is False

    def test_empty_header_with_configured_secret_fails(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, webhook_secret="abc123")
        assert adapter._verify_signature("") is False

    def test_no_secret_configured_passes_any_header(self, monkeypatch):
        # _make_adapter sets SENDBLUE_WEBHOOK_SECRET env var, and the adapter's
        # `webhook_secret or os.getenv(...)` fallback means passing
        # extra={"webhook_secret": ""} falls through to the env var. Mutate
        # post-construction to test the "no secret configured" branch.
        adapter = _make_adapter(monkeypatch)
        adapter.webhook_secret = ""
        assert adapter._verify_signature("whatever") is True


class TestSendblueWebhookRouting:
    @pytest.mark.asyncio
    async def test_routes_message_for_configured_number(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)  # SENDBLUE_NUMBER = +15555550100
        adapter.handle_message = AsyncMock()
        payload = {
            "is_outbound": False,
            "sendblue_number": "+15555550100",
            "from_number": "+17766768883",
            "content": "hello",
        }
        request = _MockRequest(
            body=payload,
            headers={"sb-signing-secret": "test-webhook-secret"},
        )
        response = await adapter._handle_webhook(request)
        await _drain_background_tasks(adapter)
        assert response.status == 200
        assert adapter.handle_message.call_count == 1

    @pytest.mark.asyncio
    async def test_silently_drops_message_for_other_number(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)  # SENDBLUE_NUMBER = +15555550100
        adapter.handle_message = AsyncMock()
        payload = {
            "is_outbound": False,
            "sendblue_number": "+15555559999",  # NOT our number
            "from_number": "+17766768883",
            "content": "hello",
        }
        request = _MockRequest(
            body=payload,
            headers={"sb-signing-secret": "test-webhook-secret"},
        )
        response = await adapter._handle_webhook(request)
        await _drain_background_tasks(adapter)
        assert response.status == 200  # silent — 200 even though dropped
        assert adapter.handle_message.call_count == 0

    @pytest.mark.asyncio
    async def test_no_number_configured_processes_all(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter.sendblue_number = ""  # disable number filter
        adapter.handle_message = AsyncMock()
        payload = {
            "is_outbound": False,
            "sendblue_number": "+15555559999",  # any number
            "from_number": "+17766768883",
            "content": "hello",
        }
        request = _MockRequest(
            body=payload,
            headers={"sb-signing-secret": "test-webhook-secret"},
        )
        response = await adapter._handle_webhook(request)
        await _drain_background_tasks(adapter)
        assert response.status == 200
        assert adapter.handle_message.call_count == 1
