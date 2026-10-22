"""`message questions` and `message answer` — an agent's question card,
answered the way the web and iOS apps answer it.

The card is shaped like the ones the channel agent's `ask_user` posts: one
pick-one question whose first option carries a dry run's ``plan_id``, and one
pick-any question. The plan id is what the tests watch most closely — it rides
on the option, hidden, and must never reach output, while the server (not the
CLI) copies it into ``metadata.answer_plan_ids`` once the answers land. So the
answers part goes up with empty metadata, exactly as the apps send it.
"""

from __future__ import annotations

import json
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from popcorn_cli import cli
from popcorn_core import operations
from popcorn_core import questions as q
from popcorn_core.errors import PopcornError


def _uuid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


_PLAN_ID = "example-plan-0001"
_QUESTION_MSG = _uuid(10)
_QUESTION_ID = _uuid(11)
_THREAD = _uuid(12)
_AGENT = {"id": _uuid(2), "username": "popcorn", "display_name": "Popcorn", "is_bot": True}
_MEMBER = {"id": _uuid(3), "username": "example-member", "is_bot": False}


def _question_part(status: str = "pending") -> dict[str, Any]:
    return {
        "id": _uuid(20),
        "type": "question",
        "metadata": {"is_reasoning": False},
        "question_id": _QUESTION_ID,
        "questions": [
            {
                "question": "Ready to record the intake answers?",
                "header": "Record",
                "options": [
                    {
                        "label": "Yes, record them (Recommended)",
                        "description": "Writes the answers to the board.",
                        "plan_id": _PLAN_ID,
                    },
                    {"label": "Cancel", "description": "Record nothing yet.", "plan_id": None},
                ],
                "multi_select": False,
            },
            {
                "question": "Who should get the email?",
                "header": "Notify",
                "options": [
                    {"label": "Owners", "description": "", "plan_id": None},
                    {"label": "Admins", "description": "", "plan_id": None},
                    {"label": "Nobody", "description": "", "plan_id": None},
                ],
                "multi_select": True,
            },
        ],
        "status": status,
        "answered_by": None if status == "pending" else _uuid(3),
        "answers_message_id": None if status == "pending" else _uuid(30),
    }


def _message(
    n: int | str,
    parts: list[dict[str, Any]],
    *,
    author: dict[str, Any] = _AGENT,
    thread_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": n if isinstance(n, str) else _uuid(n),
        "conversation_id": _uuid(1),
        "author": author,
        "user_id": author["id"],
        "thread_id": thread_id,
        "deleted_at": None,
        "metadata": {},
        "created_at": "2026-01-01T00:00:00Z",
        "content": {"parts": parts},
    }


def _text(n: int, text: str, author: dict[str, Any] = _MEMBER) -> dict[str, Any]:
    return _message(n, [{"type": "text", "content": text}], author=author)


def _card(**kw: Any) -> dict[str, Any]:
    return _message(
        _QUESTION_MSG,
        [_question_part(**{k: v for k, v in kw.items() if k == "status"})],
        thread_id=kw.get("thread_id"),
    )


# ---------------------------------------------------------------------------
# Building the answers
# ---------------------------------------------------------------------------


