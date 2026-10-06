"""`popcorn project create --if-not-exists` — the server decides "already exists".

The flag used to be approximated client-side: list every project, compare
names after re-deriving the server's space-to-hyphen rule, and catch the
duplicate error for the race in between. Each of those was a copy of a rule the
server owns and could drift from it. The create route now takes the flag
itself and answers with `already_existed`, so the CLI's whole job is to send it
and to report honestly what came back.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from popcorn_cli.cli import build_parser
from popcorn_cli.registry import dispatch
from popcorn_core import operations
from popcorn_core.errors import APIError

EXISTING_ID = "00000000-0000-4000-8000-000000000001"
CREATED_ID = "00000000-0000-4000-8000-000000000002"


def _existing(**fields):
    conv = {"id": EXISTING_ID, "name": "example-my-channel", "type": "workspace_channel"}
    conv.update(fields)
    return {"ok": True, "conversation": conv, "already_existed": True}


def _created(name="example-fresh"):
    return {
        "ok": True,
        "conversation": {"id": CREATED_ID, "name": name, "type": "workspace_channel"},
        "already_existed": False,
    }


def _error(slug: str, status: int = 400) -> APIError:
    """An error as the create route sends it: the slug nested under `detail`."""
    body = {"detail": {"ok": False, "error": slug, "detail": f"{slug}: example-my-channel"}}
    return APIError(f"{slug}: example-my-channel", status_code=status, body=json.dumps(body))


@pytest.fixture()
def parser():
    return build_parser()


@pytest.fixture()
def client():
    c = MagicMock()
    with patch("popcorn_cli.cli._get_client", return_value=c):
        yield c


def _run(parser, argv):
    dispatch(parser.parse_args(argv))


class TestRequest:
    def test_the_flag_reaches_the_server_and_nothing_is_listed(self, parser, client):
        with (
            patch("popcorn_core.operations.search_channels") as search,
            patch("popcorn_core.operations.create_conversation", return_value=_created()) as create,
        ):
            _run(parser, ["project", "create", "example-fresh", "--if-not-exists"])

        assert create.call_args.kwargs["if_not_exists"] is True
        search.assert_not_called()
        client.get.assert_not_called()

    def test_without_the_flag_it_is_not_sent(self, parser, client):
        with patch(
            "popcorn_core.operations.create_conversation", return_value=_created()
        ) as create:
            _run(parser, ["project", "create", "example-fresh"])

        assert create.call_args.kwargs["if_not_exists"] is False

    def test_the_name_goes_out_as_typed(self, parser, client):
        """Normalising the name is the server's job; the CLI must not pre-empt it."""
        with patch(
            "popcorn_core.operations.create_conversation", return_value=_created("example-a-b")
        ) as create:
            _run(parser, ["project", "create", "example-a b", "--if-not-exists"])

        assert create.call_args.kwargs["name"] == "example-a b"

    def test_the_body_carries_the_flag_only_when_set(self):
        c = MagicMock()
        operations.create_conversation(c, "example-fresh", if_not_exists=True)
        assert c.post.call_args.kwargs["data"]["if_not_exists"] is True

        c = MagicMock()
        operations.create_conversation(c, "example-fresh")
        assert "if_not_exists" not in c.post.call_args.kwargs["data"]


class TestAlreadyExisted:
    def test_it_is_reported_as_existing_not_created(self, parser, client, capsys):
        with patch("popcorn_core.operations.create_conversation", return_value=_existing()):
            _run(parser, ["project", "create", "example-my project", "--if-not-exists"])

        captured = capsys.readouterr()
        assert "Already exists" in captured.out
        assert "Created" not in captured.out
        assert EXISTING_ID in captured.out
        assert captured.err == ""

    def test_json_passes_the_servers_answer_through(self, parser, client, capsys):
        with patch("popcorn_core.operations.create_conversation", return_value=_existing()):
            _run(parser, ["--json", "project", "create", "example-my-channel", "--if-not-exists"])

        out = json.loads(capsys.readouterr().out)
        payload = out["data"]
        assert payload["already_existed"] is True
        assert payload["conversation"]["id"] == EXISTING_ID

    def test_a_different_type_is_called_out(self, parser, client, capsys):
        """The server matches on name alone, whatever type was asked for."""
        with patch(
            "popcorn_core.operations.create_conversation",
            return_value=_existing(type="public_channel"),
        ):
            _run(
                parser,
                [
                    "project",
                    "create",
                    "example-my-channel",
                    "--type",
                    "private_channel",
                    "--if-not-exists",
                ],
            )

        err = capsys.readouterr().err
        assert "public_channel" in err
        assert "private_channel" in err

    def test_an_archived_channel_is_called_out(self, parser, client, capsys):
        with patch(
            "popcorn_core.operations.create_conversation",
            return_value=_existing(is_archived=True),
        ):
            _run(parser, ["project", "create", "example-my-channel", "--if-not-exists"])

        captured = capsys.readouterr()
        assert "archived" in captured.err
        assert "Already exists" in captured.out

    def test_a_template_that_was_not_installed_is_called_out(self, parser, client, capsys):
        with patch("popcorn_core.operations.create_conversation", return_value=_existing()):
            _run(
                parser,
                [
                    "project",
                    "create",
                    "example-my-channel",
                    "--app",
                    "example-template",
                    "--if-not-exists",
                ],
            )

        assert "example-template was not installed" in capsys.readouterr().err

    def test_members_that_were_not_added_are_called_out(self, parser, client, capsys):
        with patch(
            "popcorn_core.operations.create_conversation",
            return_value=_existing(type="private_channel"),
        ):
            _run(
                parser,
                [
                    "project",
                    "create",
                    "example-my-channel",
                    "--type",
                    "private_channel",
                    "--members",
                    "00000000-0000-4000-8000-000000000003",
                    "--if-not-exists",
                ],
            )

        assert "--members were not added" in capsys.readouterr().err

    def test_workspace_channel_members_are_noted_once(self, parser, client, capsys):
        """The up-front "ignored" note already covers an existing workspace
        project; a second "not added" note would say the same thing twice."""
        with patch("popcorn_core.operations.create_conversation", return_value=_existing()):
            _run(
                parser,
                [
                    "project",
                    "create",
                    "example-my-channel",
                    "--members",
                    "00000000-0000-4000-8000-000000000003",
                    "--if-not-exists",
                ],
            )

        err = capsys.readouterr().err
        assert "--members is ignored" in err
        assert "--members were not added" not in err

    def test_a_newly_created_channel_gets_no_notes(self, parser, client, capsys):
        """The notes describe an existing project; a fresh create did apply them."""
        with patch("popcorn_core.operations.create_conversation", return_value=_created()):
            _run(
                parser,
                [
                    "project",
                    "create",
                    "example-fresh",
                    "--app",
                    "example-template",
                    "--if-not-exists",
                ],
            )

        captured = capsys.readouterr()
        assert "Created" in captured.out
        assert captured.err == ""

    def test_notes_stay_off_stdout_under_json(self, parser, client, capsys):
        with patch(
            "popcorn_core.operations.create_conversation",
            return_value=_existing(is_archived=True),
        ):
            _run(parser, ["--json", "project", "create", "example-my-channel", "--if-not-exists"])

        captured = capsys.readouterr()
        json.loads(captured.out)
        assert "archived" in captured.err


