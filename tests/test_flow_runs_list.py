"""`flow runs list --flow` — one flow's runs, and the flow name on every line.

The filter is the server's: it narrows the query that selects a page, so a
page stays `--limit` long and the cursor stays exact. What the CLI owns is
sending it on every page, refusing an answer from an API that ignored it, and
turning the server's rejections into errors a person can act on.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import MagicMock

import httpx
import pytest

from popcorn_core import operations
from popcorn_core.client import APIClient
from popcorn_core.errors import APIError, PopcornError

_PATH = "/api/customer-flow-runs/list"


def _run(flow_name, workflow_id="wf-1", **extra):
    return {
        "workflow_id": workflow_id,
        "status": "Running",
        "workflow_type": "InterpreterWorkflow",
        "start_time": "2026-01-01T00:00:00Z",
        "flow_name": flow_name,
        **extra,
    }


@pytest.fixture
def mock_client(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(operations, "resolve_conversation", lambda c, conv: "conv-1")
    return client


class TestOperation:
    def test_flow_name_is_sent(self, mock_client):
        mock_client.get.return_value = {"executions": [_run("claim_turn")]}
        operations.list_flow_runs(mock_client, "#ops", flow_name="claim_turn")
        mock_client.get.assert_called_once_with(
            _PATH, {"conversation_id": "conv-1", "limit": 50, "flow_name": "claim_turn"}
        )

    def test_flow_name_rides_along_with_the_page_token(self, mock_client):
        """The server needs the same name on every page; a cursor alone would
        continue an unfiltered sequence."""
        mock_client.get.return_value = {"executions": []}
        operations.list_flow_runs(
            mock_client, "#ops", status="failed", page_token="tok2", flow_name="claim_turn"
        )
        params = mock_client.get.call_args.args[1]
        assert params["page_token"] == "tok2"
        assert params["flow_name"] == "claim_turn"
        assert params["status"] == "failed"

    def test_no_flow_sends_no_param(self, mock_client):
        mock_client.get.return_value = {"executions": [_run("a"), _run("b")]}
        resp = operations.list_flow_runs(mock_client, "#ops")
        assert "flow_name" not in mock_client.get.call_args.args[1]
        assert len(resp["executions"]) == 2

    @pytest.mark.parametrize("name", ["", "   "])
    def test_a_blank_name_is_refused_before_any_request(self, mock_client, name):
        """Dropping it instead would quietly list every flow's runs."""
        with pytest.raises(PopcornError) as exc:
            operations.list_flow_runs(mock_client, "#ops", flow_name=name)
        assert exc.value.error_code == "validation"
        mock_client.get.assert_not_called()


class TestOldServerGuard:
    """An API that predates the filter ignores it and returns every flow's
    runs. Presenting that as the filtered list would be a wrong answer."""

    def test_a_run_of_another_flow_is_refused(self, mock_client):
        mock_client.get.return_value = {
            "executions": [_run("claim_turn"), _run("digest", "wf-2"), _run("alert", "wf-3")],
            "count": 3,
        }
        with pytest.raises(PopcornError) as exc:
            operations.list_flow_runs(mock_client, "#ops", flow_name="claim_turn")
        assert exc.value.error_code == "validation"
        assert "ignored --flow 'claim_turn'" in str(exc.value)
        assert "alert, digest" in str(exc.value)
        assert exc.value.hint

    def test_a_run_with_no_flow_name_is_refused(self, mock_client):
        """A filtering server matches on the name, so it can never return a
        run that has none."""
        mock_client.get.return_value = {"executions": [_run(None)]}
        with pytest.raises(PopcornError) as exc:
            operations.list_flow_runs(mock_client, "#ops", flow_name="claim_turn")
        assert "<none>" in str(exc.value)

    def test_a_page_of_only_that_flow_passes(self, mock_client):
        page = {"executions": [_run("claim_turn"), _run("claim_turn", "wf-2")], "count": 2}
        mock_client.get.return_value = page
        assert operations.list_flow_runs(mock_client, "#ops", flow_name="claim_turn") == page

    def test_an_empty_page_passes(self, mock_client):
        mock_client.get.return_value = {"executions": [], "count": 0}
        resp = operations.list_flow_runs(mock_client, "#ops", flow_name="claim_turn")
        assert resp["count"] == 0

    def test_unfiltered_lists_are_not_checked(self, mock_client):
        mock_client.get.return_value = {"executions": [_run("a"), _run(None, "wf-2")]}
        operations.list_flow_runs(mock_client, "#ops")


