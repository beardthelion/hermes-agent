"""Tests for the Sendblue iMessage gateway adapter."""
import asyncio
import json
from unittest.mock import AsyncMock, Mock

import httpx
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
    to do signature check, body parse, and logging — no more. Pass
    json_error to make .json() raise that exception (used to test the
    JSONDecodeError → 400 path).
    """
    def __init__(self, body=None, headers=None, remote="127.0.0.1", json_error=None):
        self._body = body
        self.headers = headers or {}
        self.remote = remote
        self._json_error = json_error

    async def json(self):
        if self._json_error is not None:
            raise self._json_error
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


class _MockHttpxResponse:
    """Minimal httpx.Response surface for media download tests.

    Provides .content (bytes payload) and .raise_for_status() (no-op for
    2xx, raises HTTPStatusError for 4xx/5xx). Enough for the helper to
    do its happy path and to test error branches.
    """
    def __init__(self, content=b"", status_code=200):
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=httpx.Request("GET", "http://test"),
                response=self,
            )


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


class TestSendblueWebhookParsing:
    @pytest.mark.asyncio
    async def test_single_object_normalized_to_list(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter.handle_message = AsyncMock()
        payload = {
            "is_outbound": False,
            "sendblue_number": "+15555550100",
            "from_number": "+17766768883",
            "content": "hello",
        }
        request = _MockRequest(
            body=payload,  # dict, not list
            headers={"sb-signing-secret": "test-webhook-secret"},
        )
        response = await adapter._handle_webhook(request)
        await _drain_background_tasks(adapter)
        assert response.status == 200
        assert adapter.handle_message.call_count == 1

    @pytest.mark.asyncio
    async def test_array_processed_as_list(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter.handle_message = AsyncMock()
        item = {
            "is_outbound": False,
            "sendblue_number": "+15555550100",
            "from_number": "+17766768883",
            "content": "hello",
        }
        request = _MockRequest(
            body=[item, item],  # array of two valid items
            headers={"sb-signing-secret": "test-webhook-secret"},
        )
        response = await adapter._handle_webhook(request)
        await _drain_background_tasks(adapter)
        assert response.status == 200
        assert adapter.handle_message.call_count == 2

    @pytest.mark.asyncio
    async def test_malformed_json_returns_400(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter.handle_message = AsyncMock()
        request = _MockRequest(
            json_error=json.JSONDecodeError("expecting value", "", 0),
            headers={"sb-signing-secret": "test-webhook-secret"},
        )
        response = await adapter._handle_webhook(request)
        await _drain_background_tasks(adapter)
        assert response.status == 400
        assert adapter.handle_message.call_count == 0

    @pytest.mark.asyncio
    async def test_missing_from_number_skipped(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter.handle_message = AsyncMock()
        payload = {
            "is_outbound": False,
            "sendblue_number": "+15555550100",
            # no from_number
            "content": "hello",
        }
        request = _MockRequest(
            body=payload,
            headers={"sb-signing-secret": "test-webhook-secret"},
        )
        response = await adapter._handle_webhook(request)
        await _drain_background_tasks(adapter)
        # Item skipped at required-fields check, but batch still succeeds
        assert response.status == 200
        assert adapter.handle_message.call_count == 0

    @pytest.mark.asyncio
    async def test_missing_content_skipped(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter.handle_message = AsyncMock()
        payload = {
            "is_outbound": False,
            "sendblue_number": "+15555550100",
            "from_number": "+17766768883",
            # no content, no media_url — no text source at all
        }
        request = _MockRequest(
            body=payload,
            headers={"sb-signing-secret": "test-webhook-secret"},
        )
        response = await adapter._handle_webhook(request)
        await _drain_background_tasks(adapter)
        assert response.status == 200
        assert adapter.handle_message.call_count == 0


class TestSendblueMediaDownload:
    @pytest.mark.asyncio
    async def test_image_url_cached_with_correct_ext(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter.client = AsyncMock()
        adapter.client.get = AsyncMock(
            return_value=_MockHttpxResponse(content=b"fake-image-bytes")
        )
        cache_mock = Mock(return_value="/cache/foo.jpg")
        monkeypatch.setattr(
            "gateway.platforms.sendblue.cache_image_from_bytes",
            cache_mock,
        )
        local_path, mime_type = await adapter._download_and_cache_media(
            "https://cdn.sendblue.com/img/test.jpg"
        )
        assert local_path == "/cache/foo.jpg"
        assert mime_type == "image/jpeg"
        cache_mock.assert_called_once_with(b"fake-image-bytes", ".jpg")

    @pytest.mark.asyncio
    async def test_audio_url_warns_and_returns_none(self, monkeypatch, caplog):
        adapter = _make_adapter(monkeypatch)
        adapter.client = AsyncMock()  # client exists, but get won't be called
        with caplog.at_level("WARNING"):
            local_path, mime_type = await adapter._download_and_cache_media(
                "https://cdn.sendblue.com/audio/voice.caf"
            )
        assert local_path is None
        assert mime_type is None
        adapter.client.get.assert_not_called()  # extension check short-circuits
        assert any(
            "audio attachment received" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_download_failure_returns_none(self, monkeypatch, caplog):
        adapter = _make_adapter(monkeypatch)
        adapter.client = AsyncMock()
        adapter.client.get = AsyncMock(
            side_effect=httpx.RequestError(
                "connection refused",
                request=httpx.Request("GET", "http://test"),
            )
        )
        with caplog.at_level("WARNING"):
            local_path, mime_type = await adapter._download_and_cache_media(
                "https://cdn.sendblue.com/img/missing.jpg"
            )
        assert local_path is None
        assert mime_type is None
        assert any(
            "media download failed" in r.getMessage()
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_media_only_with_failure_dropped_in_handler(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter.handle_message = AsyncMock()
        # Shallow mock: skip the network and cache layers entirely
        adapter._download_and_cache_media = AsyncMock(return_value=(None, None))
        payload = {
            "is_outbound": False,
            "sendblue_number": "+15555550100",
            "from_number": "+17766768883",
            # no content
            "media_url": "https://cdn.sendblue.com/img/test.jpg",
        }
        request = _MockRequest(
            body=payload,
            headers={"sb-signing-secret": "test-webhook-secret"},
        )
        response = await adapter._handle_webhook(request)
        await _drain_background_tasks(adapter)
        assert response.status == 200  # batch succeeds, item silently dropped
        assert adapter.handle_message.call_count == 0


class TestSendblueOutboundSend:
    @pytest.mark.asyncio
    async def test_send_makes_correct_api_call(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._sendblue_api_post = AsyncMock(
            return_value=(200, '{"message_handle": "abc"}')
        )
        result = await adapter.send("+17766768883", "hello")
        assert result.success is True
        assert result.message_id == "abc"
        adapter._sendblue_api_post.assert_called_once_with(
            "send-message",
            {
                "number": "+17766768883",
                "from_number": "+15555550100",
                "content": "hello",
            },
        )

    @pytest.mark.asyncio
    async def test_truncates_content_over_max_length(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._sendblue_api_post = AsyncMock(
            return_value=(200, '{"message_handle": "abc"}')
        )
        # 20000 chars > 18996 MAX_MESSAGE_LENGTH — inherited truncate_message
        # should split into multiple chunks, each POSTed separately
        result = await adapter.send("+17766768883", "X" * 20000)
        assert result.success is True
        assert adapter._sendblue_api_post.call_count > 1

    @pytest.mark.asyncio
    async def test_empty_content_returns_failure(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._sendblue_api_post = AsyncMock()
        result = await adapter.send("+17766768883", "")
        assert result.success is False
        assert "non-empty" in result.error
        adapter._sendblue_api_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_network_error_returns_retryable_failure(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        # status=0 is the transport-error convention from _sendblue_api_post
        adapter._sendblue_api_post = AsyncMock(
            return_value=(0, "connection error")
        )
        result = await adapter.send("+17766768883", "hello")
        assert result.success is False
        assert result.retryable is True

    @pytest.mark.asyncio
    async def test_4xx_returns_non_retryable_failure(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._sendblue_api_post = AsyncMock(
            return_value=(400, '{"error": "bad request"}')
        )
        result = await adapter.send("+17766768883", "hello")
        assert result.success is False
        assert result.retryable is False


class TestSendblueSendImage:
    def test_public_image_url_truth_table(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        # Public HTTPS image URLs -> True
        assert adapter._is_public_image_url("https://cdn.example.com/img.jpg") is True
        assert adapter._is_public_image_url("https://cdn.example.com/img.png") is True
        assert adapter._is_public_image_url("https://cdn.example.com/img.JPG") is True
        assert adapter._is_public_image_url("https://cdn.example.com/img.heic") is True
        assert adapter._is_public_image_url("https://cdn.example.com/img.jpg?token=abc") is True

    def test_non_public_url_truth_table(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        # Non-HTTPS or no/wrong extension -> False
        assert adapter._is_public_image_url("http://cdn.example.com/img.jpg") is False
        assert adapter._is_public_image_url("file:///tmp/img.jpg") is False
        assert adapter._is_public_image_url("https://cdn.example.com/img.txt") is False
        assert adapter._is_public_image_url("https://cdn.example.com/noext") is False
        assert adapter._is_public_image_url("") is False
        assert adapter._is_public_image_url("not-a-url") is False

    @pytest.mark.asyncio
    async def test_public_image_sends_with_media_url(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._sendblue_api_post = AsyncMock(
            return_value=(200, '{"message_handle": "img-abc"}')
        )
        result = await adapter.send_image(
            "+17766768883",
            "https://cdn.example.com/img.jpg",
            caption=None,
        )
        assert result.success is True
        assert result.message_id == "img-abc"
        adapter._sendblue_api_post.assert_called_once_with(
            "send-message",
            {
                "number": "+17766768883",
                "from_number": "+15555550100",
                "media_url": "https://cdn.example.com/img.jpg",
            },
        )

    @pytest.mark.asyncio
    async def test_public_image_with_caption_includes_content(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._sendblue_api_post = AsyncMock(
            return_value=(200, '{"message_handle": "img-xyz"}')
        )
        result = await adapter.send_image(
            "+17766768883",
            "https://cdn.example.com/img.jpg",
            caption="look at this",
        )
        assert result.success is True
        adapter._sendblue_api_post.assert_called_once_with(
            "send-message",
            {
                "number": "+17766768883",
                "from_number": "+15555550100",
                "media_url": "https://cdn.example.com/img.jpg",
                "content": "look at this",
            },
        )

    @pytest.mark.asyncio
    async def test_non_public_url_falls_back_to_base_class(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._sendblue_api_post = AsyncMock()  # should not be called
        super_send_image = AsyncMock()
        monkeypatch.setattr(
            "gateway.platforms.base.BasePlatformAdapter.send_image",
            super_send_image,
        )
        await adapter.send_image(
            "+17766768883",
            "http://cdn.example.com/img.jpg",  # http, not https
            caption="hi",
        )
        super_send_image.assert_called_once()
        adapter._sendblue_api_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_4xx_returns_non_retryable_failure(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._sendblue_api_post = AsyncMock(
            return_value=(400, '{"error": "invalid media_url"}')
        )
        result = await adapter.send_image(
            "+17766768883",
            "https://cdn.example.com/img.jpg",
        )
        assert result.success is False
        assert result.retryable is False


class TestSendblueQuotaCommand:
    def test_format_zero_outbound(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        out = adapter._format_quota_response({"outbound": 0, "inbound": 5})
        assert out.startswith("📊 Sendblue (since 3am EST)")
        assert "↑ 0 sent / 200 daily cap" in out
        assert "[░░░░░░░░] 0%" in out
        assert "↓ 5 received" in out

    def test_format_at_cap(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        out = adapter._format_quota_response({"outbound": 200, "inbound": 0})
        assert "[████████] 100%" in out
        assert "↑ 200 sent / 200 daily cap" in out

    def test_format_custom_cap(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, sendblue_daily_cap=500)
        out = adapter._format_quota_response({"outbound": 250, "inbound": 0})
        assert "/ 500 daily cap" in out
        assert "50%" in out
        assert "[████░░░░]" in out

    @pytest.mark.asyncio
    async def test_fetch_usage_happy_path(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._sendblue_api_get = AsyncMock(side_effect=[
            (200, {"pagination": {"total": 42}}),
            (200, {"pagination": {"total": 17}}),
        ])
        result = await adapter._fetch_sendblue_usage()
        assert result["outbound"] == 42
        assert result["inbound"] == 17
        assert result["source"] == "Sendblue API"
        assert adapter._sendblue_api_get.call_count == 2

    @pytest.mark.asyncio
    async def test_fetch_usage_cache_hit(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._sendblue_api_get = AsyncMock(side_effect=[
            (200, {"pagination": {"total": 5}}),
            (200, {"pagination": {"total": 3}}),
        ])
        first = await adapter._fetch_sendblue_usage()
        second = await adapter._fetch_sendblue_usage()
        assert second["outbound"] == 5
        assert second["source"] == "cache"
        assert adapter._sendblue_api_get.call_count == 2

    @pytest.mark.asyncio
    async def test_fetch_usage_api_error_no_cache(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._sendblue_api_get = AsyncMock(return_value=(500, "server error"))
        result = await adapter._fetch_sendblue_usage()
        assert result["source"] == "error"
        assert "500" in (result.get("error") or "")
        assert result["outbound"] == 0

    @pytest.mark.asyncio
    async def test_quota_intercept_short_circuits_agent(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._fetch_sendblue_usage = AsyncMock(return_value={
            "outbound": 10, "inbound": 5, "day_key": "x",
            "source": "Sendblue API", "error": None,
        })
        adapter.send = AsyncMock()
        adapter.handle_message = AsyncMock()
        request = _MockRequest(
            body={
                "is_outbound": False,
                "sendblue_number": "+15555550100",
                "from_number": "+17766768883",
                "content": "/quota",
                "message_handle": "abc",
            },
            headers={"sb-signing-secret": "test-webhook-secret"},
        )
        response = await adapter._handle_webhook(request)
        assert response.status == 200
        await _drain_background_tasks(adapter)
        adapter._fetch_sendblue_usage.assert_called_once()
        adapter.send.assert_called_once()
        sent_content = adapter.send.call_args[0][1]
        assert "📊 Sendblue" in sent_content
        adapter.handle_message.assert_not_called()


class TestSendblueReadReceiptsAndTyping:
    @pytest.mark.asyncio
    async def test_mark_read_posts_correct_payload(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._sendblue_api_post = AsyncMock(return_value=(200, {}))
        ok = await adapter.mark_read("+17766768883")
        assert ok is True
        adapter._sendblue_api_post.assert_called_once_with(
            "mark-read",
            {"number": "+17766768883", "from_number": "+15555550100"},
            timeout=5.0,
        )

    @pytest.mark.asyncio
    async def test_mark_read_disabled_when_flag_false(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, send_read_receipts=False)
        adapter._sendblue_api_post = AsyncMock()
        ok = await adapter.mark_read("+17766768883")
        assert ok is False
        adapter._sendblue_api_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_mark_read_failure_returns_false(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._sendblue_api_post = AsyncMock(return_value=(400, "bad"))
        ok = await adapter.mark_read("+17766768883")
        assert ok is False

    @pytest.mark.asyncio
    async def test_send_typing_posts_correct_payload(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._sendblue_api_post = AsyncMock(return_value=(200, {}))
        result = await adapter.send_typing("+17766768883")
        assert result is None
        adapter._sendblue_api_post.assert_called_once_with(
            "send-typing-indicator",
            {"number": "+17766768883", "from_number": "+15555550100"},
            timeout=5.0,
        )

    @pytest.mark.asyncio
    async def test_webhook_dispatches_mark_read(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter.handle_message = AsyncMock()
        adapter.mark_read = AsyncMock(return_value=True)
        request = _MockRequest(
            body={
                "is_outbound": False,
                "sendblue_number": "+15555550100",
                "from_number": "+17766768883",
                "content": "hello",
            },
            headers={"sb-signing-secret": "test-webhook-secret"},
        )
        response = await adapter._handle_webhook(request)
        assert response.status == 200
        await _drain_background_tasks(adapter)
        adapter.mark_read.assert_called_once_with("+17766768883")


class TestSendblueSendStyle:
    def test_normalize_valid_style_lowercased(self, monkeypatch):
        from gateway.platforms.sendblue import _normalize_send_style
        assert _normalize_send_style("Confetti") == "confetti"
        assert _normalize_send_style("  GENTLE  ") == "gentle"

    def test_normalize_none_and_empty(self, monkeypatch):
        from gateway.platforms.sendblue import _normalize_send_style
        assert _normalize_send_style(None) is None
        assert _normalize_send_style("") is None
        assert _normalize_send_style("   ") is None

    def test_normalize_invalid_returns_none_and_warns(self, monkeypatch, caplog):
        from gateway.platforms.sendblue import _normalize_send_style
        with caplog.at_level("WARNING"):
            assert _normalize_send_style("nuclear") is None
        assert any("invalid send_style" in r.message for r in caplog.records)

    def test_default_style_from_env(self, monkeypatch):
        monkeypatch.setenv("SENDBLUE_DEFAULT_SEND_STYLE", "confetti")
        adapter = _make_adapter(monkeypatch)
        assert adapter.default_send_style == "confetti"

    def test_default_style_from_extra_overrides_env(self, monkeypatch):
        monkeypatch.setenv("SENDBLUE_DEFAULT_SEND_STYLE", "confetti")
        adapter = _make_adapter(monkeypatch, sendblue_default_send_style="slam")
        assert adapter.default_send_style == "slam"

    def test_default_style_invalid_env_is_dropped(self, monkeypatch, caplog):
        monkeypatch.setenv("SENDBLUE_DEFAULT_SEND_STYLE", "nuclear")
        with caplog.at_level("WARNING"):
            adapter = _make_adapter(monkeypatch)
        assert adapter.default_send_style is None

    @pytest.mark.asyncio
    async def test_send_includes_default_style(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, sendblue_default_send_style="confetti")
        adapter._sendblue_api_post = AsyncMock(
            return_value=(200, json.dumps({"message_handle": "h1"}))
        )
        await adapter.send("+17706768883", "hi")
        sent_payload = adapter._sendblue_api_post.call_args[0][1]
        assert sent_payload["send_style"] == "confetti"

    @pytest.mark.asyncio
    async def test_send_no_style_when_default_unset(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._sendblue_api_post = AsyncMock(
            return_value=(200, json.dumps({"message_handle": "h1"}))
        )
        await adapter.send("+17706768883", "hi")
        sent_payload = adapter._sendblue_api_post.call_args[0][1]
        assert "send_style" not in sent_payload

    @pytest.mark.asyncio
    async def test_send_metadata_overrides_default(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, sendblue_default_send_style="confetti")
        adapter._sendblue_api_post = AsyncMock(
            return_value=(200, json.dumps({"message_handle": "h1"}))
        )
        await adapter.send("+17706768883", "hi", metadata={"send_style": "slam"})
        sent_payload = adapter._sendblue_api_post.call_args[0][1]
        assert sent_payload["send_style"] == "slam"

    @pytest.mark.asyncio
    async def test_send_metadata_none_suppresses_default(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, sendblue_default_send_style="confetti")
        adapter._sendblue_api_post = AsyncMock(
            return_value=(200, json.dumps({"message_handle": "h1"}))
        )
        await adapter.send("+17706768883", "hi", metadata={"send_style": None})
        sent_payload = adapter._sendblue_api_post.call_args[0][1]
        assert "send_style" not in sent_payload

    @pytest.mark.asyncio
    async def test_send_invalid_metadata_style_dropped(self, monkeypatch, caplog):
        adapter = _make_adapter(monkeypatch, sendblue_default_send_style="confetti")
        adapter._sendblue_api_post = AsyncMock(
            return_value=(200, json.dumps({"message_handle": "h1"}))
        )
        with caplog.at_level("WARNING"):
            await adapter.send("+17706768883", "hi", metadata={"send_style": "nuclear"})
        sent_payload = adapter._sendblue_api_post.call_args[0][1]
        assert "send_style" not in sent_payload

    @pytest.mark.asyncio
    async def test_send_image_includes_style(self, monkeypatch):
        adapter = _make_adapter(monkeypatch, sendblue_default_send_style="balloons")
        adapter._sendblue_api_post = AsyncMock(
            return_value=(200, json.dumps({"message_handle": "h1"}))
        )
        await adapter.send_image(
            "+17706768883", "https://example.com/a.png", caption="cap"
        )
        sent_payload = adapter._sendblue_api_post.call_args[0][1]
        assert sent_payload["send_style"] == "balloons"
        assert sent_payload["media_url"] == "https://example.com/a.png"
        assert sent_payload["content"] == "cap"


class _MockUploadResponse:
    """Minimal httpx.Response surface for upload_file tests."""
    def __init__(self, status_code=200, body=None, raise_on_json=False):
        self.status_code = status_code
        self._body = body or {}
        self._raise_on_json = raise_on_json
        self.text = json.dumps(self._body) if not raise_on_json else "not json"

    def json(self):
        if self._raise_on_json:
            raise ValueError("not json")
        return self._body


class TestSendblueMediaUpload:
    @pytest.mark.asyncio
    async def test_upload_file_happy_path(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch)
        f = tmp_path / "pic.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        adapter.client = AsyncMock()
        adapter.client.post = AsyncMock(
            return_value=_MockUploadResponse(
                200, {"media_url": "https://cdn.sb/abc.png"}
            )
        )
        media_url = await adapter._upload_file_to_sendblue(str(f))
        assert media_url == "https://cdn.sb/abc.png"
        # Verify multipart was used (files= kwarg present)
        _, kwargs = adapter.client.post.call_args
        assert "files" in kwargs
        assert kwargs["headers"]["sb-api-key-id"] == "test-key-id"
        # Content-Type NOT injected (httpx sets multipart boundary itself)
        assert "Content-Type" not in kwargs["headers"]

    @pytest.mark.asyncio
    async def test_upload_file_missing_returns_none(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter.client = AsyncMock()
        result = await adapter._upload_file_to_sendblue("/no/such/file.png")
        assert result is None
        adapter.client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_upload_file_http_error_returns_none(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch)
        f = tmp_path / "pic.png"
        f.write_bytes(b"data")
        adapter.client = AsyncMock()
        adapter.client.post = AsyncMock(
            return_value=_MockUploadResponse(500, {"error": "boom"})
        )
        assert await adapter._upload_file_to_sendblue(str(f)) is None

    @pytest.mark.asyncio
    async def test_upload_file_no_media_url_returns_none(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch)
        f = tmp_path / "pic.png"
        f.write_bytes(b"data")
        adapter.client = AsyncMock()
        adapter.client.post = AsyncMock(
            return_value=_MockUploadResponse(200, {"status": "OK"})  # no media_url
        )
        assert await adapter._upload_file_to_sendblue(str(f)) is None

    @pytest.mark.asyncio
    async def test_upload_file_timeout_returns_none(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch)
        f = tmp_path / "pic.png"
        f.write_bytes(b"data")
        adapter.client = AsyncMock()
        adapter.client.post = AsyncMock(side_effect=httpx.TimeoutException("slow"))
        assert await adapter._upload_file_to_sendblue(str(f)) is None

    @pytest.mark.asyncio
    async def test_send_image_file_uploads_then_sends(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch)
        f = tmp_path / "pic.png"
        f.write_bytes(b"data")
        adapter._upload_file_to_sendblue = AsyncMock(
            return_value="https://cdn.sb/pic.png"
        )
        adapter._sendblue_api_post = AsyncMock(
            return_value=(200, json.dumps({"message_handle": "h1"}))
        )
        result = await adapter.send_image_file("+17706768883", str(f), caption="cap")
        assert result.success
        sent_payload = adapter._sendblue_api_post.call_args[0][1]
        assert sent_payload["media_url"] == "https://cdn.sb/pic.png"
        assert sent_payload["content"] == "cap"

    @pytest.mark.asyncio
    async def test_send_image_file_upload_failure_propagates(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch)
        f = tmp_path / "pic.png"
        f.write_bytes(b"data")
        adapter._upload_file_to_sendblue = AsyncMock(return_value=None)
        adapter._sendblue_api_post = AsyncMock()
        result = await adapter.send_image_file("+17706768883", str(f))
        assert not result.success
        assert result.retryable is True
        adapter._sendblue_api_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_voice_caf_no_warning(self, monkeypatch, tmp_path, caplog):
        adapter = _make_adapter(monkeypatch)
        f = tmp_path / "memo.caf"
        f.write_bytes(b"data")
        adapter._upload_file_to_sendblue = AsyncMock(
            return_value="https://cdn.sb/memo.caf"
        )
        adapter._sendblue_api_post = AsyncMock(
            return_value=(200, json.dumps({"message_handle": "h1"}))
        )
        with caplog.at_level("DEBUG"):
            result = await adapter.send_voice("+17706768883", str(f))
        assert result.success
        assert not any("not .caf" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_send_voice_non_caf_logs_debug(self, monkeypatch, tmp_path, caplog):
        adapter = _make_adapter(monkeypatch)
        f = tmp_path / "memo.mp3"
        f.write_bytes(b"data")
        adapter._upload_file_to_sendblue = AsyncMock(
            return_value="https://cdn.sb/memo.mp3"
        )
        adapter._sendblue_api_post = AsyncMock(
            return_value=(200, json.dumps({"message_handle": "h1"}))
        )
        with caplog.at_level("DEBUG"):
            await adapter.send_voice("+17706768883", str(f))
        assert any("not .caf" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_send_video_uploads_then_sends(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch)
        f = tmp_path / "clip.mp4"
        f.write_bytes(b"data")
        adapter._upload_file_to_sendblue = AsyncMock(
            return_value="https://cdn.sb/clip.mp4"
        )
        adapter._sendblue_api_post = AsyncMock(
            return_value=(200, json.dumps({"message_handle": "h1"}))
        )
        result = await adapter.send_video("+17706768883", str(f))
        assert result.success
        assert adapter._sendblue_api_post.call_args[0][1]["media_url"].endswith(".mp4")

    @pytest.mark.asyncio
    async def test_send_document_uploads_then_sends(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch)
        f = tmp_path / "report.pdf"
        f.write_bytes(b"data")
        adapter._upload_file_to_sendblue = AsyncMock(
            return_value="https://cdn.sb/report.pdf"
        )
        adapter._sendblue_api_post = AsyncMock(
            return_value=(200, json.dumps({"message_handle": "h1"}))
        )
        result = await adapter.send_document(
            "+17706768883", str(f), caption="see attached"
        )
        assert result.success
        payload = adapter._sendblue_api_post.call_args[0][1]
        assert payload["content"] == "see attached"
        assert payload["media_url"].endswith(".pdf")

    @pytest.mark.asyncio
    async def test_send_animation_delegates_to_send_image(self, monkeypatch):
        adapter = _make_adapter(monkeypatch)
        adapter._sendblue_api_post = AsyncMock(
            return_value=(200, json.dumps({"message_handle": "h1"}))
        )
        result = await adapter.send_animation(
            "+17706768883", "https://example.com/dance.gif", caption="lol"
        )
        assert result.success
        payload = adapter._sendblue_api_post.call_args[0][1]
        assert payload["media_url"] == "https://example.com/dance.gif"
        assert payload["content"] == "lol"

    @pytest.mark.asyncio
    async def test_media_send_inherits_default_send_style(self, monkeypatch, tmp_path):
        adapter = _make_adapter(monkeypatch, sendblue_default_send_style="confetti")
        f = tmp_path / "pic.png"
        f.write_bytes(b"data")
        adapter._upload_file_to_sendblue = AsyncMock(
            return_value="https://cdn.sb/pic.png"
        )
        adapter._sendblue_api_post = AsyncMock(
            return_value=(200, json.dumps({"message_handle": "h1"}))
        )
        await adapter.send_image_file("+17706768883", str(f))
        sent_payload = adapter._sendblue_api_post.call_args[0][1]
        assert sent_payload["send_style"] == "confetti"
