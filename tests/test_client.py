"""APIClient response handling."""

from __future__ import annotations

import httpx
import pytest

from popcorn_core.client import APIClient
from popcorn_core.errors import APIError


@pytest.fixture()
def client(profile) -> APIClient:
    return APIClient(profile)


def _respond(monkeypatch, client: APIClient, resp: httpx.Response) -> None:
    monkeypatch.setattr(client, "_do_request", lambda *a, **kw: resp)


class TestNoContentResponses:
    """204/empty bodies must not be mistaken for malformed JSON.

    The data-store DELETE endpoints (records, scalars) answer 204 with no
    body; `resp.json()` on that raises, so without a guard every delete
    surfaces as "Invalid JSON in response".
    """

    def test_204_returns_empty_dict(self, monkeypatch, client):
        _respond(monkeypatch, client, httpx.Response(204))
        assert client.delete("/api/v1/conversations/c/data-store/scalars/k") == {}

    def test_200_with_empty_body_returns_empty_dict(self, monkeypatch, client):
        _respond(monkeypatch, client, httpx.Response(200, content=b""))
        assert client.get("/api/anything") == {}

    def test_200_with_json_body_still_parses(self, monkeypatch, client):
        _respond(monkeypatch, client, httpx.Response(200, json={"ok": True, "n": 1}))
        assert client.get("/api/anything") == {"ok": True, "n": 1}

    def test_200_with_non_json_body_still_raises(self, monkeypatch, client):
        _respond(monkeypatch, client, httpx.Response(200, content=b"<html>nope</html>"))
        with pytest.raises(APIError, match="Invalid JSON in response"):
            client.get("/api/anything")


class TestStructuredIssueLists:
    """A 400 whose detail carries an `issues` list must not collapse to its
    error slug.

    The flow endpoints answer
    `400 {"detail": {"ok": false, "error": "flow_validation_failed",
    "issues": [...]}}`. The issue list IS the diagnostic — an author who sees
    only "flow_validation_failed" has nothing to act on.
    """

    def test_issues_are_preserved_in_the_message(self, monkeypatch, client):
        _respond(
            monkeypatch,
            client,
            httpx.Response(
                400,
                json={
                    "detail": {
                        "ok": False,
                        "error": "flow_validation_failed",
                        "issues": [
                            "alert_webhook.yaml steps[0](post).args.text: missing required arg",
                            "alert_webhook.yaml steps[1](x): reference to unknown step: nope",
                        ],
                    }
                },
            ),
        )
        with pytest.raises(APIError) as exc:
            client.post("/api/customer-flows/run", data={})
        msg = str(exc.value)
        assert "flow_validation_failed" in msg
        assert "missing required arg" in msg, "the issue list was swallowed"
        assert "reference to unknown step: nope" in msg

    def test_issues_survive_into_the_json_envelope(self, monkeypatch, client):
        _respond(
            monkeypatch,
            client,
            httpx.Response(
                400,
                json={"detail": {"error": "flow_validation_failed", "issues": ["boom"]}},
            ),
        )
        with pytest.raises(APIError) as exc:
            client.post("/api/customer-flows/run", data={})
        assert "boom" in exc.value.to_dict()["error"]

    def test_a_detail_dict_without_issues_is_unchanged(self, monkeypatch, client):
        _respond(monkeypatch, client, httpx.Response(400, json={"detail": {"error": "nope"}}))
        with pytest.raises(APIError) as exc:
            client.post("/api/x", data={})
        assert str(exc.value) == "nope"

    def test_an_empty_issues_list_adds_nothing(self, monkeypatch, client):
        _respond(
            monkeypatch,
            client,
            httpx.Response(400, json={"detail": {"error": "nope", "issues": []}}),
        )
        with pytest.raises(APIError) as exc:
            client.post("/api/x", data={})
        assert str(exc.value) == "nope"