class TestServerRejections:
    """The two ways the server refuses a name, through the real client."""

    @pytest.fixture
    def api(self, profile, monkeypatch):
        client = APIClient(profile)
        monkeypatch.setattr(operations, "resolve_conversation", lambda c, conv: "conv-1")
        return client

    def _respond(self, monkeypatch, client, resp):
        monkeypatch.setattr(client, "_do_request", lambda *a, **kw: resp)

    def test_invalid_flow_name_carries_the_rule_as_a_hint(self, api, monkeypatch):
        self._respond(
            monkeypatch,
            api,
            httpx.Response(
                400,
                json={
                    "detail": {
                        "ok": False,
                        "error": "invalid_flow_name",
                        "detail": "invalid flow_name 'a\"b'",
                    }
                },
            ),
        )
        with pytest.raises(APIError) as exc:
            operations.list_flow_runs(api, "#ops", flow_name='a"b')
        assert str(exc.value) == "invalid flow_name 'a\"b'"
        assert exc.value.hint and "double quote" in exc.value.hint
        assert exc.value.error_code == "client_error"
        assert exc.value.to_dict()["hint"] == exc.value.hint

    def test_other_400s_get_no_flow_hint(self, api, monkeypatch):
        self._respond(
            monkeypatch,
            api,
            httpx.Response(
                400,
                json={"detail": {"error": "invalid_page_token", "detail": "invalid page_token"}},
            ),
        )
        with pytest.raises(APIError) as exc:
            operations.list_flow_runs(api, "#ops", page_token="junk", flow_name="claim_turn")
        assert exc.value.hint is None

    def test_a_422_renders_the_field_and_reason(self, api, monkeypatch):
        self._respond(
            monkeypatch,
            api,
            httpx.Response(
                422,
                json={
                    "detail": [
                        {
                            "loc": ["query", "flow_name"],
                            "msg": "String should have at least 1 character",
                        }
                    ]
                },
            ),
        )
        with pytest.raises(APIError) as exc:
            api.get(_PATH, {"conversation_id": "conv-1", "flow_name": ""})
        assert str(exc.value) == "query.flow_name: String should have at least 1 character"


class TestDispatch:
    def _run_cli(self, monkeypatch, argv):
        from popcorn_cli import cli

        monkeypatch.setattr(cli, "_check_and_update", lambda: None)
        monkeypatch.setattr(cli, "_get_client", lambda args: object())
        monkeypatch.setattr(sys, "argv", ["popcorn", *argv])
        cli.main()

    def test_flow_reaches_the_operation_and_each_line_names_its_flow(self, monkeypatch, capsys):
        seen = {}

        def fake(client, conversation, **kw):
            seen.update(kw)
            return {"executions": [_run("claim_turn", task_queue="batch")], "count": 1}

        monkeypatch.setattr(operations, "list_flow_runs", fake)
        self._run_cli(
            monkeypatch, ["flow", "runs", "list", "--channel", "#ops", "--flow", "claim_turn"]
        )
        assert seen["flow_name"] == "claim_turn"
        out = capsys.readouterr().out
        assert "'claim_turn' runs in #ops (1):" in out
        assert "Running    claim_turn  wf-1  InterpreterWorkflow" in out
        assert "[batch]" in out

    def test_unfiltered_lines_name_each_flow_and_dash_a_missing_one(self, monkeypatch, capsys):
        monkeypatch.setattr(
            operations,
            "list_flow_runs",
            lambda client, conversation, **kw: {
                "executions": [_run("digest"), _run(None, "wf-old")],
                "count": 2,
            },
        )
        self._run_cli(monkeypatch, ["flow", "runs", "list", "--channel", "#ops"])
        out = capsys.readouterr().out
        assert "Flow runs in #ops (2):" in out
        assert " digest  wf-1 " in out
        assert " -  wf-old " in out
        assert "None" not in out

    def test_json_keeps_flow_name_and_the_next_page_resends_flow(self, monkeypatch, capsys):
        """Page two is the same command plus pagination.next — `--flow`
        included — so the filter holds across the whole sequence."""
        calls = []

        def fake(client, conversation, **kw):
            calls.append(kw)
            if kw.get("page_token") is None:
                return {"executions": [_run("claim_turn")], "count": 1, "next_page_token": "tok2"}
            return {"executions": [_run("claim_turn", "wf-2")], "count": 1}

        monkeypatch.setattr(operations, "list_flow_runs", fake)
        base = ["--json", "flow", "runs", "list", "--channel", "#ops", "--flow", "claim_turn"]
        self._run_cli(monkeypatch, base)
        first = json.loads(capsys.readouterr().out)["data"]
        assert first["executions"][0]["flow_name"] == "claim_turn"
        assert first["pagination"] == {"next": {"page-token": "tok2"}}

        nxt = [f"--{k}={v}" for k, v in first["pagination"]["next"].items()]
        self._run_cli(monkeypatch, [*base, *nxt])
        second = json.loads(capsys.readouterr().out)["data"]
        assert second["pagination"] == {"next": None}
        assert [c["flow_name"] for c in calls] == ["claim_turn", "claim_turn"]
        assert calls[1]["page_token"] == "tok2"

    def test_old_server_refusal_is_a_clean_error(self, monkeypatch, capsys):
        client = MagicMock()
        client.get.return_value = {"executions": [_run("digest")]}
        monkeypatch.setattr(operations, "resolve_conversation", lambda c, conv: "conv-1")
        from popcorn_cli import cli

        monkeypatch.setattr(cli, "_check_and_update", lambda: None)
        monkeypatch.setattr(cli, "_get_client", lambda args: client)
        monkeypatch.setattr(
            sys, "argv", ["popcorn", "flow", "runs", "list", "--channel", "#ops", "--flow", "x"]
        )
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code != 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.startswith("Error: The server ignored --flow 'x'")
        assert "Traceback" not in captured.err

    def test_invalid_flow_name_prints_message_and_hint(self, monkeypatch, capsys):
        def fake(client, conversation, **kw):
            err = APIError("invalid flow_name 'a\"b'", status_code=400)
            err.hint = "a flow name cannot contain a double quote or a backslash"
            raise err

        monkeypatch.setattr(operations, "list_flow_runs", fake)
        with pytest.raises(SystemExit):
            self._run_cli(
                monkeypatch, ["flow", "runs", "list", "--channel", "#ops", "--flow", 'a"b']
            )
        err = capsys.readouterr().err
        assert "Error: invalid flow_name 'a\"b'" in err
        assert "double quote" in err