class TestBuildAnswers:
    def test_a_pick_by_number_names_the_option_by_its_label(self):
        answers = q.build_answers(_question_part(), {1: q.Selection(options=[1])})
        assert answers[0] == {
            "question": "Ready to record the intake answers?",
            "selected": ["Yes, record them (Recommended)"],
            "custom": None,
            "skipped": False,
        }

    def test_a_question_given_nothing_goes_as_skipped_as_in_the_apps(self):
        """The server refuses answers that leave a question out; the apps send
        the one a member never got to as skipped."""
        answers = q.build_answers(_question_part(), {1: q.Selection(options=[2])})
        assert answers[1] == {
            "question": "Who should get the email?",
            "selected": [],
            "custom": None,
            "skipped": True,
        }

    def test_a_label_matches_case_insensitively_but_goes_as_the_card_spells_it(self):
        answers = q.build_answers(
            _question_part(), {1: q.Selection(labels=["yes, record them (recommended)"])}
        )
        assert answers[0]["selected"] == ["Yes, record them (Recommended)"]

    def test_multi_select_takes_several_picks_in_the_order_given(self):
        answers = q.build_answers(
            _question_part(), {2: q.Selection(options=[2], labels=["Owners"])}
        )
        assert answers[1]["selected"] == ["Admins", "Owners"]
        assert answers[1]["skipped"] is False

    def test_free_text_alone_is_an_answer(self):
        answers = q.build_answers(_question_part(), {1: q.Selection(custom="  not yet  ")})
        assert answers[0] == {
            "question": "Ready to record the intake answers?",
            "selected": [],
            "custom": "not yet",
            "skipped": False,
        }

    def test_free_text_beside_a_pick(self):
        answers = q.build_answers(
            _question_part(), {2: q.Selection(labels=["Owners"], custom="and the vendor")}
        )
        assert answers[1]["selected"] == ["Owners"]
        assert answers[1]["custom"] == "and the vendor"

    def test_blank_free_text_is_no_answer(self):
        answers = q.build_answers(_question_part(), {1: q.Selection(custom="   ")})
        assert answers[0]["custom"] is None
        assert answers[0]["skipped"] is True

    def test_skip(self):
        answers = q.build_answers(_question_part(), {1: q.Selection(skip=True)})
        assert answers[0] == {
            "question": "Ready to record the intake answers?",
            "selected": [],
            "custom": None,
            "skipped": True,
        }

    @pytest.mark.parametrize(
        ("selections", "message"),
        [
            ({1: q.Selection(options=[3])}, "no option 3"),
            ({1: q.Selection(options=[0])}, "no option 0"),
            ({1: q.Selection(labels=["Maybe"])}, "no option 'Maybe'"),
            ({1: q.Selection(options=[1, 2])}, "takes one option"),
            ({2: q.Selection(options=[1], labels=["Owners"])}, "picked twice"),
            ({1: q.Selection(options=[1], skip=True)}, "skipped and answered"),
            ({1: q.Selection(custom="x", skip=True)}, "skipped and answered"),
            ({3: q.Selection(options=[1])}, "no question 3"),
        ],
    )
    def test_an_answer_the_server_would_refuse_is_refused_first(self, selections, message):
        with pytest.raises(PopcornError, match=message) as exc:
            q.build_answers(_question_part(), selections)
        assert exc.value.error_code == "validation"


class TestAnswersPart:
    def test_the_part_is_the_one_the_apps_put_on_the_wire(self):
        answers = q.build_answers(_question_part(), {1: q.Selection(options=[1])})
        part = q.answers_part(_question_part(), _QUESTION_MSG, answers)
        assert set(part) == {
            "id",
            "type",
            "created_at",
            "streaming_state",
            "visibility",
            "version",
            "replaced_by",
            "replaces",
            "error",
            "attachments",
            "metadata",
            "question_id",
            "question_message_id",
            "answers",
        }
        assert part["type"] == "answers"
        assert part["question_id"] == _QUESTION_ID
        assert part["question_message_id"] == _QUESTION_MSG
        assert part["answers"] == answers
        assert part["visibility"] == "public"
        assert part["version"] == 1
        assert part["attachments"] == []
        # `Date.toISOString()`: UTC, milliseconds, Z.
        assert part["created_at"].endswith("Z") and len(part["created_at"]) == 24

    def test_the_plan_id_is_not_sent_the_server_copies_it(self):
        """The server writes `answer_plan_ids` from the picked option itself and
        drops a client's, so the part carries the label and nothing more."""
        answers = q.build_answers(_question_part(), {1: q.Selection(options=[1])})
        part = q.answers_part(_question_part(), _QUESTION_MSG, answers)
        assert part["metadata"] == {}
        assert _PLAN_ID not in json.dumps(part)


class TestSelectionsFromJson:
    def test_one_object_per_question(self):
        sel = q.selections_from_json(
            [{"option": 1}, {"label": ["Owners", "Admins"], "custom": "and the vendor"}]
        )
        assert sel[1] == q.Selection(options=[1])
        assert sel[2] == q.Selection(labels=["Owners", "Admins"], custom="and the vendor")

    def test_skip_and_an_option_list(self):
        sel = q.selections_from_json([{"skip": True}, {"option": [1, "3"]}])
        assert sel[1].skip is True
        assert sel[2].options == [1, 3]

    @pytest.mark.parametrize(
        ("spec", "message"),
        [
            ({"option": 1}, "JSON list"),
            ([], "JSON list"),
            (["1"], "must be an object"),
            ([{"pick": 1}], "unknown key 'pick'"),
            ([{"option": "one"}], "must be a number"),
            ([{"option": True}], "must be a number"),
            ([{"custom": 3}], "custom must be a string"),
        ],
    )
    def test_malformed(self, spec, message):
        with pytest.raises(PopcornError, match=message):
            q.selections_from_json(spec)


