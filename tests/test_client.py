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
