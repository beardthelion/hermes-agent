"""Slash command subsystem for APIServerAdapter.

Extracted from gateway/platforms/api_server.py so the slash dispatcher
and its handlers live in a separate file that doesn't collide with
upstream changes to api_server.py.

The mixin expects its host class to provide:
    - self._ensure_session_db() -> Optional[SessionDB]
    - self._model_name: str
    - self.gateway_runner (for quick_commands config access)

Usage:
    from gateway.platforms.api_server_slash import SlashCommandMixin

    class APIServerAdapter(SlashCommandMixin, BasePlatformAdapter):
        ...
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid as _uuid
from datetime import datetime as _dt
from typing import Optional

logger = logging.getLogger(__name__)


# Slash commands supported over the API server (subset of gateway commands).
# Commands NOT in this set fall through to the LLM as normal user messages.
_API_SERVER_SLASH_COMMANDS: frozenset[str] = frozenset({
    "help",
    "new",
    "reset",
    "title",
    "status",
    "usage",
    "retry",
    "undo",
    "profile",
    "branch",
    "resume",
})


class SlashCommandMixin:
    """Slash command dispatch for APIServerAdapter.

    See module docstring for the host-class contract.
    """

    # ------------------------------------------------------------------
    # Slash command dispatch (API server subset)
    # ------------------------------------------------------------------

    async def _try_slash_command(
        self, user_message: str, session_id: str, body: dict
    ) -> Optional[dict]:
        """Try to handle *user_message* as a slash command.

        Returns:
            ``{"handled": True, "response": str}`` — return as chat completion
            ``{"handled": True, "rewrite_message": str}`` — replace message, fall through to agent
            ``None`` — not a slash command, normal processing
        """
        if not isinstance(user_message, str) or not user_message.startswith("/"):
            return None

        from gateway.platforms.base import MessageEvent

        # Reuse the same parser the gateway uses — /title my session
        # tokenizes identically over SMS-via-API as it does over Telegram.
        event = MessageEvent(text=user_message)
        command = event.get_command()
        if not command:
            return None  # "/ " or "/path/to/file" — not a command

        args = event.get_command_args().strip()

        # Only intercept commands we explicitly support.  Telegram-style
        # commands (/personality, /voice, /model, …) fall through to the
        # LLM as normal user messages.
        if command not in _API_SERVER_SLASH_COMMANDS:
            # ── user-defined quick commands (bypass LLM) ────────────────
            # Check the gateway config's quick_commands section before
            # falling through.  Supports 'exec' type only (alias expansion
            # is handled by the gateway's main message pipeline).
            qcmds = self._get_quick_commands()
            if command in qcmds:
                qc = qcmds[command]
                if qc.get("type") == "exec":
                    result = await self._run_quick_command(command, qc, args)
                    if result is not None:
                        return result
            return None

        if command in ("help",):
            return {"handled": True, "response": self._slash_help()}

        if command in ("new", "reset"):
            return {"handled": True, "response": self._slash_reset(session_id)}

        if command == "title":
            return {"handled": True, "response": self._slash_title(session_id, args)}

        if command == "status":
            return {"handled": True, "response": self._slash_status(session_id, body)}

        if command == "usage":
            return {"handled": True, "response": self._slash_usage(session_id)}

        if command == "undo":
            return {"handled": True, "response": self._slash_undo(session_id)}

        if command == "retry":
            recovered = self._slash_retry(session_id)
            if recovered is None:
                return {"handled": True, "response": "No previous message to retry."}
            return {"handled": True, "rewrite_message": recovered}

        if command == "profile":
            return {"handled": True, "response": self._slash_profile()}

        if command == "branch":
            return {"handled": True, "response": self._slash_branch(session_id, args)}

        if command == "resume":
            return {"handled": True, "response": self._slash_resume(args)}

        return None  # Shouldn't reach here, but be safe

    # -- quick_commands helpers -------------------------------------------

    def _get_quick_commands(self) -> dict:
        """Return the quick_commands dict from the gateway config, or {}."""
        gw = getattr(self, "gateway_runner", None)
        if gw is None:
            return {}
        gw_config = getattr(gw, "config", None)
        if gw_config is None:
            return {}
        if isinstance(gw_config, dict):
            qcmds = gw_config.get("quick_commands", {}) or {}
        else:
            qcmds = getattr(gw_config, "quick_commands", {}) or {}
        return qcmds if isinstance(qcmds, dict) else {}

    async def _run_quick_command(
        self, command: str, qc: dict, args: str
    ) -> Optional[dict]:
        """Execute a user-defined exec quick command and return its result."""
        exec_cmd = qc.get("command", "")
        if not exec_cmd:
            return {"handled": True,
                    "response": f"Quick command '/{command}' has no command defined."}

        # Substitute {args} placeholder with actual slash-command arguments
        cmd = exec_cmd.replace("{args}", args.strip())

        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            output = (stdout or stderr).decode().strip()
            return {"handled": True,
                    "response": output if output else "Command returned no output."}
        except asyncio.TimeoutError:
            return {"handled": True,
                    "response": "Quick command timed out (30s)."}
        except Exception as e:
            return {"handled": True,
                    "response": f"Quick command error: {e}"}

    # -- individual command handlers -------------------------------------

    def _slash_help(self) -> str:
        """Return a plain-text list of API-server-supported slash commands."""
        return (
            "Available commands: "
            "/new (reset session), "
            "/title <name> (set session title), "
            "/status (session info), "
            "/usage (token counts), "
            "/retry (rerun last message), "
            "/undo (remove last exchange), "
            "/profile (show active profile), "
            "/branch [name] (fork session), "
            "/resume [name] (resume named session), "
            "/help (this list)"
        )

    def _slash_reset(self, session_id: str) -> str:
        """Clear all messages for *session_id*."""
        db = self._ensure_session_db()
        if db is not None:
            db.clear_messages(session_id)
        return "Session reset. Starting fresh."

    def _slash_title(self, session_id: str, args: str) -> str:
        """Set or show the session title."""
        db = self._ensure_session_db()
        if db is None:
            return "Session database not available."

        if args:
            try:
                sanitized = db.sanitize_title(args)
            except ValueError as e:
                return str(e)
            if not sanitized:
                return "Title must have printable characters."
            try:
                db.set_session_title(session_id, sanitized)
            except ValueError as e:
                return f"⚠️  {e}"
            return f"Title set: {sanitized}"

        title = db.get_session_title(session_id)
        if title:
            return f"Title: {title}"
        return "No title set. Usage: /title My Session Name"

    def _slash_status(self, session_id: str, body: dict) -> str:
        """Return a single-line session summary: messages, tokens, model, age."""
        db = self._ensure_session_db()
        model = body.get("model", self._model_name) if body else self._model_name

        msg_count = db.message_count(session_id) if db else 0
        parts = [f"{msg_count} msgs"]

        if db is not None:
            session = db.get_session(session_id)
            if session:
                inp = session.get("input_tokens", 0) or 0
                out = session.get("output_tokens", 0) or 0
                total = inp + out
                if total:
                    if total >= 1000:
                        parts.append(f"{total / 1000:.0f}k tokens")
                    else:
                        parts.append(f"{total} tokens")
                started = session.get("started_at")
                if started:
                    age_s = time.time() - started
                    if age_s < 3600:
                        parts.append(f"{max(1, age_s / 60):.0f}m old")
                    elif age_s < 86400:
                        parts.append(f"{age_s / 3600:.0f}h old")
                    else:
                        parts.append(f"{age_s / 86400:.0f}d old")

        parts.append(model)
        return f"session: {', '.join(parts)}"

    def _slash_usage(self, session_id: str) -> str:
        """Return token counts for the session."""
        db = self._ensure_session_db()
        if db is None:
            return "Session database not available."

        session = db.get_session(session_id)
        if not session:
            return "Session not found."

        inp = session.get("input_tokens", 0) or 0
        out = session.get("output_tokens", 0) or 0
        total = inp + out
        return f"{inp:,} in / {out:,} out / {total:,} total tokens"

    def _slash_undo(self, session_id: str) -> str:
        """Remove the last user/assistant exchange, transactionally."""
        db = self._ensure_session_db()
        if db is None:
            return "Session database not available."

        messages = db.get_messages(session_id)
        if not messages:
            return "Nothing to undo."

        last_user_idx = None
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                last_user_idx = i
                break

        if last_user_idx is None:
            return "Nothing to undo."

        removed_msg = messages[last_user_idx].get("content", "") or ""
        removed_count = len(messages) - last_user_idx
        old_count = len(messages)
        new_count = last_user_idx

        ids_to_delete = [messages[j]["id"] for j in range(last_user_idx, len(messages))]
        placeholders = ",".join("?" for _ in ids_to_delete)

        def _do(conn):
            conn.execute(
                f"DELETE FROM messages WHERE id IN ({placeholders})",
                ids_to_delete,
            )
            conn.execute(
                "UPDATE sessions SET message_count = ? WHERE id = ?",
                (new_count, session_id),
            )

        db._execute_write(_do)

        logger.info(
            "Rebuilt session %s: %d → %d messages (/undo)",
            session_id, old_count, new_count,
        )

        preview = removed_msg[:40] + "..." if len(removed_msg) > 40 else removed_msg
        return f'Removed last {removed_count} message(s): "{preview}"'

    def _slash_retry(self, session_id: str) -> Optional[str]:
        """Return the last user message for re-processing, or None.

        Truncates history so the retry doesn't duplicate context.
        Caller is responsible for passing the returned message through
        the normal agent run path.
        """
        db = self._ensure_session_db()
        if db is None:
            return None

        messages = db.get_messages(session_id)
        if not messages:
            return None

        last_user_idx = None
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                last_user_idx = i
                break

        if last_user_idx is None:
            return None

        last_user_content = messages[last_user_idx].get("content", "") or ""
        if not last_user_content:
            return None

        old_count = len(messages)
        new_count = last_user_idx

        ids_to_delete = [
            messages[j]["id"] for j in range(last_user_idx, len(messages))
        ]
        placeholders = ",".join("?" for _ in ids_to_delete)

        def _do(conn):
            conn.execute(
                f"DELETE FROM messages WHERE id IN ({placeholders})",
                ids_to_delete,
            )
            conn.execute(
                "UPDATE sessions SET message_count = ? WHERE id = ?",
                (new_count, session_id),
            )

        db._execute_write(_do)

        logger.info(
            "Rebuilt session %s: %d → %d messages (/retry)",
            session_id, old_count, new_count,
        )

        return last_user_content

    def _slash_profile(self) -> str:
        """Return the active profile name and home directory.

        Mirrors ``_handle_profile_command`` in gateway/run.py.
        """
        from hermes_constants import display_hermes_home
        from hermes_cli.profiles import get_active_profile_name

        display = display_hermes_home()
        profile_name = get_active_profile_name()

        lines = [
            f"👤 Profile: `{profile_name}`",
            f"📂 Home: `{display}`",
        ]
        return "\n".join(lines)

    def _slash_branch(self, session_id: str, args: str) -> str:
        """Fork the current session into a new independent copy.

        Mirrors ``_handle_branch_command`` in gateway/run.py.
        """
        db = self._ensure_session_db()
        if db is None:
            return "Session database not available."

        messages = db.get_messages(session_id)
        if not messages:
            return "No conversation to branch — send a message first."

        branch_name = args

        now = _dt.now()
        timestamp_str = now.strftime("%Y%m%d_%H%M%S")
        short_uuid = _uuid.uuid4().hex[:6]
        new_session_id = f"{timestamp_str}_{short_uuid}"

        if branch_name:
            branch_title = branch_name
        else:
            current_title = db.get_session_title(session_id)
            base = current_title or "branch"
            branch_title = db.get_next_title_in_lineage(base)

        try:
            db.create_session(
                session_id=new_session_id,
                source="api_server",
                parent_session_id=session_id,
            )
        except Exception as e:
            logger.error("Failed to create branch session: %s", e)
            return f"Failed to create branch: {e}"

        for msg in messages:
            try:
                db.append_message(
                    session_id=new_session_id,
                    role=msg.get("role", "user"),
                    content=msg.get("content"),
                    tool_name=msg.get("tool_name") or msg.get("name"),
                    tool_calls=msg.get("tool_calls"),
                    tool_call_id=msg.get("tool_call_id"),
                    finish_reason=msg.get("finish_reason"),
                    reasoning=msg.get("reasoning"),
                    reasoning_content=msg.get("reasoning_content"),
                    reasoning_details=msg.get("reasoning_details"),
                    codex_reasoning_items=msg.get("codex_reasoning_items"),
                    codex_message_items=msg.get("codex_message_items"),
                )
            except Exception:
                pass

        try:
            db.set_session_title(new_session_id, branch_title)
        except Exception:
            pass

        user_msg_count = len([m for m in messages if m.get("role") == "user"])
        return (
            f"⑂ Branched to **{branch_title}**\n"
            f"Session ID: `{new_session_id}` ({user_msg_count} msg)"
        )

    def _slash_resume(self, args: str) -> str:
        """Resolve a session title to its session_id.

        Mirrors ``_handle_resume_command`` in gateway/run.py.
        """
        db = self._ensure_session_db()
        if db is None:
            return "Session database not available."

        if not args:
            try:
                sessions = db.list_sessions_rich(source="api_server", limit=10)
                all_sessions = db.list_sessions_rich(limit=20)
                titled: list[dict] = []
                seen: set[str] = set()
                for s in sessions + all_sessions:
                    sid = s.get("id", "")
                    title = s.get("title")
                    if title and sid not in seen:
                        seen.add(sid)
                        titled.append(s)
                titled = titled[:10]

                if not titled:
                    return (
                        "No named sessions found.\n"
                        "Use `/title My Session` to name your current "
                        "session, then `/resume My Session` to return to "
                        "it later."
                    )

                lines = ["📋 **Named Sessions**\n"]
                for s in titled:
                    title = s["title"]
                    msg_count = s.get("message_count", 0) or 0
                    lines.append(f"• **{title}** — {msg_count} msg")
                lines.append("\nUsage: `/resume <name>`")
                return "\n".join(lines)
            except Exception as e:
                logger.debug("Failed to list titled sessions: %s", e)
                return f"Could not list sessions: {e}"

        name = args

        target_id = db.resolve_session_by_title(name)
        if not target_id:
            return (
                f"No session found matching '{name}'.\n"
                "Use `/resume` with no arguments to see available sessions."
            )

        try:
            target_id = db.resolve_resume_session_id(target_id)
        except Exception as e:
            logger.debug(
                "Failed to resolve resume continuation for %s: %s",
                target_id, e,
            )

        title = db.get_session_title(target_id) or name
        msg_count = db.message_count(target_id) or 0

        return (
            f"↻ Resumed **{title}**\n"
            f"Session ID: `{target_id}` ({msg_count} msg)"
        )