# ---------------------------------------------------------------------------
# Which cards are open, and which can be answered
# ---------------------------------------------------------------------------


class TestPendingCards:
    def test_newest_first_and_the_newest_message_is_marked(self):
        older = _message(5, [{**_question_part(), "question_id": _uuid(6)}])
        cards = q.pending_cards([older, _text(7, "hm"), _card()])
        assert [c["message_id"] for c in cards] == [_QUESTION_MSG, _uuid(5)]
        assert [c["latest"] for c in cards] == [True, False]

    def test_answered_deleted_and_non_agent_cards_are_left_out(self):
        answered = _message(5, [_question_part(status="answered")])
        deleted = {**_message(6, [_question_part()]), "deleted_at": "2026-01-01T00:00:01Z"}
        member = _message(7, [_question_part()], author=_MEMBER)
        assert q.pending_cards([answered, deleted, member]) == []

    def test_options_are_numbered_and_carry_no_plan_id(self):
        card = q.pending_cards([_card()])[0]
        assert card["questions"][0]["options"][0] == {
            "index": 1,
            "label": "Yes, record them (Recommended)",
            "description": "Writes the answers to the board.",
        }
        assert card["questions"][1]["multi_select"] is True
        assert "plan_id" not in json.dumps(card)

    def test_a_hidden_field_added_later_is_left_out_too(self):
        part = _question_part()
        part["questions"][0]["options"][0]["example_hidden"] = "secret"
        assert "secret" not in json.dumps(q.card_view(_card(), part))


class TestCheckAnswerable:
    def test_a_pending_agent_card(self):
        assert q.check_answerable(_card(), _QUESTION_MSG)["question_id"] == _QUESTION_ID

    def test_already_answered_is_a_conflict(self):
        with pytest.raises(PopcornError, match="already been answered") as exc:
            q.check_answerable(_card(status="answered"), _QUESTION_MSG)
        assert exc.value.error_code == "conflict"

    def test_a_message_without_a_question(self):
        with pytest.raises(PopcornError, match="not an agent's question"):
            q.check_answerable(_text(5, "hello", author=_AGENT), _uuid(5))

    def test_a_question_part_not_posted_by_an_agent(self):
        with pytest.raises(PopcornError, match="not an agent's question"):
            q.check_answerable(_message(5, [_question_part()], author=_MEMBER), _uuid(5))

    def test_not_found_points_at_thread(self):
        with pytest.raises(PopcornError, match="not among") as exc:
            q.check_answerable(None, _uuid(99))
        assert exc.value.error_code == "not_found"
        assert "--thread" in (exc.value.hint or "")


# ---------------------------------------------------------------------------
# The commands, end to end through main()
# ---------------------------------------------------------------------------

_SERVER_ECHO = {
    "ok": True,
    "message": {
        "id": _uuid(40),
        "content": {"parts": [{"type": "answers", "metadata": {"answer_plan_ids": [[_PLAN_ID]]}}]},
    },
}


@pytest.fixture
def client(monkeypatch):
    client = MagicMock()
    client.post.return_value = _SERVER_ECHO
    monkeypatch.setattr(operations, "resolve_conversation", lambda c, conv: _uuid(1))
    monkeypatch.setattr(cli, "_check_and_update", lambda: None)
    monkeypatch.setattr(cli, "_get_client", lambda args: client)
    monkeypatch.delenv("POPCORN_ASSUME_YES", raising=False)
    monkeypatch.delenv("POPCORN_AGENT", raising=False)
    return client


def _history(client: MagicMock, *messages: dict[str, Any]) -> None:
    client.get.return_value = {"messages": list(messages), "has_more": False}


def _run(monkeypatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["popcorn", *argv])
    cli.main()


