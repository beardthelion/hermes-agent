"""Sendblue iMessage platform adapter.

Uses Sendblue's cloud relay for outbound REST sends and inbound
webhooks. Provides iMessage access for non-Mac users (BlueBubbles
requires a macOS server; Sendblue is a hosted alternative).

MVP supports text messaging and inbound image caching.
send_image() outbound uses URL passthrough (Sendblue fetches from
public CDN). Audio/document attachments deferred.

Architecture pattern modeled on bluebubbles.py.
"""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_image_from_bytes,
    cache_audio_from_bytes,
    cache_document_from_bytes,
)
from gateway.platforms.helpers import strip_markdown

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_WEBHOOK_HOST = "127.0.0.1"
DEFAULT_WEBHOOK_PORT = 8665
DEFAULT_WEBHOOK_PATH = "/sendblue-gateway/receive"
MAX_TEXT_LENGTH = 18996
SIGNATURE_HEADER = "sb-signing-secret"
SENDBLUE_API_BASE = "https://api.sendblue.com/api"

# Log redaction patterns
_PHONE_RE = re.compile(r"\+?\d{7,15}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def _redact(text: str) -> str:
    """Redact phone numbers and emails from log output."""
    text = _PHONE_RE.sub("[REDACTED]", text)
    text = _EMAIL_RE.sub("[REDACTED]", text)
    return text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def check_sendblue_requirements() -> bool:
    try:
        import aiohttp  # noqa: F401
        import httpx  # noqa: F401
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class SendblueAdapter(BasePlatformAdapter):
    platform = Platform.SENDBLUE
    SUPPORTS_MESSAGE_EDITING = False
    MAX_MESSAGE_LENGTH = MAX_TEXT_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.SENDBLUE)
        extra = config.extra or {}
        self.api_key_id = (
            extra.get("api_key_id") or os.getenv("SENDBLUE_API_KEY_ID", "")
        )
        self.api_secret = (
            extra.get("api_secret") or os.getenv("SENDBLUE_API_SECRET", "")
        )
        self.sendblue_number = (
            extra.get("sendblue_number") or os.getenv("SENDBLUE_NUMBER", "")
        )
        self.webhook_host = (
            extra.get("webhook_host")
            or os.getenv("SENDBLUE_WEBHOOK_HOST", DEFAULT_WEBHOOK_HOST)
        )
        self.webhook_port = int(
            extra.get("webhook_port")
            or os.getenv("SENDBLUE_WEBHOOK_PORT", str(DEFAULT_WEBHOOK_PORT))
        )
        self.webhook_path = (
            extra.get("webhook_path")
            or os.getenv("SENDBLUE_WEBHOOK_PATH", DEFAULT_WEBHOOK_PATH)
        )
        if not str(self.webhook_path).startswith("/"):
            self.webhook_path = f"/{self.webhook_path}"
        self.webhook_secret = (
            extra.get("webhook_secret") or os.getenv("SENDBLUE_WEBHOOK_SECRET", "")
        )
        self.send_read_receipts = bool(extra.get("send_read_receipts", True))
        self.multi_bubble_split = bool(extra.get("multi_bubble_split", False))
        self.client: Optional[httpx.AsyncClient] = None
        self._runner = None

    # -- abstract method stubs (implemented in subsequent steps) --

    async def connect(self) -> bool:
        raise NotImplementedError("connect() not yet implemented")

    async def disconnect(self) -> None:
        raise NotImplementedError("disconnect() not yet implemented")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        raise NotImplementedError("send() not yet implemented")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        raise NotImplementedError("get_chat_info() not yet implemented")
