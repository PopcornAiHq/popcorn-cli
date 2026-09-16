"""`popcorn webhook` lifecycle: get, update, delete, override rules.

The parser-level assertions for the older subcommands live in `test_parser.py`;
this module is about what the lifecycle half *does* — which requests it sends,
what it refuses locally, and what it prints when the server quietly disagrees.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from popcorn_cli.cli import build_parser
from popcorn_cli.registry import dispatch
from popcorn_core import operations
from popcorn_core.errors import PopcornError

WEBHOOK_ID = "11111111-2222-3333-4444-555555555555"
# A channel UUID rather than "#name": these tests exercise the webhook lookup,
# and a name would first spend the mocked GET on resolving the channel.
CHANNEL_ID = "00000000-0000-4000-8000-000000000001"

_HOOK = {
    "id": WEBHOOK_ID,
    "name": "Intake",
    "is_active": True,
    "enforce_hmac": False,
    "action_mode": "ai_enhanced",
    "url": "https://hooks.example.test/ingest/s3cr3t-token",
}


@pytest.fixture()
def parser():
    return build_parser()


@pytest.fixture()
def client():
    """A stand-in API client, patched in wherever a handler builds one."""
    c = MagicMock()
    with patch("popcorn_cli.cli._get_client", return_value=c):
        yield c


def _run(parser, argv):
    dispatch(parser.parse_args(argv))


class TestGet:
    def test_reads_the_webhook_by_id(self, parser, client, capsys):
        with patch("popcorn_core.operations.get_webhook", return_value={"webhook": _HOOK}) as get:
            _run(parser, ["webhook", "get", WEBHOOK_ID])
        assert get.call_args[0][1] == WEBHOOK_ID
        out = capsys.readouterr().out
        assert "Intake" in out
        assert "ai_enhanced" in out

    def test_hides_the_ingest_url_by_default(self, parser, client, capsys):
        """The token in the URL is the credential — reading a name must not leak it."""
        with patch("popcorn_core.operations.get_webhook", return_value={"webhook": _HOOK}):
            _run(parser, ["webhook", "get", WEBHOOK_ID])
        out = capsys.readouterr().out
        assert "s3cr3t-token" not in out
        assert "--show-url" in out

    def test_show_url_prints_it(self, parser, client, capsys):
        with patch("popcorn_core.operations.get_webhook", return_value={"webhook": _HOOK}):
            _run(parser, ["webhook", "get", WEBHOOK_ID, "--show-url"])
        assert "s3cr3t-token" in capsys.readouterr().out

    def test_a_name_resolves_through_the_channel(self, parser, client, capsys):
        with (
            patch(
                "popcorn_core.operations.list_webhooks",
                return_value={"webhooks": [_HOOK]},
            ),
            patch("popcorn_core.operations.get_webhook", return_value={"webhook": _HOOK}) as get,
        ):
            _run(parser, ["webhook", "get", "Intake", "--channel", "#ops"])
        assert get.call_args[0][1] == WEBHOOK_ID


class TestUpdate:
    def test_sends_only_the_named_fields(self, parser, client):
        """A PATCH leaves out what it is not given; sending None would clear it."""
        with patch(
            "popcorn_core.operations.update_webhook", return_value={"webhook": _HOOK}
        ) as update:
            _run(parser, ["webhook", "update", WEBHOOK_ID, "--name", "Renamed"])
        sent = update.call_args.kwargs
        assert sent["name"] == "Renamed"
        assert sent["description"] is None
        assert sent["is_active"] is None

    def test_deactivate_is_distinct_from_unset(self, parser, client):
        with patch(
            "popcorn_core.operations.update_webhook", return_value={"webhook": _HOOK}
        ) as update:
            _run(parser, ["webhook", "update", WEBHOOK_ID, "--deactivate"])
        assert update.call_args.kwargs["is_active"] is False

    def test_no_fields_is_refused_locally(self, parser, client):
        """An empty PATCH would succeed and change nothing — say so instead."""
        with pytest.raises(PopcornError) as exc:
            _run(parser, ["webhook", "update", WEBHOOK_ID])
        assert exc.value.error_code == "validation"
        assert "--activate" in str(exc.value)

    def test_contradictory_flags_are_refused(self, parser, client):
        with pytest.raises(PopcornError) as exc:
            _run(parser, ["webhook", "update", WEBHOOK_ID, "--activate", "--deactivate"])
        assert exc.value.error_code == "validation"

    def test_enforce_hmac_that_did_not_take_is_reported(self, parser, client, capsys):
        """The server writes enforcement back as False when there is no secret.

        It answers 200 either way, so a caller who does not read the body
        believes signature verification is on when nothing is verifying.
        """
        with patch(
            "popcorn_core.operations.update_webhook",
            return_value={"webhook": {**_HOOK, "enforce_hmac": False}},
        ):
            _run(parser, ["webhook", "update", WEBHOOK_ID, "--enforce-hmac"])
        out = capsys.readouterr().out
        assert "did NOT take" in out
        assert "has to be created" in out

    def test_enforce_hmac_that_took_says_nothing_extra(self, parser, client, capsys):
        with patch(
            "popcorn_core.operations.update_webhook",
            return_value={"webhook": {**_HOOK, "enforce_hmac": True}},
        ):
            _run(parser, ["webhook", "update", WEBHOOK_ID, "--enforce-hmac"])
        assert "did NOT take" not in capsys.readouterr().out

    def test_there_is_no_flow_rebinding_flag(self, parser):
        """A webhook's flow is fixed at creation; the server rejects the id form."""
        with pytest.raises(SystemExit):
            parser.parse_args(["webhook", "update", WEBHOOK_ID, "--trigger-flow-id", WEBHOOK_ID])


