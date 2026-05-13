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
        self.webhook_public_url = (
            extra.get("webhook_public_url")
            or os.getenv("SENDBLUE_WEBHOOK_PUBLIC_URL", "")
        )
        self.webhook_secret = (
            extra.get("webhook_secret") or os.getenv("SENDBLUE_WEBHOOK_SECRET", "")
        )
        self.send_read_receipts = bool(extra.get("send_read_receipts", True))
        self.multi_bubble_split = bool(extra.get("multi_bubble_split", False))
        self.client: Optional[httpx.AsyncClient] = None
        self._runner = None

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    def _build_api_headers(self) -> Dict[str, str]:
        """Build the standard Sendblue API auth headers."""
        return {
            "sb-api-key-id": self.api_key_id,
            "sb-api-secret-key": self.api_secret,
            "Content-Type": "application/json",
        }

    async def _sendblue_api_post(
        self,
        endpoint: str,
        json_body: Dict[str, Any],
        timeout: float = 10.0,
    ) -> tuple:
        """POST to Sendblue API. Returns (status_code, response_text).

        On timeout: returns (0, "timeout") and logs WARNING.
        On other errors: returns (0, str(error)) and logs ERROR.
        Caller is responsible for status code interpretation.
        """
        if self.client is None:
            logger.error("[sendblue] _sendblue_api_post called before connect()")
            return 0, "client_not_initialized"
        url = f"{SENDBLUE_API_BASE}/{endpoint}"
        try:
            resp = await self.client.post(
                url,
                json=json_body,
                headers=self._build_api_headers(),
                timeout=timeout,
            )
            return resp.status_code, resp.text
        except httpx.TimeoutException:
            logger.warning(
                "[sendblue] API POST timeout: %s (%.0fs)", endpoint, timeout
            )
            return 0, "timeout"
        except Exception as e:
            logger.error("[sendblue] API POST error (%s): %s", endpoint, e)
            return 0, str(e)

    async def _sendblue_api_get(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 5.0,
    ) -> tuple:
        """GET from Sendblue API. Returns (status_code, response_dict_or_text).

        Response is JSON-parsed when possible; falls back to text on parse failure.
        Error handling matches _sendblue_api_post.
        """
        if self.client is None:
            logger.error("[sendblue] _sendblue_api_get called before connect()")
            return 0, "client_not_initialized"
        url = f"{SENDBLUE_API_BASE}/{endpoint}"
        try:
            resp = await self.client.get(
                url,
                params=params,
                headers=self._build_api_headers(),
                timeout=timeout,
            )
            try:
                return resp.status_code, resp.json()
            except Exception:
                return resp.status_code, resp.text
        except httpx.TimeoutException:
            logger.warning(
                "[sendblue] API GET timeout: %s (%.0fs)", endpoint, timeout
            )
            return 0, "timeout"
        except Exception as e:
            logger.error("[sendblue] API GET error (%s): %s", endpoint, e)
            return 0, str(e)

    def _verify_signature(self, header_value: str) -> bool:
        """Verify the sb-signing-secret header against the configured webhook_secret.

        Returns True if no secret is configured (signature verification disabled
        with operator warning at connect() time). Otherwise returns True only on
        exact string equality match.
        """
        if not self.webhook_secret:
            return True
        return header_value == self.webhook_secret

    async def _find_registered_webhook_urls(self) -> List[str]:
        """Fetch the list of currently-registered receive webhook URLs.

        Returns empty list on API failure (logged at WARNING in caller context).
        Caller is responsible for deciding what to do with the result.
        """
        if self.client is None:
            return []
        status, body = await self._sendblue_api_get("account/webhooks")
        if status != 200 or not isinstance(body, dict):
            return []
        webhooks = body.get("webhooks", {})
        if not isinstance(webhooks, dict):
            return []
        receive_list = webhooks.get("receive", [])
        if not isinstance(receive_list, list):
            return []
        urls = []
        for entry in receive_list:
            if isinstance(entry, dict) and isinstance(entry.get("url"), str):
                urls.append(entry["url"])
        return urls

    async def _register_webhook(self) -> bool:
        """Register self.webhook_public_url with Sendblue's API.

        Crash-resilient: if our URL is already in the receive list, skip the
        POST and return True. This handles restart-after-crash without
        duplicate registrations.

        Returns True on success or already-registered. Returns False on missing
        config, missing client, or API failure. A False return does NOT fail
        connect() — webhook server still runs locally, just won't receive
        traffic until the URL is manually registered or next connect retry.
        """
        if not self.webhook_public_url:
            logger.warning(
                "[sendblue] SENDBLUE_WEBHOOK_PUBLIC_URL not set — webhook registration skipped"
            )
            return False
        if self.client is None:
            logger.error("[sendblue] _register_webhook called before connect()")
            return False

        existing_urls = await self._find_registered_webhook_urls()
        if self.webhook_public_url in existing_urls:
            logger.info(
                "[sendblue] webhook already registered: %s", self.webhook_public_url
            )
            return True

        payload = {
            "webhooks": [
                {"url": self.webhook_public_url, "secret": self.webhook_secret}
            ],
            "type": "receive",
        }
        status, body = await self._sendblue_api_post("account/webhooks", payload)
        if 200 <= status < 300:
            logger.info(
                "[sendblue] webhook registered with Sendblue: %s",
                self.webhook_public_url,
            )
            return True
        logger.warning(
            "[sendblue] webhook registration failed (status %s): %s", status, body
        )
        return False

    async def _unregister_webhook(self) -> bool:
        """Unregister self.webhook_public_url from Sendblue's API.

        Inline DELETE call (no shared helper — only callsite in MVP).
        Returns True if the DELETE succeeded, False on missing config,
        missing client, or API failure. Failures are logged at DEBUG
        per architecture Section 6 (non-critical — webhook re-registration
        on next connect() handles cleanup).
        """
        if not self.webhook_public_url:
            return True  # nothing to do, no warning on cleanup path
        if self.client is None:
            return False

        url = f"{SENDBLUE_API_BASE}/account/webhooks"
        payload = {
            "webhooks": [self.webhook_public_url],
            "type": "receive",
        }
        try:
            resp = await self.client.request(
                "DELETE",
                url,
                json=payload,
                headers=self._build_api_headers(),
                timeout=5.0,
            )
            if 200 <= resp.status_code < 300:
                logger.info(
                    "[sendblue] webhook unregistered: %s", self.webhook_public_url
                )
                return True
            logger.debug(
                "[sendblue] webhook unregistration returned status %s: %s",
                resp.status_code,
                resp.text,
            )
            return False
        except Exception as exc:
            logger.debug(
                "[sendblue] webhook unregistration failed (non-critical): %s", exc
            )
            return False

    @staticmethod
    def _value(*candidates: Any) -> Optional[str]:
        """Return the first non-empty stripped string from candidates, or None.

        Used in _handle_webhook for field extraction with fallbacks
        (e.g. _value(item.get("content"), item.get("text"), item.get("body"))).
        Whitespace-only candidates are treated as empty.
        """
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    @staticmethod
    def _message_type_from_mime(mime_type: str) -> MessageType:
        """Map a MIME type string to the corresponding MessageType enum.

        image/* → PHOTO, audio/* → VOICE, video/* → VIDEO, else DOCUMENT.
        Matches the routing in bluebubbles.py:844-859. MVP only exercises
        the PHOTO branch (images-only); other branches are scaffolded for
        future audio/video/document support.
        """
        if mime_type.startswith("image/"):
            return MessageType.PHOTO
        if mime_type.startswith("audio/"):
            return MessageType.VOICE
        if mime_type.startswith("video/"):
            return MessageType.VIDEO
        return MessageType.DOCUMENT

    async def _handle_webhook(self, request):
        """Sendblue webhook handler — stub. Real implementation in step 13.

        Returns 501 Not Implemented so Sendblue treats this as a server-side
        failure rather than a permanent rejection. The handler exists as a
        placeholder so connect() step 4 can register it as the aiohttp route
        without raising AttributeError; the actual signature verification,
        payload parsing, and message dispatch land in the next step.
        """
        from aiohttp import web
        return web.Response(status=501, text="not implemented")

    # -- abstract method stubs (implemented in subsequent steps) --

    async def connect(self) -> bool:
        """Connect to Sendblue and start the webhook server.

        See architecture Section 3 for the seven-step sequence.
        """
        # Step 1: Preflight validation
        if not self.api_key_id or not self.api_secret:
            logger.error(
                "[sendblue] SENDBLUE_API_KEY_ID and SENDBLUE_API_SECRET are required"
            )
            return False
        if not self.webhook_secret:
            logger.warning(
                "[sendblue] SENDBLUE_WEBHOOK_SECRET not set — "
                "webhook signature verification disabled"
            )
        if not self.sendblue_number:
            logger.warning(
                "[sendblue] SENDBLUE_NUMBER not set — adapter will process "
                "ALL inbound messages (no number filter)"
            )

        # Step 2: HTTP client creation
        from aiohttp import web
        from gateway.platforms._http_client_limits import platform_httpx_limits
        self.client = httpx.AsyncClient(
            timeout=30.0, limits=platform_httpx_limits()
        )

        # Step 3: Connectivity check (also pre-fetches webhook list for
        # step 6's crash-resilience reuse via _register_webhook).
        try:
            status, body = await self._sendblue_api_get("account/webhooks")
            if status != 200:
                logger.error(
                    "[sendblue] cannot reach Sendblue API "
                    "(GET account/webhooks returned status %s): %s",
                    status,
                    body,
                )
                await self.client.aclose()
                self.client = None
                return False
            masked_key = (
                f"{self.api_key_id[:6]}…" if len(self.api_key_id) >= 6 else "***"
            )
            logger.info(
                "[sendblue] authenticated to Sendblue API as %s", masked_key
            )
        except Exception as exc:
            logger.error("[sendblue] cannot reach Sendblue API: %s", exc)
            if self.client:
                await self.client.aclose()
                self.client = None
            return False

        # Step 4: Webhook server startup
        app = web.Application()
        app.router.add_get("/health", lambda _: web.Response(text="ok"))
        app.router.add_post(self.webhook_path, self._handle_webhook)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.webhook_host, self.webhook_port)
        await site.start()
        logger.info(
            "[sendblue] webhook listening on http://%s:%s%s",
            self.webhook_host,
            self.webhook_port,
            self.webhook_path,
        )

        # Step 5: Mark connected
        self._mark_connected()

        # Step 6: Webhook URL registration (non-fatal on failure)
        await self._register_webhook()

        # Step 7: Return
        return True

    async def disconnect(self) -> None:
        """Disconnect from Sendblue and tear down the webhook server.

        See architecture Section 3 disconnect sequence.
        """
        # Step 1: Unregister webhook (non-critical, logged at DEBUG on failure)
        await self._unregister_webhook()

        # Step 2: Close HTTP client
        if self.client:
            await self.client.aclose()
            self.client = None

        # Step 3: Shutdown webhook server
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

        # Step 4: Mark disconnected
        self._mark_disconnected()

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        raise NotImplementedError("send() not yet implemented")

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "dm"}