class TestDefaultType:
    """With no `--type`, a project is a workspace project, as the web client
    makes it: every workspace member is in it, including anyone who joins
    later. It used to be a public project, which held only its creator."""

    MEMBER = "00000000-0000-4000-8000-000000000003"

    def test_no_type_creates_a_workspace_channel(self, parser, client):
        with patch(
            "popcorn_core.operations.create_conversation", return_value=_created()
        ) as create:
            _run(parser, ["project", "create", "example-fresh"])

        assert create.call_args.kwargs["conv_type"] == "workspace_channel"

    def test_the_library_default_matches(self):
        c = MagicMock()
        operations.create_conversation(c, "example-fresh")
        assert c.post.call_args.kwargs["data"]["conversation_type"] == "workspace_channel"

    @pytest.mark.parametrize("conv_type", ["public_channel", "private_channel"])
    def test_an_explicit_type_is_sent_as_given(self, parser, client, conv_type):
        with patch(
            "popcorn_core.operations.create_conversation", return_value=_created()
        ) as create:
            _run(parser, ["project", "create", "example-fresh", "--type", conv_type])

        assert create.call_args.kwargs["conv_type"] == conv_type

    def test_members_on_a_workspace_channel_are_noted_not_sent(self, parser, client, capsys):
        """The server replaces the list with everyone, so it is not an error —
        but the caller should not think the list chose the members."""
        with patch(
            "popcorn_core.operations.create_conversation", return_value=_created()
        ) as create:
            _run(parser, ["project", "create", "example-fresh", "--members", self.MEMBER])

        captured = capsys.readouterr()
        assert create.call_args.kwargs["member_ids"] is None
        assert "--members is ignored" in captured.err
        assert "Created" in captured.out

    def test_the_note_stays_off_stdout_under_json(self, parser, client, capsys):
        with patch("popcorn_core.operations.create_conversation", return_value=_created()):
            _run(
                parser,
                ["--json", "project", "create", "example-fresh", "--members", self.MEMBER],
            )

        captured = capsys.readouterr()
        json.loads(captured.out)
        assert "--members is ignored" in captured.err

    @pytest.mark.parametrize("conv_type", ["public_channel", "private_channel"])
    def test_members_on_other_types_are_sent_silently(self, parser, client, capsys, conv_type):
        with patch(
            "popcorn_core.operations.create_conversation", return_value=_created()
        ) as create:
            _run(
                parser,
                [
                    "project",
                    "create",
                    "example-fresh",
                    "--type",
                    conv_type,
                    "--members",
                    self.MEMBER,
                ],
            )

        assert create.call_args.kwargs["member_ids"] == [self.MEMBER]
        assert capsys.readouterr().err == ""


class TestDuplicate:
    def test_a_name_held_elsewhere_fails_and_says_why(self, parser, client):
        """The server returns only a project the caller is a member of; any
        other holder of the name is still a duplicate."""
        with (
            patch(
                "popcorn_core.operations.create_conversation",
                side_effect=_error("already_exists"),
            ),
            pytest.raises(APIError) as exc,
        ):
            _run(parser, ["project", "create", "example-my-channel", "--if-not-exists"])

        assert exc.value.status_code == 400
        assert exc.value.hint and "not a member" in exc.value.hint

    def test_another_400_gets_no_duplicate_hint(self, parser, client):
        with (
            patch(
                "popcorn_core.operations.create_conversation",
                side_effect=_error("invalid_request"),
            ),
            pytest.raises(APIError) as exc,
        ):
            _run(parser, ["project", "create", "example-fresh", "--if-not-exists"])

        assert exc.value.hint is None

    def test_without_the_flag_a_duplicate_fails_plainly(self, parser, client):
        with (
            patch(
                "popcorn_core.operations.create_conversation",
                side_effect=_error("already_exists"),
            ),
            pytest.raises(APIError) as exc,
        ):
            _run(parser, ["project", "create", "example-my-channel"])

        assert exc.value.hint is None
