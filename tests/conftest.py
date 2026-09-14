"""Shared test fixtures."""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from popcorn_core.config import Profile


@pytest.fixture()
def tty(monkeypatch):
    """Script an interactive confirmation prompt.

    pytest's own stdin is not a TTY, which is the branch `_confirm` and
    `_confirm_force` take to raise rather than hang — so a test of the
    PROMPT has to supply one. `prompts = tty("y")` installs a TTY stdin
    answering "y" and hands back the list each prompt string lands in, so a
    test can assert on what the user was actually asked.
    """

    def _install(reply: str) -> list[str]:
        prompts: list[str] = []

        class _Tty:
            def isatty(self) -> bool:
                return True

        monkeypatch.setattr(sys, "stdin", _Tty())
        monkeypatch.setattr("builtins.input", lambda p="": (prompts.append(p), reply)[1])
        return prompts

    return _install


@pytest.fixture()
def profile() -> Profile:
    """Authenticated profile for testing."""
    return Profile(
        api_url="https://api.test.popcorn.ai",
        clerk_issuer="https://clerk.test.popcorn.ai",
        clerk_client_id="test-client-id",
        id_token="test-id-token",
        access_token="test-access-token",
        refresh_token="test-refresh-token",
        email="test@popcorn.ai",
        expires_at=9999999999,
        workspace_id="ws-0000-0000-0000-000000000000",
        workspace_name="Test Workspace",
    )


@pytest.fixture()
def mock_client(profile: Profile) -> MagicMock:
    """Mock APIClient that returns controllable responses."""
    client = MagicMock()
    client.profile = profile
    return client