class TestDelete:
    def test_prompts_before_deleting(self, parser, client, tty):
        prompts = tty("n")
        with (
            patch("popcorn_core.operations.get_webhook", return_value={"webhook": _HOOK}),
            patch("popcorn_core.operations.delete_webhook") as delete,
        ):
            _run(parser, ["webhook", "delete", WEBHOOK_ID])
        delete.assert_not_called()
        # The prompt names the webhook, not just its id: a UUID tells the
        # reader nothing about what stops receiving deliveries.
        assert "Intake" in prompts[0]

    def test_yes_skips_the_prompt(self, parser, client):
        with (
            patch("popcorn_core.operations.get_webhook", return_value={"webhook": _HOOK}),
            patch("popcorn_core.operations.delete_webhook") as delete,
        ):
            _run(parser, ["--yes", "webhook", "delete", WEBHOOK_ID])
        assert delete.call_args[0][1] == WEBHOOK_ID

    def test_non_interactive_without_yes_refuses(self, parser, client):
        """pytest's stdin is not a TTY — the same branch a script or agent hits."""
        with (
            patch("popcorn_core.operations.get_webhook", return_value={"webhook": _HOOK}),
            patch("popcorn_core.operations.delete_webhook") as delete,
            pytest.raises(PopcornError) as exc,
        ):
            _run(parser, ["webhook", "delete", WEBHOOK_ID])
        delete.assert_not_called()
        assert exc.value.error_code == "validation"


class TestDeliveriesLimit:
    """A migrated argument has to keep the default its hand-written form had.

    `registry.Argument` had no `default`, so `--limit` parsed to None, and
    `None` reached the query string as an empty value — the server answered
    `query.limit: Input should be a valid integer`. Every sibling operation
    happened to guard its optional params, so `webhook deliveries` was the one
    command that broke.
    """

    def test_limit_defaults_rather_than_parsing_to_none(self, parser):
        args = parser.parse_args(["webhook", "deliveries", "#ops"])
        assert args.limit == 50

    def test_an_absent_limit_is_left_off_the_wire(self):
        client = MagicMock()
        operations.list_webhook_deliveries(client, CHANNEL_ID, limit=None)
        assert "limit" not in client.get.call_args[0][1]

    def test_a_given_limit_is_sent(self):
        client = MagicMock()
        operations.list_webhook_deliveries(client, CHANNEL_ID, limit=10)
        assert client.get.call_args[0][1]["limit"] == 10


