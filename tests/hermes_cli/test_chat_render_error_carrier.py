"""Bare ``error`` turn results must render diagnostic text in interactive chat."""

from __future__ import annotations

import queue
from types import SimpleNamespace
from unittest.mock import MagicMock

from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin


class _Stub(CLIChatTurnMixin):
    def __init__(self):
        self._interrupt_queue = queue.Queue()
        self._pending_input = queue.Queue()
        self._voice_tts = None
        self._voice_continuous = False
        self.agent = MagicMock(max_iterations=500, provider="openrouter", model="x")
        self.provider = "openrouter"
        self.model = "x"
        self.rendered = []
        self._chat_print_reasoning_box = lambda turn: None
        self._chat_print_response_panel = lambda turn, response: self.rendered.append(response)
        self._emit_focus_recovery_line = lambda: None
        self._ring_bell = lambda **kwargs: None
        self._chat_resolve_interrupt = lambda turn, agent_thread, interrupt_msg, response: (None, False)


def test_bare_error_with_empty_response_renders_diagnostic_text():
    cli = _Stub()
    turn = SimpleNamespace(
        result={"final_response": "", "error": "provider unavailable"},
        mute_notification_reply=False,
        use_streaming_tts=False,
    )

    response = cli._chat_render_turn(turn, MagicMock(), None)

    assert response
    assert "provider unavailable" in response
    assert cli.rendered == [response]
