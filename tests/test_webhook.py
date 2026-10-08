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


# ---------------------------------------------------------------------------
# `send` keeps the ingest URL's token out of its output
# ---------------------------------------------------------------------------

_TOKEN = "s3cr3t-token"
_URL = _HOOK["url"]


@pytest.fixture()
def popcorn(monkeypatch, client, capsys):
    """Run the whole CLI, so error output is captured the way a user sees it.

    `httpx.post` is stubbed with whatever the test hands in — a response, or
    an exception to raise — which is the one boundary `send` crosses.
    """
    import sys

    import httpx

    from popcorn_cli import cli

    client.get.return_value = {"webhook": _HOOK}

    def _run(*argv: str, reply=None) -> tuple[int, str, str]:
        if reply is None:
            reply = httpx.Response(202, json={"status": "accepted", "request_id": "req-1"})
        post = MagicMock(side_effect=reply) if isinstance(reply, Exception) else None
        monkeypatch.setattr(cli, "_check_and_update", lambda: None)
        monkeypatch.setattr(sys, "argv", ["popcorn", "--no-color", *argv])
        with patch("popcorn_core.operations.httpx.post", post or MagicMock(return_value=reply)):
            try:
                cli.main()
                code = 0
            except SystemExit as exc:
                code = int(exc.code or 0)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run


class TestSendHidesTheUrl:
    """The reported leak: `HTTP 202 → https://…/ingest/<token>` on every send.

    `list` and `get` already hid the URL behind --show-url; `send` now follows
    them — on success and on every error path, which print whatever the flag.
    """

    def test_a_named_send_prints_the_webhook_not_its_url(self, popcorn):
        code, out, err = popcorn("webhook", "send", WEBHOOK_ID)
        assert code == 0
        assert _TOKEN not in out + err
        assert f"HTTP 202 → webhook 'Intake' ({WEBHOOK_ID})" in out
        assert "pass --show-url" in out
        assert "req-1" in out

    def test_a_url_target_prints_redacted(self, popcorn):
        code, out, err = popcorn("webhook", "send", _URL)
        assert code == 0
        assert _TOKEN not in out + err
        assert "HTTP 202 → https://hooks.example.test/ingest/…" in out

    def test_show_url_prints_it_for_a_name(self, popcorn):
        _, out, _ = popcorn("webhook", "send", WEBHOOK_ID, "--show-url")
        assert f"url: {_URL}" in out
        assert "pass --show-url" not in out

    def test_show_url_prints_it_for_a_url_target(self, popcorn):
        _, out, _ = popcorn("webhook", "send", _URL, "--show-url")
        assert f"HTTP 202 → {_URL}" in out

    def test_a_reply_echoing_the_url_is_scrubbed(self, popcorn):
        import httpx

        reply = httpx.Response(202, json={"received_at": _URL})
        _, out, _ = popcorn("webhook", "send", WEBHOOK_ID, reply=reply)
        assert _TOKEN not in out

    def test_json_carries_the_url_as_list_and_get_do(self, popcorn):
        """`--json` of `list` and `get` serves the record whole, URL included,
        and `send` follows that rather than starting a third rule."""
        _, out, _ = popcorn("--json", "webhook", "send", WEBHOOK_ID)
        data = json.loads(out)["data"]
        assert data["url"] == _URL
        assert data["webhook"] == {"id": WEBHOOK_ID, "name": "Intake"}
        assert data["status"] == 202

    @pytest.mark.parametrize("as_json", [False, True])
    def test_an_error_reply_is_scrubbed(self, popcorn, as_json):
        import httpx

        reply = httpx.Response(404, text=f"no such hook: {_URL}")
        flags = ["--json"] if as_json else []
        code, out, err = popcorn(*flags, "webhook", "send", WEBHOOK_ID, reply=reply)
        assert code != 0
        assert "HTTP 404" in out + err
        assert _TOKEN not in out + err

    @pytest.mark.parametrize("as_json", [False, True])
    def test_a_timeout_is_scrubbed(self, popcorn, as_json):
        import httpx

        flags = ["--json"] if as_json else []
        code, out, err = popcorn(
            *flags, "webhook", "send", _URL, reply=httpx.ReadTimeout("timed out")
        )
        assert code != 0
        assert "timed out for https://hooks.example.test/ingest/" in out + err
        assert _TOKEN not in out + err

    def test_a_network_error_naming_the_url_is_scrubbed(self, popcorn):
        import httpx

        code, out, err = popcorn(
            "webhook", "send", _URL, reply=httpx.ConnectError(f"cannot reach {_URL}")
        )
        assert code != 0
        assert "network error" in out + err
        assert _TOKEN not in out + err

    def test_an_invalid_port_is_a_validation_error_not_a_traceback(self, popcorn):
        import httpx

        url = "https://hooks.example.test:99999/ingest/s3cr3t-token"
        code, out, err = popcorn("webhook", "send", url, reply=httpx.InvalidURL("Invalid port"))
        assert code != 0
        assert "Not a usable ingest URL" in out + err
        assert _TOKEN not in out + err

    def test_an_invalid_url_is_a_validation_error_not_a_traceback(self, popcorn):
        import httpx

        code, out, err = popcorn("webhook", "send", _URL, reply=httpx.InvalidURL(f"bad: {_URL}"))
        assert code != 0
        assert "Not a usable ingest URL" in out + err
        assert _TOKEN not in out + err


class TestRedactWebhookUrl:
    def test_a_malformed_port_degrades_rather_than_raising(self):
        """It runs on the InvalidURL error path, where a ValueError would
        surface as a traceback."""
        assert operations.redact_webhook_url("https://hooks.example.test:99999/x/tok") == (
            "https://hooks.example.test/x/…"
        )
        assert operations.redact_webhook_url("https://[::1/x/tok") == "<ingest URL>/…"

    def test_scrubs_a_query_token_echoed_alone(self):
        url = "https://hooks.example.test/ingest?token=s3cr3t-query"
        assert operations.scrub_webhook_url("bad token s3cr3t-query", url) == "bad token …"

    def test_scrubs_userinfo_echoed_alone(self):
        url = "https://hookuser:s3cr3t-pass@hooks.example.test/ingest/x"
        out = operations.scrub_webhook_url("auth failed for hookuser / s3cr3t-pass", url)
        assert "s3cr3t-pass" not in out
        assert "hookuser" not in out

    def test_leaves_short_query_values_alone(self):
        url = "https://hooks.example.test/ingest/s3cr3t-token?v=1"
        assert operations.scrub_webhook_url("retry 1 of 3", url) == "retry 1 of 3"

    def test_drops_the_last_segment(self):
        assert operations.redact_webhook_url(_URL) == "https://hooks.example.test/ingest/…"

    def test_drops_userinfo_and_query_and_keeps_the_port(self):
        url = "https://user:pw@hooks.example.test:8443/ingest/tok?sig=abc"
        assert operations.redact_webhook_url(url) == "https://hooks.example.test:8443/ingest/…"


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
