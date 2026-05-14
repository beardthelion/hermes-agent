"""Tests for slash command interception in the API server adapter.

Extracted from tests/gateway/test_api_server.py so the slash test class
lives separately and doesn't collide with upstream test changes.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

# Reuse the fixtures and helpers from the main test module.
from tests.gateway.test_api_server import (
    _create_app,
    adapter,
    auth_adapter,
)


# Slash command interception
# ---------------------------------------------------------------------------


class TestSlashCommandInterception:
    """Slash commands are intercepted before the LLM call."""

    @pytest.mark.asyncio
    async def test_help_command_returns_formatted_list(self, adapter):
        """/help returns the command list without hitting the LLM."""
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/help"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            content = data["choices"][0]["message"]["content"]
            assert "/new" in content
            assert "/help" in content
            assert "/status" in content
            # Must be a slash response, not an LLM response — no tokens consumed
            assert data["usage"]["total_tokens"] == 0

    @pytest.mark.asyncio
    async def test_new_reset_clears_session(self, adapter):
        """/new and /reset both clear the session and return confirmation."""
        for cmd in ("/new", "/reset"):
            app = _create_app(adapter)
            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={"model": "hermes-agent", "messages": [{"role": "user", "content": cmd}]},
                )
                assert resp.status == 200
                data = await resp.json()
                assert "Session reset" in data["choices"][0]["message"]["content"]
                assert data["usage"]["total_tokens"] == 0

    @pytest.mark.asyncio
    async def test_non_slash_message_passes_through_to_llm(self, adapter):
        """A non-slash message is NOT intercepted — it reaches the agent."""
        mock_result = {"final_response": "Hello, human!", "messages": [], "api_calls": 1}
        usage = {"input_tokens": 50, "output_tokens": 20, "total_tokens": 70}

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, usage)
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={"model": "hermes-agent", "messages": [{"role": "user", "content": "Hello"}]},
                )
            assert resp.status == 200
            data = await resp.json()
            # The agent ran and produced a response
            assert data["choices"][0]["message"]["content"] == "Hello, human!"
            assert data["usage"]["total_tokens"] == 70
            mock_run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unknown_slash_command_passes_through_to_llm(self, adapter):
        """/xyz123 (not in supported set) falls through to the LLM."""
        mock_result = {"final_response": "I don't know that command", "messages": [], "api_calls": 1}
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, usage)
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/xyz123"}]},
                )
            assert resp.status == 200
            data = await resp.json()
            # The agent ran (not intercepted)
            assert data["choices"][0]["message"]["content"] == "I don't know that command"
            mock_run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_malformed_slash_not_treated_as_command(self, adapter):
        """/path/to/file (contains extra slashes) is not parsed as a command."""
        mock_result = {"final_response": "That looks like a path", "messages": [], "api_calls": 1}
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, usage)
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/path/to/file"}]},
                )
            assert resp.status == 200
            # Falls through to LLM — not treated as a command
            mock_run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_title_set_and_show(self, adapter):
        """/title sets the session title via SessionDB."""
        app = _create_app(adapter)
        session_id = "test-title-session-002"

        # Mock SessionDB to store/retrieve title
        mock_db = MagicMock()
        mock_db.sanitize_title = lambda t: t.strip() if t else None
        mock_db.set_session_title = MagicMock(return_value=True)
        mock_db.get_session_title = MagicMock(return_value=None)
        adapter._session_db = mock_db

        async with TestClient(TestServer(app)) as cli:
            # Set title
            resp = await cli.post(
                "/v1/chat/completions",
                json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/title hello world"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            assert "hello world" in data["choices"][0]["message"]["content"]
            mock_db.set_session_title.assert_called_once()
            assert data["usage"]["total_tokens"] == 0

    @pytest.mark.asyncio
    async def test_title_duplicate_returns_warning_not_500(self, adapter):
        """/title returns a ⚠️ warning (not 500) when set_session_title raises ValueError."""
        app = _create_app(adapter)

        mock_db = MagicMock()
        mock_db.sanitize_title = lambda t: t.strip() if t else None
        mock_db.set_session_title = MagicMock(side_effect=ValueError("A session with this title already exists"))
        mock_db.get_session_title = MagicMock(return_value=None)
        adapter._session_db = mock_db

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/title My Duped Title"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            content = data["choices"][0]["message"]["content"]
            assert "⚠️" in content
            assert "already exists" in content
            assert data["usage"]["total_tokens"] == 0

    @pytest.mark.asyncio
    async def test_retry_triggers_agent_rerun(self, auth_adapter):
        """/retry rewrites the message and reruns the agent."""
        mock_result = {"final_response": "Fresh answer!", "messages": [], "api_calls": 1}
        usage = {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130}

        app = _create_app(auth_adapter)
        session_id = "test-retry-session-004"

        async with TestClient(TestServer(app)) as cli:
            with patch.object(auth_adapter, "_slash_retry", return_value="recovered question"), \
                 patch.object(auth_adapter, "_run_agent", new_callable=AsyncMock) as mock_run:
                mock_run.return_value = (mock_result, usage)
                resp = await cli.post(
                    "/v1/chat/completions",
                    headers={"X-Hermes-Session-Id": session_id, "Authorization": "Bearer sk-secret"},
                    json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/retry"}]},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["choices"][0]["message"]["content"] == "Fresh answer!"
                assert data["usage"]["total_tokens"] == 130
                # Verify agent was called with the recovered message
                mock_run.assert_awaited_once()
                call_kwargs = mock_run.call_args.kwargs
                assert call_kwargs["user_message"] == "recovered question"

    @pytest.mark.asyncio
    async def test_undo_removes_last_exchange(self, adapter):
        """/undo returns a confirmation message."""
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            with patch.object(adapter, "_slash_undo", return_value="Removed 2 message(s): \"test\""):
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/undo"}]},
                )
                assert resp.status == 200
                data = await resp.json()
                content = data["choices"][0]["message"]["content"]
                assert "Removed" in content or "Undid" in content
                assert data["usage"]["total_tokens"] == 0

    @pytest.mark.asyncio
    async def test_retry_with_no_history_returns_error(self, adapter):
        """/retry with no prior messages returns a helpful message."""
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/retry"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            assert "No previous message" in data["choices"][0]["message"]["content"]
            assert data["usage"]["total_tokens"] == 0

    @pytest.mark.asyncio
    async def test_undo_with_no_history_returns_error(self, adapter):
        """/undo with nothing to undo returns a helpful message."""
        app = _create_app(adapter)
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/undo"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            assert "Nothing to undo" in data["choices"][0]["message"]["content"]
            assert data["usage"]["total_tokens"] == 0

    # ── /profile ────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_profile_returns_active_profile(self, adapter):
        """/profile returns the active profile name and home directory."""
        app = _create_app(adapter)
        with patch("hermes_cli.profiles.get_active_profile_name", return_value="my-profile"), \
             patch("hermes_constants.display_hermes_home", return_value="/home/user/.hermes"):
            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/profile"}]},
                )
                assert resp.status == 200
                data = await resp.json()
                content = data["choices"][0]["message"]["content"]
                assert "👤" in content
                assert "my-profile" in content
                assert "/home/user/.hermes" in content
                assert data["usage"]["total_tokens"] == 0

    # ── /branch ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_branch_clones_with_default_title(self, auth_adapter):
        """/branch with no name auto-generates a title and returns new session ID."""
        app = _create_app(auth_adapter)
        session_id = "test-branch-session-001"

        mock_messages = [
            {"role": "user", "content": "hello", "id": 1},
            {"role": "assistant", "content": "hi", "id": 2},
            {"role": "user", "content": "how are you", "id": 3},
        ]
        mock_db = MagicMock()
        mock_db.get_messages.return_value = mock_messages
        mock_db.get_session_title.return_value = "Old Title"
        mock_db.get_next_title_in_lineage.return_value = "Old Title #2"
        mock_db.create_session = MagicMock()
        mock_db.append_message = MagicMock()
        mock_db.set_session_title = MagicMock()
        auth_adapter._session_db = mock_db

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                headers={"X-Hermes-Session-Id": session_id, "Authorization": "Bearer sk-secret"},
                json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/branch"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            content = data["choices"][0]["message"]["content"]
            assert "⑂" in content
            assert "Old Title #2" in content
            assert "Session ID:" in content
            assert "2 msg" in content
            assert data["usage"]["total_tokens"] == 0
            mock_db.create_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_branch_with_custom_title(self, auth_adapter):
        """/branch My Name uses the provided name as the title."""
        app = _create_app(auth_adapter)
        session_id = "test-branch-session-002"

        mock_messages = [
            {"role": "user", "content": "test", "id": 1},
        ]
        mock_db = MagicMock()
        mock_db.get_messages.return_value = mock_messages
        mock_db.create_session = MagicMock()
        mock_db.append_message = MagicMock()
        mock_db.set_session_title = MagicMock()
        auth_adapter._session_db = mock_db

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                headers={"X-Hermes-Session-Id": session_id, "Authorization": "Bearer sk-secret"},
                json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/branch My Custom Branch"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            content = data["choices"][0]["message"]["content"]
            assert "My Custom Branch" in content
            assert "1 msg" in content
            assert data["usage"]["total_tokens"] == 0

    @pytest.mark.asyncio
    async def test_branch_no_history_returns_error(self, adapter):
        """/branch with an empty session returns a helpful message."""
        app = _create_app(adapter)

        mock_db = MagicMock()
        mock_db.get_messages.return_value = []
        adapter._session_db = mock_db

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/branch"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            assert "No conversation to branch" in data["choices"][0]["message"]["content"]
            assert data["usage"]["total_tokens"] == 0

    @pytest.mark.asyncio
    async def test_branch_copies_all_message_fields(self, auth_adapter):
        """/branch copies tool_calls, reasoning, and metadata to the new session."""
        app = _create_app(auth_adapter)
        session_id = "test-branch-rich-003"

        mock_messages = [
            {"role": "user", "content": "run test", "id": 1},
            {"role": "assistant", "content": None, "tool_calls": "[{\"id\":\"t1\"}]", "id": 2},
            {"role": "tool", "content": "result", "tool_call_id": "t1", "tool_name": "terminal", "id": 3},
            {"role": "assistant", "content": "done", "reasoning": "thought here", "id": 4},
        ]
        mock_db = MagicMock()
        mock_db.get_messages.return_value = mock_messages
        mock_db.get_session_title.return_value = None
        mock_db.get_next_title_in_lineage.return_value = "branch"
        mock_db.create_session = MagicMock()
        mock_db.append_message = MagicMock()
        mock_db.set_session_title = MagicMock()
        auth_adapter._session_db = mock_db

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                headers={"X-Hermes-Session-Id": session_id, "Authorization": "Bearer sk-secret"},
                json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/branch"}]},
            )
            assert resp.status == 200
            # All 4 messages should be copied
            assert mock_db.append_message.call_count == 4
            # First message is plain user text
            first_call = mock_db.append_message.call_args_list[0].kwargs
            assert first_call["role"] == "user"
            assert first_call["content"] == "run test"
            # Tool message should carry tool_call_id and tool_name
            tool_call = mock_db.append_message.call_args_list[2].kwargs
            assert tool_call["tool_call_id"] == "t1"
            assert tool_call["tool_name"] == "terminal"

    # ── /resume ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_resume_no_args_lists_titled_sessions(self, adapter):
        """/resume with no arguments lists recent titled sessions."""
        app = _create_app(adapter)

        mock_db = MagicMock()
        mock_db.list_sessions_rich.return_value = [
            {"id": "s1", "title": "Research", "message_count": 12},
            {"id": "s2", "title": "Debug Session", "message_count": 5},
        ]
        adapter._session_db = mock_db

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/resume"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            content = data["choices"][0]["message"]["content"]
            assert "📋" in content
            assert "Research" in content
            assert "12 msg" in content
            assert "Debug Session" in content
            assert "5 msg" in content
            assert "/resume" in content
            assert data["usage"]["total_tokens"] == 0

    @pytest.mark.asyncio
    async def test_resume_no_args_empty_result(self, adapter):
        """/resume with no titled sessions returns usage guidance."""
        app = _create_app(adapter)

        mock_db = MagicMock()
        mock_db.list_sessions_rich.return_value = []
        adapter._session_db = mock_db

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/resume"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            content = data["choices"][0]["message"]["content"]
            assert "No named sessions" in content
            assert "/title" in content
            assert data["usage"]["total_tokens"] == 0

    @pytest.mark.asyncio
    async def test_resume_valid_title_returns_session_id(self, adapter):
        """/resume <name> resolves the title and returns the session ID."""
        app = _create_app(adapter)

        mock_db = MagicMock()
        mock_db.resolve_session_by_title.return_value = "20260428_120000_abc123"
        mock_db.resolve_resume_session_id.return_value = "20260428_120000_abc123"
        mock_db.get_session_title.return_value = "My Session"
        mock_db.message_count.return_value = 42
        adapter._session_db = mock_db

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/resume My Session"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            content = data["choices"][0]["message"]["content"]
            assert "↻" in content
            assert "My Session" in content
            assert "20260428_120000_abc123" in content
            assert "42 msg" in content
            assert data["usage"]["total_tokens"] == 0

    @pytest.mark.asyncio
    async def test_resume_invalid_title_returns_error(self, adapter):
        """/resume NonExistent returns a clear error message."""
        app = _create_app(adapter)

        mock_db = MagicMock()
        mock_db.resolve_session_by_title.return_value = None
        adapter._session_db = mock_db

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/resume Bad Name"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            content = data["choices"][0]["message"]["content"]
            assert "No session found" in content
            assert "Bad Name" in content
            assert data["usage"]["total_tokens"] == 0

    @pytest.mark.asyncio
    async def test_resume_follows_compression_chain(self, adapter):
        """/resume follows the compression chain via resolve_resume_session_id."""
        app = _create_app(adapter)

        mock_db = MagicMock()
        mock_db.resolve_session_by_title.return_value = "old-session-001"
        # Compression created a continuation — resume should return the latest
        mock_db.resolve_resume_session_id.return_value = "old-session-001-cont-2"
        mock_db.get_session_title.return_value = "My Session"
        mock_db.message_count.return_value = 15
        adapter._session_db = mock_db

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/resume My Session"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            content = data["choices"][0]["message"]["content"]
            # Should return the continuation ID, not the original
            assert "old-session-001-cont-2" in content
            mock_db.resolve_resume_session_id.assert_called_once_with("old-session-001")

    @pytest.mark.asyncio
    async def test_resume_deduplicates_listed_sessions(self, adapter):
        """/resume listing deduplicates sessions with the same ID."""
        app = _create_app(adapter)

        mock_db = MagicMock()
        # First call returns api_server sessions, second returns all
        mock_db.list_sessions_rich.side_effect = [
            [{"id": "s1", "title": "Alpha", "message_count": 3}],
            [{"id": "s1", "title": "Alpha", "message_count": 3},
             {"id": "s2", "title": "Beta", "message_count": 7}],
        ]
        adapter._session_db = mock_db

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/resume"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            content = data["choices"][0]["message"]["content"]
            # Both appear, Alpha only once
            assert content.count("Alpha") == 1
            assert "Beta" in content

    # ── Dispatcher integration ───────────────────────────────────────

    @pytest.mark.asyncio
    async def test_profile_intercepted_before_llm(self, adapter):
        """/profile never reaches the agent — zero tokens consumed."""
        app = _create_app(adapter)
        with patch.object(adapter, "_slash_profile", return_value="Profile: default"):
            async with TestClient(TestServer(app)) as cli:
                resp = await cli.post(
                    "/v1/chat/completions",
                    json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/profile"}]},
                )
                assert resp.status == 200
                data = await resp.json()
                assert data["usage"]["total_tokens"] == 0

    @pytest.mark.asyncio
    async def test_branch_intercepted_before_llm(self, auth_adapter):
        """/branch never reaches the agent — zero tokens consumed."""
        app = _create_app(auth_adapter)
        session_id = "test-intercept-branch-001"
        mock_db = MagicMock()
        mock_db.get_messages.return_value = [{"role": "user", "content": "hi", "id": 1}]
        mock_db.get_session_title.return_value = None
        mock_db.get_next_title_in_lineage.return_value = "branch"
        mock_db.create_session = MagicMock()
        mock_db.append_message = MagicMock()
        mock_db.set_session_title = MagicMock()
        auth_adapter._session_db = mock_db

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                headers={"X-Hermes-Session-Id": session_id, "Authorization": "Bearer sk-secret"},
                json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/branch"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["usage"]["total_tokens"] == 0

    @pytest.mark.asyncio
    async def test_resume_intercepted_before_llm(self, adapter):
        """/resume never reaches the agent — zero tokens consumed."""
        app = _create_app(adapter)
        mock_db = MagicMock()
        mock_db.list_sessions_rich.return_value = [
            {"id": "s1", "title": "Test", "message_count": 1},
        ]
        adapter._session_db = mock_db

        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post(
                "/v1/chat/completions",
                json={"model": "hermes-agent", "messages": [{"role": "user", "content": "/resume"}]},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["usage"]["total_tokens"] == 0
