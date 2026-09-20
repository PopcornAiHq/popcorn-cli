"""`popcorn channel create --if-not-exists` — matching the name the server stores.

The flag's whole job is to be safe to re-run, and both ways it used to fail
looked the same from outside: the create it was supposed to skip went out
anyway and came back as a duplicate error.

One is deterministic — the server turns a space into a hyphen before it checks
for a collision, so a raw `"my channel"` never matched the `my-channel` it was
about to collide with. The other is the race branch, which keyed on a 409 the
create route does not send; it answers a duplicate with a 400 carrying an
`already_exists` slug.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from popcorn_cli.cli import build_parser
from popcorn_cli.registry import dispatch
from popcorn_core.errors import APIError

EXISTING_ID = "00000000-0000-4000-8000-000000000001"
CREATED_ID = "00000000-0000-4000-8000-000000000002"

_EXISTING = {"id": EXISTING_ID, "name": "example-my-channel"}


def _already_exists_error(status: int = 400) -> APIError:
    """The duplicate-name error as the create route actually sends it."""
    body = {
        "detail": {
            "ok": False,
            "error": "already_exists",
            "detail": "Channel already exists: example-my-channel",
        }
    }
    return APIError("Channel already exists", status_code=status, body=json.dumps(body))


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


class TestPreCheck:
    def test_a_name_with_spaces_matches_the_hyphenated_channel(self, parser, client, capsys):
        """The server would normalise this name onto the existing one."""
        with (
            patch(
                "popcorn_core.operations.search_channels",
                return_value={"conversations": [_EXISTING]},
            ) as search,
            patch("popcorn_core.operations.create_conversation") as create,
        ):
            _run(parser, ["channel", "create", "example-my channel", "--if-not-exists"])

        create.assert_not_called()
        # The listing filter is a substring match on the raw query, so the
        # normalised name has to reach it too — the raw one filters the
        # channel it is looking for out of the response.
        assert search.call_args[0][1] == "example-my-channel"
        out = capsys.readouterr().out
        assert "Already exists" in out
        assert EXISTING_ID in out

    def test_a_new_name_still_creates(self, parser, client, capsys):
        with (
            patch("popcorn_core.operations.search_channels", return_value={"conversations": []}),
            patch(
                "popcorn_core.operations.create_conversation",
                return_value={"conversation": {"id": CREATED_ID, "name": "example-fresh"}},
            ) as create,
        ):
            _run(parser, ["channel", "create", "example-fresh", "--if-not-exists"])

        create.assert_called_once()
        assert create.call_args.kwargs["name"] == "example-fresh"
        out = capsys.readouterr().out
        assert "Created" in out
        assert CREATED_ID in out

    def test_the_create_payload_keeps_the_name_as_typed(self, parser, client):
        """Normalisation is for comparing, not for rewriting the request."""
        with (
            patch("popcorn_core.operations.search_channels", return_value={"conversations": []}),
            patch(
                "popcorn_core.operations.create_conversation",
                return_value={"conversation": {"id": CREATED_ID, "name": "example-a-b"}},
            ) as create,
        ):
            _run(parser, ["channel", "create", "example-a b", "--if-not-exists"])

        assert create.call_args.kwargs["name"] == "example-a b"


class TestRace:
    def test_a_400_already_exists_returns_the_channel(self, parser, client, capsys):
        """Created between the pre-check and the create: report it, don't fail."""
        with (
            patch(
                "popcorn_core.operations.search_channels",
                side_effect=[{"conversations": []}, {"conversations": [_EXISTING]}],
            ),
            patch(
                "popcorn_core.operations.create_conversation",
                side_effect=_already_exists_error(400),
            ),
        ):
            _run(parser, ["channel", "create", "example-my channel", "--if-not-exists"])

        out = capsys.readouterr().out
        assert "Already exists" in out
        assert EXISTING_ID in out

    def test_it_holds_if_the_status_becomes_409(self, parser, client, capsys):
        """The slug is the key, so correcting the status server-side is a no-op here."""
        with (
            patch(
                "popcorn_core.operations.search_channels",
                side_effect=[{"conversations": []}, {"conversations": [_EXISTING]}],
            ),
            patch(
                "popcorn_core.operations.create_conversation",
                side_effect=_already_exists_error(409),
            ),
        ):
            _run(parser, ["channel", "create", "example-my-channel", "--if-not-exists"])

        assert "Already exists" in capsys.readouterr().out

    def test_another_400_is_not_swallowed(self, parser, client):
        """Only the duplicate slug is recoverable — a bad template is still a failure."""
        err = APIError(
            "Unknown template",
            status_code=400,
            body=json.dumps({"detail": {"ok": False, "error": "invalid_template"}}),
        )
        with (
            patch("popcorn_core.operations.search_channels", return_value={"conversations": []}),
            patch("popcorn_core.operations.create_conversation", side_effect=err),
            pytest.raises(APIError),
        ):
            _run(parser, ["channel", "create", "example-fresh", "--if-not-exists"])

    def test_without_the_flag_a_duplicate_still_fails(self, parser, client):
        with (
            patch(
                "popcorn_core.operations.create_conversation",
                side_effect=_already_exists_error(400),
            ),
            pytest.raises(APIError),
        ):
            _run(parser, ["channel", "create", "example-my-channel"])