def _sent_body(client: MagicMock) -> dict[str, Any]:
    client.post.assert_called_once()
    assert client.post.call_args.args[0] == "/api/messages/post"
    return client.post.call_args.kwargs["data"]


class TestQuestionsCommand:
    def test_text_lists_numbered_options_without_plan_ids(self, client, monkeypatch, capsys):
        _history(client, _text(5, "hi"), _card())
        _run(monkeypatch, "message", "questions", "#example-channel", "--no-color")
        out = capsys.readouterr().out
        assert f"message {_QUESTION_MSG}" in out
        assert "1) Yes, record them (Recommended) — Writes the answers to the board." in out
        assert "(pick any)" in out
        assert _PLAN_ID not in out

    def test_json_without_plan_ids(self, client, monkeypatch, capsys):
        _history(client, _card())
        _run(monkeypatch, "--json", "message", "questions", "#example-channel")
        out = capsys.readouterr().out
        data = json.loads(out)["data"]
        assert data["questions"][0]["question_id"] == _QUESTION_ID
        assert data["scanned"] == 1
        assert _PLAN_ID not in out

    def test_reads_the_thread_when_asked(self, client, monkeypatch, capsys):
        _history(client)
        _run(monkeypatch, "message", "questions", "#example-channel", "--thread", _THREAD)
        assert client.get.call_args.args[0] == "/api/messages/thread"
        assert client.get.call_args.args[1]["thread_ts"] == _THREAD
        assert "No open questions" in capsys.readouterr().out


