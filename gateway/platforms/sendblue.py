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
import hmac
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
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
MAX_WEBHOOK_BODY_BYTES = 1_048_576
SIGNATURE_HEADER = "sb-signing-secret"
SENDBLUE_API_BASE = "https://api.sendblue.com/api"

# iMessage send effects accepted by POST /api/send-message. Sendblue does
# not validate server-side — arbitrary strings are accepted with HTTP 202
# and silently dropped on the recipient side. This frozenset is the only
# validation gate, so keep it in sync with Sendblue's docs.
VALID_SEND_STYLES = frozenset({
    "slam", "loud", "gentle", "invisible", "echo", "spotlight",
    "balloons", "confetti", "love", "lasers", "fireworks",
    "shooting_star", "celebration",
})


def _normalize_send_style(style: Optional[str]) -> Optional[str]:
    """Return a lowercased valid send_style, or None.

    None/empty/whitespace → None (caller should drop the field).
    Unknown style → logs WARNING and returns None (graceful degrade —
    Sendblue would accept it silently otherwise).
    """
    if not style or not isinstance(style, str) or not style.strip():
        return None
    s = style.strip().lower()
    if s not in VALID_SEND_STYLES:
        logger.warning(
            "[sendblue] invalid send_style %r — dropping. Valid: %s",
            style, ", ".join(sorted(VALID_SEND_STYLES)),
        )
        return None
    return s

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

    _IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"})
    _AUDIO_EXTENSIONS = frozenset({".caf", ".m4a", ".mp3", ".wav", ".aac", ".ogg", ".flac"})
    _VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".webm"})

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
        self.daily_cap = int(
            extra.get("sendblue_daily_cap") or os.getenv("SENDBLUE_DAILY_CAP", "200")
        )
        self.default_send_style = _normalize_send_style(
            extra.get("sendblue_default_send_style")
            or os.getenv("SENDBLUE_DEFAULT_SEND_STYLE", "")
        )
        self._quota_cache: Dict[str, Dict[str, Any]] = {}
        self.client: Optional[httpx.AsyncClient] = None
        self._runner = None

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_group_chat_id(chat_id: str) -> bool:
        """Sendblue group IDs are UUID-like strings; DM chat IDs are
        E.164 phone numbers starting with +. The phone-number prefix
        is the only reliable distinguisher in webhook payloads.
        """
        return bool(chat_id) and not chat_id.startswith("+")

    def _resolve_send_style(
        self, metadata: Optional[Dict[str, Any]]
    ) -> Optional[str]:
        """Pick the send_style for this call.

        Precedence: per-call metadata["send_style"] > self.default_send_style.
        Explicit metadata["send_style"]=None or "" forces no style even
        when a default is configured (caller wins).
        """
        if metadata is not None and "send_style" in metadata:
            return _normalize_send_style(metadata.get("send_style"))
        return self.default_send_style

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
        return hmac.compare_digest(header_value, self.webhook_secret)

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

    # ------------------------------------------------------------------
    # Quota / usage tracking (/quota slash command)
    # ------------------------------------------------------------------

    _QUOTA_CACHE_TTL_SECONDS = 60

    @staticmethod
    def _get_sendblue_day_key() -> str:
        """Sendblue daily quota resets at 3am America/New_York.

        Returns the UTC ISO-8601 timestamp (Z suffix) of the current
        day's window start. Computed in America/New_York to respect
        the 3am EST/EDT cutoff (and DST), then converted to UTC so the
        value passed as `created_at_gte` is unambiguous regardless of
        how Sendblue parses ISO offsets.
        """
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]
        tz = ZoneInfo("America/New_York")
        now = datetime.now(tz)
        cutoff = now.replace(hour=3, minute=0, second=0, microsecond=0)
        if now < cutoff:
            cutoff -= timedelta(days=1)
        utc_cutoff = cutoff.astimezone(timezone.utc)
        return utc_cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    async def _fetch_sendblue_usage(self) -> Dict[str, Any]:
        """Count today's outbound + inbound messages via Sendblue API.

        Cached 60s keyed on the 3am EST day boundary. On API failure
        returns stale cache if available, else an error-marker dict.
        """
        day_key = self._get_sendblue_day_key()
        cached = self._quota_cache.get(day_key)
        if cached and (time.time() - cached["cached_at"]) < self._QUOTA_CACHE_TTL_SECONDS:
            return {**cached["data"], "source": "cache"}

        try:
            status, data = await self._sendblue_api_get(
                "v2/messages",
                {"is_outbound": "true", "created_at_gte": day_key, "limit": 1},
                timeout=3.0,
            )
            if status != 200:
                raise RuntimeError(f"HTTP {status}: {str(data)[:200]}")
            outbound = data.get("pagination", {}).get("total", 0)

            status, data = await self._sendblue_api_get(
                "v2/messages",
                {"is_outbound": "false", "created_at_gte": day_key, "limit": 1},
                timeout=3.0,
            )
            if status != 200:
                raise RuntimeError(f"HTTP {status}: {str(data)[:200]}")
            inbound = data.get("pagination", {}).get("total", 0)

            result = {
                "outbound": outbound,
                "inbound": inbound,
                "day_key": day_key,
                "source": "Sendblue API",
                "error": None,
            }
            self._quota_cache[day_key] = {"data": result, "cached_at": time.time()}
            return result
        except Exception as e:
            err = str(e)[:200]
            logger.warning("[sendblue] usage fetch failed: %s", err)
            stale = self._quota_cache.get(day_key)
            if stale:
                return {**stale["data"], "source": "stale cache", "error": err}
            return {
                "outbound": 0,
                "inbound": 0,
                "day_key": day_key,
                "source": "error",
                "error": err,
            }

    def _format_quota_response(self, usage: Dict[str, Any]) -> str:
        """Build /quota SMS reply from _fetch_sendblue_usage() output."""
        outbound = usage.get("outbound", 0)
        inbound = usage.get("inbound", 0)
        cap = self.daily_cap
        pct = min(100, int((outbound / cap) * 100)) if cap > 0 else 0
        bar_width = 8
        filled = max(0, min(bar_width, int(round((pct / 100) * bar_width))))
        bar = "█" * filled + "░" * (bar_width - filled)
        return "\n".join([
            "📊 Sendblue (since 3am EST)",
            f"↑ {outbound} sent / {cap} daily cap  [{bar}] {pct}%",
            f"↓ {inbound} received",
        ])

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

    async def _download_and_cache_media(
        self, media_url: str
    ) -> tuple[Optional[str], Optional[str]]:
        """Download a Sendblue CDN media URL and cache it locally.

        Returns (local_path, mime_type) on success, (None, None) on failure.
        MIME type is inferred from the URL extension (Sendblue doesn't
        provide Content-Type in the webhook payload — extension is the
        only signal).

        Routes by extension category:
          - image (jpg/png/gif/webp/heic/heif) → cache_image_from_bytes
          - audio (caf/m4a/mp3/wav/aac/ogg/flac) → cache_audio_from_bytes
          - video (mp4/mov/etc.) → cache_document_from_bytes
            (gateway treats videos as documents downstream)
          - everything else → cache_document_from_bytes

        Mirrors bluebubbles._download_attachment routing.
        """
        if not self.client:
            return None, None
        if not media_url:
            return None, None

        # Extract extension from URL (strip query string, last path segment).
        path = media_url.split("?")[0]
        stem = path.rsplit("/", 1)[-1] if "/" in path else path
        ext = "." + stem.rsplit(".", 1)[-1].lower() if "." in stem else ""

        try:
            resp = await self.client.get(
                media_url, timeout=60.0, follow_redirects=True
            )
            resp.raise_for_status()
            data = resp.content
        except Exception as exc:
            logger.warning(
                "[sendblue] media download failed for %s: %s",
                _redact(media_url), exc,
            )
            return None, None

        try:
            if ext in self._IMAGE_EXTENSIONS:
                local_path = cache_image_from_bytes(data, ext)
            elif ext in self._AUDIO_EXTENSIONS:
                local_path = cache_audio_from_bytes(data, ext)
            else:
                # Videos, documents, and unknown extensions all go to
                # cache_document_from_bytes — matches BB at bluebubbles.py:731.
                # Use the URL's last path segment as the filename so the
                # cached doc keeps a recognizable name.
                filename = stem or f"file_{uuid.uuid4().hex[:8]}"
                local_path = cache_document_from_bytes(data, filename)
        except ValueError as exc:
            logger.warning(
                "[sendblue] media at %s failed validation: %s",
                _redact(media_url), exc,
            )
            return None, None

        mime_type = self._ext_to_mime(ext)
        return local_path, mime_type

    @staticmethod
    def _ext_to_mime(ext: str) -> str:
        """Map a file extension to a canonical MIME type.

        Covers image / audio / video extensions used by Sendblue
        webhooks. Unknown extensions return application/octet-stream
        so MessageType still routes to DOCUMENT via
        _message_type_from_mime.
        """
        ext = ext.lower()
        # Image
        if ext in (".jpg", ".jpeg"):
            return "image/jpeg"
        if ext == ".png":
            return "image/png"
        if ext == ".gif":
            return "image/gif"
        if ext == ".webp":
            return "image/webp"
        if ext == ".heic":
            return "image/heic"
        if ext == ".heif":
            return "image/heif"
        # Audio
        if ext == ".caf":
            return "audio/x-caf"
        if ext == ".m4a":
            return "audio/mp4"
        if ext == ".mp3":
            return "audio/mpeg"
        if ext == ".wav":
            return "audio/wav"
        if ext == ".aac":
            return "audio/aac"
        if ext == ".ogg":
            return "audio/ogg"
        if ext == ".flac":
            return "audio/flac"
        # Video
        if ext == ".mp4":
            return "video/mp4"
        if ext == ".mov":
            return "video/quicktime"
        if ext == ".m4v":
            return "video/x-m4v"
        if ext == ".3gp":
            return "video/3gpp"
        if ext == ".avi":
            return "video/x-msvideo"
        if ext == ".mkv":
            return "video/x-matroska"
        if ext == ".webm":
            return "video/webm"
        return "application/octet-stream"

    async def _handle_webhook(self, request):
        """Handle inbound Sendblue webhook POST.

        Verifies the sb-signing-secret header, parses the JSON body
        (accepting either a single message object or a list of them),
        and dispatches each inbound message to the agent via
        handle_message(). Outbound echoes and messages for other
        sendblue_numbers are filtered out silently. Returns 200 "ok"
        on any successfully processed batch, 401/400 on auth/parse
        failures.

        Architecture: Section 4. Mirrors bluebubbles.py:768-936 with
        Sendblue-specific signature model, flat payload shape, and
        DM-only assumption.
        """
        from aiohttp import web

        # -- STEP 0: Body size cap --
        # Sendblue payloads are small JSON (media is referenced by URL).
        # Reject oversized bodies before crypto so a leaked secret can't
        # turn the webhook into a DoS surface.
        if (request.content_length or 0) > MAX_WEBHOOK_BODY_BYTES:
            return web.json_response({"error": "payload too large"}, status=413)

        # -- STEP 1: Signature verification --
        secret = request.headers.get(SIGNATURE_HEADER, "")
        if not self._verify_signature(secret):
            logger.warning(
                "[sendblue] signature verification failed from %s",
                request.remote,
            )
            return web.json_response({"error": "unauthorized"}, status=401)

        # -- STEP 2: Body parsing --
        try:
            body = await request.json()
        except json.JSONDecodeError as exc:
            logger.error("[sendblue] webhook parse error: %s", exc)
            return web.json_response({"error": "invalid payload"}, status=400)

        items = body if isinstance(body, list) else [body]

        # -- STEP 3: Per-message loop --
        for item in items:
            if not isinstance(item, dict):
                logger.debug("[sendblue] skipping non-dict item: %r", item)
                continue

            # -- STEP 3a: Skip outbound echoes --
            if item.get("is_outbound"):
                continue

            # -- STEP 3b: Routing filter -- is this for our number? --
            inbound_line = self._value(
                item.get("sendblue_number"),
                item.get("to_number"),
            )
            if self.sendblue_number and inbound_line != self.sendblue_number:
                continue

            # -- STEP 3c: Allowed-number check is handled at gateway level --
            # (gateway runner's _is_user_authorized() runs before dispatch)

            # -- STEP 3d: Extract fields --
            text = self._value(
                item.get("content"),
                item.get("text"),
                item.get("body"),
            ) or ""
            from_number = item.get("from_number", "")
            msg_handle = item.get("message_handle", "")
            media_url = (item.get("media_url") or "").strip() or None
            group_id = (item.get("group_id") or "").strip()
            group_display_name = (item.get("group_display_name") or "").strip()
            is_group = bool(group_id)
            # In a group, replies route to the group_id, not the sender's
            # number. In a DM, both are the same effectively (chat_id is
            # the other party's phone).
            chat_id = group_id if is_group else from_number

            # -- STEP 3e: Media handling --
            media_urls: List[str] = []
            media_types: List[str] = []
            msg_type = MessageType.TEXT
            if media_url:
                cached_path, mime_type = await self._download_and_cache_media(
                    media_url
                )
                if cached_path:
                    media_urls.append(cached_path)
                    media_types.append(mime_type)
                    msg_type = self._message_type_from_mime(mime_type)
                else:
                    logger.warning(
                        "[sendblue] media download failed for %s", media_url
                    )
                    # Continue with text-only -- don't fail the whole message
            if not text and media_urls:
                text = "(attachment)"
            # Media-only message where all downloads fail falls through to
            # Step 3f and is silently dropped (no text, nothing to dispatch).

            # -- STEP 3f: Required fields check --
            if not from_number or not text or not chat_id:
                logger.debug(
                    "[sendblue] missing required fields -- "
                    "from_number=%r, has_text=%s, chat_id=%r",
                    from_number, bool(text), chat_id,
                )
                continue

            # -- STEP 3f.5: Platform-native slash command intercept --
            if text.strip().lower() == "/quota":
                async def _send_quota_reply(target=chat_id):
                    try:
                        usage = await self._fetch_sendblue_usage()
                        reply = self._format_quota_response(usage)
                    except Exception as exc:
                        logger.exception(
                            "[sendblue] /quota handler failed: %s", exc
                        )
                        reply = "Couldn't fetch Sendblue usage — try again?"
                    await self.send(target, reply)
                task = asyncio.create_task(_send_quota_reply())
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
                continue

            # -- STEP 3g: Build MessageEvent --
            source = self.build_source(
                chat_id=chat_id,
                chat_name=group_display_name or chat_id,
                chat_type="group" if is_group else "dm",
                user_id=from_number,
                user_name=from_number,
            )
            event = MessageEvent(
                text=text,
                message_type=msg_type,
                source=source,
                raw_message=item,
                message_id=msg_handle,
                media_urls=media_urls,
                media_types=media_types,
            )

            # -- STEP 3h: Dispatch to agent --
            task = asyncio.create_task(self.handle_message(event))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

            # -- STEP 3i: Fire-and-forget read receipt --
            # Sendblue's mark-read API targets a DM by the other party's
            # number. There's no documented per-group mark-read, so skip
            # for group messages. Track the task so adapter shutdown can
            # await it cleanly instead of GC-cancelling mid-flight.
            if self.send_read_receipts and not is_group:
                read_task = asyncio.create_task(self.mark_read(from_number))
                self._background_tasks.add(read_task)
                read_task.add_done_callback(self._background_tasks.discard)

        # -- STEP 4: Return --
        return web.Response(text="ok")

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

    def format_message(self, content: str) -> str:
        return strip_markdown(content)

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an outbound text message via POST /api/send-message.

        Strips markdown via format_message(). If multi_bubble_split is
        enabled, splits on paragraph breaks (\\n\\s*\\n) first. Any
        chunk exceeding MAX_MESSAGE_LENGTH is further split via the
        inherited truncate_message(). Each chunk is POSTed as its own
        Sendblue API call; the message_id of the last successful chunk
        is returned as the SendResult.message_id.

        Architecture: Section 5 H1-H9. Mirrors bluebubbles.py:404-456
        with Sendblue-specific payload shape and API helper contract.

        reply_to and metadata parameters are accepted for interface
        compatibility but currently ignored -- Sendblue MVP does not
        implement message threading.
        """
        if reply_to is not None:
            logger.debug(
                "[sendblue] send() ignoring reply_to=%r "
                "(not implemented in MVP)",
                reply_to,
            )

        send_style = self._resolve_send_style(metadata)

        text = self.format_message(content)
        if not text:
            return SendResult(
                success=False,
                error="Sendblue send requires non-empty text",
            )

        # Determine chunks: paragraph-split if multi-bubble enabled,
        # otherwise single chunk. Either way, oversized chunks get
        # further split by the inherited truncate_message().
        if self.multi_bubble_split:
            paragraphs = [
                p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()
            ]
        else:
            paragraphs = [text]

        chunks: List[str] = []
        for para in paragraphs:
            if len(para) <= self.MAX_MESSAGE_LENGTH:
                chunks.append(para)
            else:
                chunks.extend(
                    self.truncate_message(
                        para, max_length=self.MAX_MESSAGE_LENGTH
                    )
                )

        if not chunks:
            return SendResult(
                success=False,
                error="Sendblue send requires non-empty text (all chunks empty after split)",
            )

        is_group = self._is_group_chat_id(chat_id)
        endpoint = "send-group-message" if is_group else "send-message"

        last = SendResult(success=True)
        for chunk in chunks:
            payload: Dict[str, Any] = {
                "from_number": self.sendblue_number,
                "content": chunk,
            }
            if is_group:
                payload["group_id"] = chat_id
            else:
                payload["number"] = chat_id
            if send_style:
                payload["send_style"] = send_style
            status, body = await self._sendblue_api_post(endpoint, payload)
            if not (200 <= status < 300):
                retryable = (status == 0 or status >= 500)
                logger.error(
                    "[sendblue] send failed status=%d retryable=%s body=%s",
                    status,
                    retryable,
                    body[:200],
                )
                return SendResult(
                    success=False,
                    error=f"Sendblue API returned {status}: {body[:200]}",
                    retryable=retryable,
                )
            try:
                parsed = json.loads(body) if body else {}
            except json.JSONDecodeError:
                parsed = {}
            msg_id = parsed.get("message_handle") or "ok"
            last = SendResult(
                success=True,
                message_id=str(msg_id),
                raw_response=parsed,
            )

        return last

    @staticmethod
    def _is_public_image_url(url: str) -> bool:
        """Return True if the URL is a public HTTPS image URL.

        Sendblue's media_url field requires:
          - HTTPS scheme
          - file extension at URL end (.jpg, .png, etc.)
          - publicly accessible (no signed URLs)

        We can verify the first two cheaply; the third is detected
        only at API call time (Sendblue fetches the URL).
        """
        if not url or not url.startswith("https://"):
            return False
        path = url.split("?")[0]
        stem = path.rsplit("/", 1)[-1] if "/" in path else path
        ext = "." + stem.rsplit(".", 1)[-1].lower() if "." in stem else ""
        return ext in SendblueAdapter._IMAGE_EXTENSIONS

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image natively via POST /api/send-message with media_url.

        Sendblue fetches the public CDN URL directly -- no upload step
        on our side. content and media_url are sent in a single payload.

        Requirements (Sendblue API):
          - image_url must be publicly accessible HTTPS
          - URL must end with proper file extension (.jpg, .png, etc.)
          - media_url does NOT support signed URLs (use media upload
            endpoint for those, deferred post-MVP)

        Falls back to base class URL-as-text via send() if the URL
        doesn't look like a public image URL. Permissive fallback --
        better to send a clickable link than to fail.

        reply_to and metadata accepted for interface compatibility but
        ignored (Sendblue MVP doesn't implement threading).
        """
        if not self._is_public_image_url(image_url):
            logger.debug(
                "[sendblue] send_image: URL not a public HTTPS image, "
                "falling back to base class text behavior: %s",
                image_url[:120],
            )
            return await super().send_image(
                chat_id, image_url, caption, reply_to, metadata
            )

        if reply_to is not None:
            logger.debug(
                "[sendblue] send_image() ignoring reply_to=%r "
                "(not implemented in MVP)",
                reply_to,
            )

        return await self._send_with_media_url(
            chat_id, image_url, caption, metadata,
        )

    async def _send_with_media_url(
        self,
        chat_id: str,
        media_url: str,
        caption: Optional[str],
        metadata: Optional[Dict[str, Any]],
    ) -> SendResult:
        """POST /api/send-message with a media_url attachment.

        Shared body of send_image (URL passthrough) and the upload-then-
        send media methods (send_image_file, send_voice, etc.). Strips
        markdown from caption and applies send_style precedence.
        """
        send_style = self._resolve_send_style(metadata)
        caption_text = self.format_message(caption) if caption else ""

        is_group = self._is_group_chat_id(chat_id)
        endpoint = "send-group-message" if is_group else "send-message"
        payload: Dict[str, Any] = {
            "from_number": self.sendblue_number,
            "media_url": media_url,
        }
        if is_group:
            payload["group_id"] = chat_id
        else:
            payload["number"] = chat_id
        if caption_text:
            payload["content"] = caption_text
        if send_style:
            payload["send_style"] = send_style

        status, body = await self._sendblue_api_post(endpoint, payload)
        if not (200 <= status < 300):
            retryable = (status == 0 or status >= 500)
            logger.error(
                "[sendblue] media send failed status=%d retryable=%s body=%s",
                status, retryable, body[:200],
            )
            return SendResult(
                success=False,
                error=f"Sendblue API returned {status}: {body[:200]}",
                retryable=retryable,
            )
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            parsed = {}
        msg_id = parsed.get("message_handle") or "ok"
        return SendResult(
            success=True, message_id=str(msg_id), raw_response=parsed,
        )

    async def _upload_file_to_sendblue(
        self, file_path: str
    ) -> Optional[str]:
        """Upload a local file to Sendblue's CDN.

        POST /api/upload-file (multipart). Returns the media_url on
        success, None on failure. The returned URL is then passed as
        media_url to /api/send-message.

        100 MB limit per Sendblue API. Filename + extension are
        preserved on the CDN — pass a .caf file to get native iMessage
        voice-memo rendering on the recipient device.
        """
        if self.client is None:
            logger.error("[sendblue] _upload_file_to_sendblue called before connect()")
            return None
        if not os.path.isfile(file_path):
            logger.error("[sendblue] upload: file not found: %s", file_path)
            return None

        url = f"{SENDBLUE_API_BASE}/upload-file"
        fname = os.path.basename(file_path)
        try:
            with open(file_path, "rb") as f:
                # httpx multipart: pass files as field-name -> (filename, fileobj)
                files = {"file": (fname, f, "application/octet-stream")}
                # Don't include Content-Type from _build_api_headers — httpx
                # sets multipart boundary itself.
                headers = {
                    "sb-api-key-id": self.api_key_id,
                    "sb-api-secret-key": self.api_secret,
                }
                resp = await self.client.post(
                    url, files=files, headers=headers, timeout=120.0,
                )
        except httpx.TimeoutException:
            logger.warning("[sendblue] upload timeout for %s", fname)
            return None
        except Exception as exc:
            logger.error("[sendblue] upload error for %s: %s", fname, exc)
            return None

        if not (200 <= resp.status_code < 300):
            logger.error(
                "[sendblue] upload returned status %d: %s",
                resp.status_code, resp.text[:200],
            )
            return None
        try:
            parsed = resp.json()
        except Exception:
            logger.error(
                "[sendblue] upload response not JSON: %s", resp.text[:200],
            )
            return None
        media_url = parsed.get("media_url")
        if not media_url:
            logger.error(
                "[sendblue] upload response missing media_url: %s",
                str(parsed)[:200],
            )
            return None
        return media_url

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local image file via Sendblue CDN upload + send-message."""
        media_url = await self._upload_file_to_sendblue(image_path)
        if not media_url:
            return SendResult(
                success=False,
                error=f"Sendblue upload failed for {image_path}",
                retryable=True,
            )
        return await self._send_with_media_url(
            chat_id, media_url, caption, kwargs.get("metadata"),
        )

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send an audio file as an iMessage attachment.

        For native voice-memo rendering on the recipient device, the
        file MUST have a .caf extension (Sendblue uses the extension
        to decide voice-bubble vs generic-attachment rendering).
        Non-.caf audio is uploaded as a generic attachment with a
        DEBUG log.
        """
        ext = os.path.splitext(audio_path)[1].lower()
        if ext != ".caf":
            logger.debug(
                "[sendblue] send_voice: %s is not .caf — will render as "
                "generic attachment, not voice memo",
                ext or "(no ext)",
            )
        media_url = await self._upload_file_to_sendblue(audio_path)
        if not media_url:
            return SendResult(
                success=False,
                error=f"Sendblue upload failed for {audio_path}",
                retryable=True,
            )
        return await self._send_with_media_url(
            chat_id, media_url, caption, kwargs.get("metadata"),
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send a local video file via Sendblue CDN upload + send-message."""
        media_url = await self._upload_file_to_sendblue(video_path)
        if not media_url:
            return SendResult(
                success=False,
                error=f"Sendblue upload failed for {video_path}",
                retryable=True,
            )
        return await self._send_with_media_url(
            chat_id, media_url, caption, kwargs.get("metadata"),
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        **kwargs,
    ) -> SendResult:
        """Send any file as an iMessage attachment.

        file_name is accepted for interface parity with BlueBubbles but
        ignored — Sendblue's upload uses the on-disk filename.
        """
        media_url = await self._upload_file_to_sendblue(file_path)
        if not media_url:
            return SendResult(
                success=False,
                error=f"Sendblue upload failed for {file_path}",
                retryable=True,
            )
        return await self._send_with_media_url(
            chat_id, media_url, caption, kwargs.get("metadata"),
        )

    async def send_animation(
        self,
        chat_id: str,
        animation_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an animated image (GIF). Mirrors BlueBubbles' behavior of
        delegating to send_image — Sendblue treats .gif identically to
        static images on the CDN.
        """
        return await self.send_image(
            chat_id, animation_url, caption, reply_to, metadata,
        )

    async def mark_read(self, chat_id: str) -> bool:
        """Send a read receipt for a received iMessage.

        POST /api/mark-read. Sendblue accepts 200 or 202 as success.
        Gated by self.send_read_receipts (extra.send_read_receipts,
        default True).
        """
        if not self.send_read_receipts:
            return False
        if self._is_group_chat_id(chat_id):
            # Sendblue's mark-read API targets a DM by the other party's
            # number. No documented per-group equivalent; silently skip.
            return False
        status, body = await self._sendblue_api_post(
            "mark-read",
            {"number": chat_id, "from_number": self.sendblue_number},
            timeout=5.0,
        )
        if status in (200, 202):
            return True
        logger.warning(
            "[sendblue] mark_read failed (%d): %s", status, str(body)[:200]
        )
        return False

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Show a typing indicator on the recipient's device.

        POST /api/send-typing-indicator. Called by the gateway's
        auto-typing keepalive loop while the agent is composing a
        reply. Sendblue typing indicators are short-lived; the
        keepalive loop refreshes them automatically. No stop_typing
        override needed.
        """
        if self._is_group_chat_id(chat_id):
            # Sendblue's typing-indicator API targets a DM; no
            # documented per-group equivalent.
            return
        status, _ = await self._sendblue_api_post(
            "send-typing-indicator",
            {"number": chat_id, "from_number": self.sendblue_number},
            timeout=5.0,
        )
        if status not in (200, 202):
            logger.debug("[sendblue] send_typing returned %d", status)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {
            "name": chat_id,
            "type": "group" if self._is_group_chat_id(chat_id) else "dm",
        }
