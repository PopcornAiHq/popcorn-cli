"""Tests for proxy mode in APIClient."""

from __future__ import annotations

import argparse
import os
from unittest.mock import patch

import pytest

from popcorn_cli.cli import _get_client
from popcorn_core.client import APIClient
from popcorn_core.config import Profile
from popcorn_core.errors import AuthError


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


# ---------------------------------------------------------------------------
# CLI _get_client() proxy mode tests
# ---------------------------------------------------------------------------


def test_get_client_proxy_mode_skips_auth(proxy_env, monkeypatch):
    """_get_client() in proxy mode constructs client without profile validation."""
    args = argparse.Namespace(env=None, workspace=None, timeout=None, debug=False)
    client = _get_client(args)
    assert client.profile.api_url == "http://sidecar:8091/popcorn"
    assert client.profile.workspace_id == "ws-1234"
    assert client.profile.id_token == ""  # No token needed


def test_get_client_normal_mode_requires_auth():
    """Without proxy mode, _get_client() raises on missing auth."""
    empty_profile = Profile(api_url="https://api.popcorn.ai")
    mock_cfg = type(
        "Cfg", (), {"default_profile": "dev", "active_profile": lambda self: empty_profile}
    )()
    env = {"POPCORN_PROXY_MODE": ""}
    with (
        patch.dict(os.environ, env, clear=False),
        patch("popcorn_cli.cli.load_config", return_value=mock_cfg),
    ):
        args = argparse.Namespace(env=None, workspace=None, timeout=None, debug=False)
        with pytest.raises(AuthError):
            _get_client(args)
