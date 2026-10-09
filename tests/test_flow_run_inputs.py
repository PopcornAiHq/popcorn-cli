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


# ---------------------------------------------------------------------------
# `--input key=value`, repeatable, merged over `--inputs`
# ---------------------------------------------------------------------------


def _flow_run(monkeypatch, argv: list[str], stdin: str = "") -> dict:
    """Run `popcorn flow run <argv>`, returning the inputs sent."""
    import io
    import sys
    from unittest.mock import patch

    from popcorn_cli.cli import build_parser
    from popcorn_cli.registry import dispatch
    from popcorn_core import operations

    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    sent: dict = {}

    def _run(client, channel, flow_id, inputs=None):
        sent["inputs"] = inputs
        return {"workflow_id": "wf-1", "flow_name": flow_id}

    with (
        patch("popcorn_cli.cli._get_client", return_value=object()),
        patch("popcorn_cli.cli._output"),
        patch.object(operations, "run_flow", _run),
    ):
        dispatch(build_parser().parse_args(["flow", "run", "example", "--channel", "#c", *argv]))
    return sent["inputs"]


class TestInputFlag:
    def test_values_are_strings(self, monkeypatch):
        sent = _flow_run(monkeypatch, ["--input", "a=1", "--input", "b=true", "--input", "c="])
        assert sent == {"a": "1", "b": "true", "c": ""}

    def test_only_the_first_equals_splits(self, monkeypatch):
        assert _flow_run(monkeypatch, ["--input", "q=a=b"]) == {"q": "a=b"}

    def test_merged_over_inputs(self, monkeypatch):
        sent = _flow_run(monkeypatch, ["--inputs", '{"a": 1, "b": 2}', "--input", "b=override"])
        assert sent == {"a": 1, "b": "override"}

    def test_at_path_is_the_files_text(self, monkeypatch, tmp_path):
        body = tmp_path / "body.md"
        body.write_text("line one\nline two\n")
        sent = _flow_run(monkeypatch, ["--input", f"body=@{body}"])
        assert sent == {"body": "line one\nline two\n"}

    def test_at_dash_is_stdin(self, monkeypatch):
        assert _flow_run(monkeypatch, ["--input", "body=@-"], stdin="piped") == {"body": "piped"}

    def test_a_backslash_keeps_a_literal_at(self, monkeypatch):
        assert _flow_run(monkeypatch, ["--input", "handle=\\@someone"]) == {"handle": "@someone"}

    @pytest.mark.parametrize("bad", ["novalue", "=x"])
    def test_not_key_value_is_refused(self, monkeypatch, bad):
        from popcorn_core.errors import PopcornError

        with pytest.raises(PopcornError, match="not key=value") as exc:
            _flow_run(monkeypatch, ["--input", bad])
        assert exc.value.error_code == "validation"

    def test_a_key_twice_is_refused(self, monkeypatch):
        from popcorn_core.errors import PopcornError

        with pytest.raises(PopcornError, match="twice"):
            _flow_run(monkeypatch, ["--input", "a=1", "--input", "a=2"])

    def test_stdin_is_read_once(self, monkeypatch):
        from popcorn_core.errors import PopcornError

        with pytest.raises(PopcornError, match="stdin"):
            _flow_run(monkeypatch, ["--input", "a=@-", "--input", "b=@-"])
        with pytest.raises(PopcornError, match="stdin"):
            _flow_run(monkeypatch, ["--inputs", "@-", "--input", "b=@-"], stdin="{}")

    def test_a_missing_file_says_how_to_escape(self, monkeypatch, tmp_path):
        from popcorn_core.errors import PopcornError

        with pytest.raises(PopcornError, match="Cannot read --input") as exc:
            _flow_run(monkeypatch, ["--input", f"a=@{tmp_path / 'nope'}"])
        assert exc.value.hint is not None and "\\@" in exc.value.hint

    def test_bad_input_sends_nothing(self, monkeypatch):
        """Parsed before the client is built, so no request goes out."""
        from unittest.mock import patch

        from popcorn_core.errors import PopcornError

        with (
            patch("popcorn_cli.cli._get_client", side_effect=AssertionError("no client")),
            pytest.raises(PopcornError),
        ):
            from popcorn_cli.cli import build_parser
            from popcorn_cli.registry import dispatch

            dispatch(
                build_parser().parse_args(["flow", "run", "x", "--channel", "#c", "--input", "bad"])
            )
