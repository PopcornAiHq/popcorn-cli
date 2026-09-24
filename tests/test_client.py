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


class TestNullFieldsInTheErrorEnvelope:
    """A field present but null must fall through to the next candidate.

    The backend's `ErrorResponse` declares `detail: Optional[str] = None`, so a
    structured error serialises as `{"ok": false, "error": "not_found",
    "detail": null}`. `dict.get(key, default)` returns that `None` rather than
    the default, which printed the literal string "None" in place of the code
    the server sent — on the API's most common 404.
    """

    def test_a_null_inner_detail_falls_through_to_the_error_code(self, monkeypatch, client):
        _respond(
            monkeypatch,
            client,
            httpx.Response(
                404, json={"detail": {"ok": False, "error": "not_found", "detail": None}}
            ),
        )
        with pytest.raises(APIError) as exc:
            client.get("/api/conversations/info")
        assert str(exc.value) == "not_found"

    def test_a_null_inner_detail_keeps_the_issue_list(self, monkeypatch, client):
        _respond(
            monkeypatch,
            client,
            httpx.Response(
                400,
                json={
                    "detail": {
                        "ok": False,
                        "error": "flow_validation_failed",
                        "detail": None,
                        "issues": ["boom"],
                    }
                },
            ),
        )
        with pytest.raises(APIError) as exc:
            client.post("/api/customer-flows/run", data={})
        assert str(exc.value) == "flow_validation_failed\n  - boom"

    def test_a_null_top_level_detail_falls_through_to_the_error_code(self, monkeypatch, client):
        _respond(
            monkeypatch,
            client,
            httpx.Response(404, json={"ok": False, "error": "not_found", "detail": None}),
        )
        with pytest.raises(APIError) as exc:
            client.get("/api/conversations/info")
        assert str(exc.value) == "not_found"


_SIDECAR_ENV = {
    "POPCORN_PROXY_MODE": "1",
    "POPCORN_USER_ID": "00000000-0000-4000-8000-000000000001",
    "POPCORN_WORKSPACE_ID": "00000000-0000-4000-8000-000000000002",
    "POPCORN_TASK_TOKEN": "example-task-token",
}


class TestSidecarEnvVarsAreIgnored:
    """The environment variables that once switched the client into an
    unauthenticated sidecar mode no longer change what is sent: every request
    carries the profile's bearer token and nothing self-asserted."""

    def test_headers_are_bearer_only(self, monkeypatch, client):
        for k, v in _SIDECAR_ENV.items():
            monkeypatch.setenv(k, v)
        assert client._headers() == {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {client.profile.id_token}",
        }

    def test_401_still_refreshes(self, monkeypatch, client):
        monkeypatch.setenv("POPCORN_PROXY_MODE", "1")
        responses = iter([httpx.Response(401), httpx.Response(200, json={"ok": True})])
        monkeypatch.setattr(client, "_do_request", lambda *a, **kw: next(responses))
        refreshed: list[bool] = []
        monkeypatch.setattr(client, "_refresh_token", lambda: refreshed.append(True))
        assert client.get("/api/anything") == {"ok": True}
        assert refreshed == [True]
