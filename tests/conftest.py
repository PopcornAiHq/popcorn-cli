"""Shared test fixtures."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from popcorn_core.config import Profile


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
