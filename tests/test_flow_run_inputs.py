"""`flow run` supplies conversation_id to a run's inputs.

Found while authoring a real template: nearly every flow declares the input,
and a run started without it fails at runtime rather than at the call.
"""

from __future__ import annotations

import pytest

from popcorn_core.operations import with_conversation_id


class TestWithConversationId:
    def test_injected_when_absent(self):
        assert with_conversation_id(None, "conv-1") == {"conversation_id": "conv-1"}

    def test_existing_inputs_are_preserved(self):
        out = with_conversation_id({"action": "ack"}, "conv-1")
        assert out == {"action": "ack", "conversation_id": "conv-1"}

    def test_caller_value_wins(self):
        """Never override an explicit input — a flow may legitimately target a
        different conversation than the one being addressed."""
        out = with_conversation_id({"conversation_id": "other"}, "conv-1")
        assert out["conversation_id"] == "other"

    def test_does_not_mutate_the_caller_dict(self):
        original = {"action": "ack"}
        with_conversation_id(original, "conv-1")
        assert original == {"action": "ack"}

    @pytest.mark.parametrize("empty", [None, {}])
    def test_empty_inputs_still_get_the_id(self, empty):
        assert with_conversation_id(empty, "conv-1")["conversation_id"] == "conv-1"
