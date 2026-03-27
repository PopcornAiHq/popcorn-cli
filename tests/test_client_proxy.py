"""Tests for proxy mode in APIClient."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from popcorn_core.client import APIClient
from popcorn_core.config import Profile


@pytest.fixture()
def proxy_env():
    """Env vars for proxy mode."""
    env = {
        "POPCORN_PROXY_MODE": "1",
        "POPCORN_API_URL": "http://sidecar:8091/popcorn",
        "POPCORN_WORKSPACE_ID": "ws-1234",
        "POPCORN_USER_ID": "user-5678",
    }
    with patch.dict(os.environ, env, clear=False):
        yield env


def test_proxy_mode_token_returns_none(proxy_env):
    """In proxy mode, _token() returns None — no auth token needed."""
    profile = Profile(api_url="http://sidecar:8091/popcorn", workspace_id="ws-1234")
    client = APIClient(profile)
    assert client._token() is None


def test_proxy_mode_headers_no_authorization(proxy_env):
    """In proxy mode, headers omit Authorization and include X-Actor-User-ID."""
    profile = Profile(api_url="http://sidecar:8091/popcorn", workspace_id="ws-1234")
    client = APIClient(profile)
    headers = client._headers()
    assert "Authorization" not in headers
    assert headers["Content-Type"] == "application/json"
    assert headers["X-Actor-User-ID"] == "user-5678"


def test_proxy_mode_headers_include_workspace(proxy_env):
    """In proxy mode, headers include X-Workspace-ID."""
    profile = Profile(api_url="http://sidecar:8091/popcorn", workspace_id="ws-1234")
    client = APIClient(profile)
    headers = client._headers()
    assert headers["X-Workspace-ID"] == "ws-1234"


def test_normal_mode_unchanged():
    """Without POPCORN_PROXY_MODE, client behaves normally."""
    env = {"POPCORN_PROXY_MODE": ""}
    with patch.dict(os.environ, env, clear=False):
        profile = Profile(
            api_url="https://api.popcorn.ai",
            id_token="test-token",
            expires_at=9999999999,
            workspace_id="ws-1234",
        )
        client = APIClient(profile)
        headers = client._headers()
        assert "Authorization" in headers
        assert headers["Authorization"] == "Bearer test-token"
        assert "X-Actor-User-ID" not in headers