class TestAnswerCommand:
    def test_yes_posts_the_answers_part_and_prints_no_plan_id(self, client, monkeypatch, capsys):
        _history(client, _card())
        _run(
            monkeypatch,
            "--json",
            "message",
            "answer",
            "#example-channel",
            "--message",
            _QUESTION_MSG,
            "--option",
            "1",
            "--yes",
        )
        body = _sent_body(client)
        assert body["conversation"] == _uuid(1)
        assert "thread_id" not in body  # a top-level question is answered top level
        [part] = body["content"]["parts"]
        assert part["type"] == "answers"
        assert part["question_message_id"] == _QUESTION_MSG
        assert part["answers"][0]["selected"] == ["Yes, record them (Recommended)"]
        assert part["answers"][1]["skipped"] is True
        assert part["metadata"] == {}

        out = capsys.readouterr().out
        data = json.loads(out)["data"]
        assert data["message_id"] == _uuid(40)
        assert data["answers"] == part["answers"]
        # The server's echo carries `answer_plan_ids`; it is not passed on.
        assert _PLAN_ID not in out

    def test_a_threaded_question_is_answered_in_its_thread(self, client, monkeypatch):
        _history(client, _card(thread_id=_THREAD))
        _run(
            monkeypatch,
            "message",
            "answer",
            "#example-channel",
            "--thread",
            _THREAD,
            "--message",
            _QUESTION_MSG,
            "--label",
            "Cancel",
            "-y",
        )
        assert _sent_body(client)["thread_id"] == _THREAD

    def test_answers_json_answers_every_question(self, client, monkeypatch):
        _history(client, _card())
        spec = json.dumps([{"option": 2}, {"label": ["Owners", "Admins"], "custom": "and more"}])
        _run(
            monkeypatch,
            "message",
            "answer",
            "#example-channel",
            "--message",
            _QUESTION_MSG,
            "--answers",
            spec,
            "--yes",
        )
        answers = _sent_body(client)["content"]["parts"][0]["answers"]
        assert answers[0]["selected"] == ["Cancel"]
        assert answers[1] == {
            "question": "Who should get the email?",
            "selected": ["Owners", "Admins"],
            "custom": "and more",
            "skipped": False,
        }

    def test_question_flag_targets_the_second_question(self, client, monkeypatch):
        _history(client, _card())
        _run(
            monkeypatch,
            "message",
            "answer",
            "#example-channel",
            "--message",
            _QUESTION_MSG,
            "--question",
            "2",
            "--option",
            "1",
            "--option",
            "3",
            "--yes",
        )
        answers = _sent_body(client)["content"]["parts"][0]["answers"]
        assert answers[0]["skipped"] is True
        assert answers[1]["selected"] == ["Owners", "Nobody"]

    def test_without_yes_off_a_tty_it_refuses_and_sends_nothing(self, client, monkeypatch, capsys):
        _history(client, _card())
        with pytest.raises(SystemExit):
            _run(
                monkeypatch,
                "message",
                "answer",
                "#example-channel",
                "--message",
                _QUESTION_MSG,
                "--option",
                "1",
            )
        client.post.assert_not_called()
        assert "--yes" in capsys.readouterr().err

    def test_the_prompt_shows_the_question_and_the_pick(self, client, monkeypatch, capsys, tty):
        _history(client, _card())
        prompts = tty("n")
        _run(
            monkeypatch,
            "message",
            "answer",
            "#example-channel",
            "--message",
            _QUESTION_MSG,
            "--option",
            "1",
        )
        client.post.assert_not_called()
        err = capsys.readouterr().err
        assert "Ready to record the intake answers?" in err
        assert '→ "Yes, record them (Recommended)"' in err
        assert "→ (skipped)" in err
        assert _PLAN_ID not in err
        assert prompts == ["Send? [y/N] "]

    def test_confirming_at_the_prompt_sends(self, client, monkeypatch, capsys, tty):
        _history(client, _card())
        tty("y")
        _run(
            monkeypatch,
            "message",
            "answer",
            "#example-channel",
            "--message",
            _QUESTION_MSG,
            "--custom",
            "not yet",
        )
        assert _sent_body(client)["content"]["parts"][0]["answers"][0]["custom"] == "not yet"
        assert f"Answered (id: {_uuid(40)})" in capsys.readouterr().out

    def test_dry_run_sends_nothing(self, client, monkeypatch, capsys):
        _history(client, _card())
        _run(
            monkeypatch,
            "--json",
            "message",
            "answer",
            "#example-channel",
            "--message",
            _QUESTION_MSG,
            "--option",
            "1",
            "--dry-run",
        )
        client.post.assert_not_called()
        out = capsys.readouterr().out
        data = json.loads(out)["data"]
        assert data["dry_run"] is True
        assert data["part"]["type"] == "answers"
        assert _PLAN_ID not in out

    @pytest.mark.parametrize(
        ("history", "flags", "error_code", "message"),
        [
            ([_card(status="answered")], ["--option", "1"], "conflict", "already been answered"),
            ([_text(5, "hi")], ["--option", "1"], "not_found", "not among"),
            ([_card()], ["--option", "9"], "validation", "no option 9"),
            ([_card()], [], "validation", "Say what to answer"),
            ([_card()], ["--option", "1", "--answers", "[]"], "validation", "replaces"),
            ([_card()], ["--answers", "{nope"], "validation", "not valid JSON"),
        ],
    )
    def test_refusals_send_nothing(
        self, client, monkeypatch, capsys, history, flags, error_code, message
    ):
        _history(client, *history)
        with pytest.raises(SystemExit):
            _run(
                monkeypatch,
                "--json",
                "message",
                "answer",
                "#example-channel",
                "--message",
                _QUESTION_MSG,
                *flags,
                "--yes",
            )
        client.post.assert_not_called()
        err = json.loads(capsys.readouterr().err)
        assert err["error_code"] == error_code
        assert message in err["error"]

    def test_a_message_that_is_not_a_question(self, client, monkeypatch, capsys):
        _history(client, _text(5, "hi", author=_AGENT))
        with pytest.raises(SystemExit):
            _run(
                monkeypatch,
                "--json",
                "message",
                "answer",
                "#example-channel",
                "--message",
                _uuid(5),
                "--option",
                "1",
                "--yes",
            )
        client.post.assert_not_called()
        assert "not an agent's question" in capsys.readouterr().err


def test_both_commands_are_in_the_schema(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["popcorn", "commands", "--json", "--groups=message"])
    monkeypatch.setattr(cli, "_check_and_update", lambda: None)
    cli.main()
    schema = json.loads(capsys.readouterr().out)
    [message] = [c for c in schema["commands"] if c["name"] == "message"]
    subs = {s["name"]: s for s in message["subcommands"]}
    assert {"questions", "answer"} <= set(subs)
    flags = {f for a in subs["answer"]["arguments"] for f in a.get("flags", [])}
    assert {"--message", "--option", "--label", "--custom", "--skip", "--answers"} <= flags
