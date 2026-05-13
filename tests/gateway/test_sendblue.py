"""Tests for the Sendblue iMessage gateway adapter."""
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
        adapter = _make_adapter(monkeypatch, webhook_secret="")
        assert adapter._verify_signature("whatever") is True
