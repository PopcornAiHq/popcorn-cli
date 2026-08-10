"""`flow run` addressing: resolve a flow by name, and supply conversation_id.

Both gaps were found while authoring a real template. `flow list` prints
names, the docs tell you to use them, and `flow run <name>` then failed —
because ad-hoc imported flows are UUID-addressed rows and the server's
by-name path only covers channel_app-bound bundle flows.
"""

from __future__ import annotations

import pytest

from popcorn_core.operations import resolve_flow_ref, with_conversation_id

_FLOWS = {
    "flows": [
        {"id": "50c3375e-ec4b-4f34-843d-7f870c9e5544", "name": "alert_tick"},
        {"id": "7299e10f-9d22-4602-bbed-2c01bed0955b", "name": "alert_apply"},
    ]
}


class TestResolveFlowRef:
    def test_uuid_passes_through_without_a_lookup(self):
        """A UUID is already addressable — no reason to spend a round trip."""
        calls = []

        def lister(*a, **k):
            calls.append(a)
            return _FLOWS

        out = resolve_flow_ref(None, "#ops", "50c3375e-ec4b-4f34-843d-7f870c9e5544", lister)
        assert out == "50c3375e-ec4b-4f34-843d-7f870c9e5544"
        assert calls == []

    def test_name_resolves_to_id(self):
        out = resolve_flow_ref(None, "#ops", "alert_tick", lambda *a, **k: _FLOWS)
        assert out == "50c3375e-ec4b-4f34-843d-7f870c9e5544"

    def test_unknown_name_passes_through(self):
        """Bundle flows bound via channel_app ARE resolvable by name server
        side, so an unmatched name must still reach the server rather than
        being rejected locally."""
        out = resolve_flow_ref(None, "#ops", "some_bundle_flow", lambda *a, **k: _FLOWS)
        assert out == "some_bundle_flow"

    def test_lookup_failure_is_not_fatal(self):
        """The lookup is a convenience. If listing fails, fall back to the
        server's own resolution instead of failing the run."""

        def boom(*a, **k):
            raise RuntimeError("network")

        assert resolve_flow_ref(None, "#ops", "alert_tick", boom) == "alert_tick"

    def test_empty_flow_list_passes_through(self):
        assert resolve_flow_ref(None, "#ops", "alert_tick", lambda *a, **k: {}) == "alert_tick"


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