class TestOverrideRules:
    def test_get_renders_each_pattern(self, parser, client, capsys):
        rules = {"issues.opened": {"ignore": True}}
        with patch(
            "popcorn_core.operations.get_webhook_override_rules",
            return_value={"rules": rules},
        ):
            _run(parser, ["webhook", "override-rules", "get", WEBHOOK_ID])
        out = capsys.readouterr().out
        assert "issues.opened" in out
        assert "ignore: True" in out

    def test_get_with_no_rules_says_defaults_apply(self, parser, client, capsys):
        with patch(
            "popcorn_core.operations.get_webhook_override_rules", return_value={"rules": {}}
        ):
            _run(parser, ["webhook", "override-rules", "get", WEBHOOK_ID])
        assert "provider's defaults" in capsys.readouterr().out

    def test_set_sends_the_parsed_object(self, parser, client):
        rules = {"push.*": {"skip_llm": True}}
        with patch(
            "popcorn_core.operations.set_webhook_override_rules",
            return_value={"rules": rules},
        ) as put:
            _run(parser, ["webhook", "override-rules", "set", WEBHOOK_ID, json.dumps(rules)])
        assert put.call_args[0][2] == rules

    def test_set_reads_a_file(self, parser, client, tmp_path):
        path = tmp_path / "rules.json"
        path.write_text('{"issues.*": {"ignore": true}}')
        with patch(
            "popcorn_core.operations.set_webhook_override_rules", return_value={"rules": {}}
        ) as put:
            _run(parser, ["webhook", "override-rules", "set", WEBHOOK_ID, f"@{path}"])
        assert put.call_args[0][2] == {"issues.*": {"ignore": True}}

    def test_bad_json_is_a_validation_error(self, parser, client):
        with pytest.raises(PopcornError) as exc:
            _run(parser, ["webhook", "override-rules", "set", WEBHOOK_ID, "not-json"])
        assert exc.value.error_code == "validation"


class TestResolveWebhookId:
    def test_a_uuid_costs_no_request(self):
        client = MagicMock()
        assert operations.resolve_webhook_id(client, WEBHOOK_ID) == WEBHOOK_ID
        client.get.assert_not_called()

    def test_a_name_resolves_through_the_listing(self):
        client = MagicMock()
        client.get.return_value = {"webhooks": [_HOOK]}
        assert operations.resolve_webhook_id(client, "Intake", CHANNEL_ID) == WEBHOOK_ID

    def test_a_name_without_a_channel_says_why(self):
        """Names are unique per channel, so there is nothing to match against."""
        client = MagicMock()
        with pytest.raises(PopcornError) as exc:
            operations.resolve_webhook_id(client, "Intake")
        assert exc.value.error_code == "validation"
        assert "channel" in str(exc.value).lower()
        client.get.assert_not_called()

    def test_an_unknown_name_lists_what_exists(self):
        client = MagicMock()
        client.get.return_value = {"webhooks": [_HOOK]}
        with pytest.raises(PopcornError) as exc:
            operations.resolve_webhook_id(client, "Nope", CHANNEL_ID)
        assert exc.value.error_code == "not_found"
        assert "Intake" in str(exc.value)


class TestSendByUuid:
    def test_a_uuid_target_needs_no_channel(self):
        """`send` addresses a webhook the same way the lifecycle commands do."""
        client = MagicMock()
        client.get.return_value = {"webhook": _HOOK}
        url = operations.resolve_webhook_url(client, WEBHOOK_ID)
        assert url == _HOOK["url"]
        assert client.get.call_args[0][0] == f"/api/webhooks/{WEBHOOK_ID}"


class TestRequests:
    """The routes each operation calls, pinned so a rename is caught here."""

    def test_update_patches_the_webhook(self):
        client = MagicMock()
        operations.update_webhook(client, WEBHOOK_ID, name="x")
        assert client.patch.call_args[0][0] == f"/api/webhooks/{WEBHOOK_ID}"
        assert client.patch.call_args.kwargs["data"] == {"name": "x"}

    def test_delete_deletes_the_webhook(self):
        client = MagicMock()
        operations.delete_webhook(client, WEBHOOK_ID)
        assert client.delete.call_args[0][0] == f"/api/webhooks/{WEBHOOK_ID}"

    def test_override_rules_put_wraps_the_rules(self):
        client = MagicMock()
        operations.set_webhook_override_rules(client, WEBHOOK_ID, {"a.b": {"ignore": True}})
        assert client.put.call_args[0][0] == f"/api/webhooks/{WEBHOOK_ID}/override-rules"
        assert client.put.call_args.kwargs["data"] == {"rules": {"a.b": {"ignore": True}}}
