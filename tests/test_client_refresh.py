"""Token refresh writes back to its own profile slot, and only its tokens.

`popcorn -e dev <anything>` selects the "dev" profile in memory and never writes
that selection to disk. A refresh that keyed off the config file's
`default_profile` therefore wrote dev's credentials — endpoints and all — into
the "prod" slot, and the reverse direction put prod credentials in the dev slot.
Nothing downstream noticed: each clobbered slot stays internally consistent, so
the issuer gate in `APIClient._token` passes.
"""

from __future__ import annotations

import argparse

import httpx
import jwt
import pytest

from popcorn_cli.cli import _get_client
from popcorn_core.client import APIClient
from popcorn_core.config import Config, Profile, load_config, save_config

PROD_ISS = "https://clerk.popcorn.ai"
DEV_ISS = "https://clerk.dev.popcorn.ai"
_TEST_SIGNING_KEY = "x" * 32  # length only matters to silence PyJWT's key-length warning

_EXPIRED = 1
_VALID = 9999999999


def _jwt(iss: str, exp: int) -> str:
    return jwt.encode(
        {"iss": iss, "email": "u@popcorn.ai", "exp": exp}, _TEST_SIGNING_KEY, algorithm="HS256"
    )


PROD_ID_TOKEN = _jwt(PROD_ISS, _VALID)
NEW_PROD_ID_TOKEN = _jwt(PROD_ISS, _VALID - 1)
NEW_DEV_ID_TOKEN = _jwt(DEV_ISS, _VALID)


@pytest.fixture()
def two_profiles(tmp_path, monkeypatch):
    """A config file whose default is `prod`, with an expired `dev` alongside."""
    monkeypatch.setattr("popcorn_core.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr("popcorn_core.config.CONFIG_FILE", tmp_path / "auth.json")
    monkeypatch.setattr("popcorn_core.config._keyring_available", False)
    for var in (
        "POPCORN_API_URL",
        "POPCORN_CLERK_ISSUER",
        "POPCORN_CLERK_CLIENT_ID",
        "POPCORN_PROXY_MODE",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = Config(default_profile="prod")
    cfg.profiles["prod"] = Profile(
        api_url="https://api.popcorn.ai",
        clerk_issuer=PROD_ISS,
        clerk_client_id="prod-client",
        id_token=PROD_ID_TOKEN,
        access_token="prod-access",
        refresh_token="prod-refresh",
        email="u@popcorn.ai",
        expires_at=_VALID,
        workspace_id="ws-prod",
        workspace_name="Prod Workspace",
    )
    cfg.profiles["dev"] = Profile(
        api_url="https://api.dev.popcorn.ai",
        clerk_issuer=DEV_ISS,
        clerk_client_id="dev-client",
        id_token=_jwt(DEV_ISS, _EXPIRED),
        access_token="dev-access",
        refresh_token="dev-refresh",
        email="u@popcorn.ai",
        expires_at=_EXPIRED,
        workspace_id="ws-dev",
        workspace_name="Dev Workspace",
    )
    save_config(cfg)
    return cfg


@pytest.fixture()
def clerk_refreshes(monkeypatch):
    """Stub Clerk so a refresh succeeds without a network call.

    The reply is issued by whichever environment asked, so the issuer gate in
    `_token` sees a consistent pair either way.
    """
    monkeypatch.setattr(
        "popcorn_core.client.discover_oidc",
        lambda issuer: {"token_endpoint": f"{issuer}/oauth/token"},
    )

    def _post(url, data=None, **kw):
        if data["client_id"] == "prod-client":
            body = {
                "id_token": NEW_PROD_ID_TOKEN,
                "access_token": "prod-access-2",
                "refresh_token": "prod-refresh-2",
            }
        else:
            body = {
                "id_token": NEW_DEV_ID_TOKEN,
                "access_token": "dev-access-2",
                "refresh_token": "dev-refresh-2",
            }
        return httpx.Response(200, json=body)

    monkeypatch.setattr("popcorn_core.client.httpx.post", _post)


def _dev_override_args(**over: object) -> argparse.Namespace:
    base: dict[str, object] = {"env": "dev", "workspace": None, "timeout": None, "debug": False}
    base.update(over)
    return argparse.Namespace(**base)


def test_refresh_under_env_override_leaves_the_default_slot_alone(two_profiles, clerk_refreshes):
    client = _get_client(_dev_override_args())
    client._token()  # expired -> refreshes

    saved = load_config()
    prod = saved.profiles["prod"]
    assert prod.api_url == "https://api.popcorn.ai"
    assert prod.clerk_issuer == PROD_ISS
    assert prod.clerk_client_id == "prod-client"
    assert prod.id_token == PROD_ID_TOKEN
    assert prod.access_token == "prod-access"
    assert prod.refresh_token == "prod-refresh"
    assert prod.expires_at == _VALID
    assert prod.workspace_id == "ws-prod"


def test_refresh_under_env_override_updates_the_overridden_slot(two_profiles, clerk_refreshes):
    client = _get_client(_dev_override_args())
    client._token()

    saved = load_config()
    dev = saved.profiles["dev"]
    assert dev.id_token == NEW_DEV_ID_TOKEN
    assert dev.access_token == "dev-access-2"
    assert dev.refresh_token == "dev-refresh-2"
    assert dev.expires_at == _VALID
    # The selection itself is still not persisted — only `popcorn env` does that.
    assert saved.default_profile == "prod"


def test_refresh_does_not_persist_an_in_memory_override(two_profiles, clerk_refreshes):
    """A `--workspace` override lasts for the command, not forever.

    `_get_client` resolves `--workspace` by mutating the live profile, so
    writing the whole object back on refresh turned that into permanent state.
    Run against the default profile, where the slot key was never in doubt —
    otherwise the wrong-key bug hides this one by writing somewhere else
    entirely.
    """
    client = _get_client(_dev_override_args(env=None))
    client.profile.workspace_id = "ws-override"
    client.profile.workspace_name = "Override Workspace"
    client.profile.expires_at = _EXPIRED  # force the refresh
    client._token()

    prod = load_config().profiles["prod"]
    assert prod.workspace_id == "ws-prod"
    assert prod.workspace_name == "Prod Workspace"
    # The tokens themselves did land.
    assert prod.id_token == NEW_PROD_ID_TOKEN
    assert prod.refresh_token == "prod-refresh-2"


def test_refresh_of_an_unnamed_profile_writes_nothing(two_profiles, clerk_refreshes):
    """A hand-built profile names no slot, so there is no slot to guess at."""
    client = APIClient(
        Profile(
            api_url="https://api.dev.popcorn.ai",
            clerk_issuer=DEV_ISS,
            clerk_client_id="dev-client",
            id_token=_jwt(DEV_ISS, _EXPIRED),
            refresh_token="dev-refresh",
            expires_at=_EXPIRED,
        )
    )
    client._token()

    saved = load_config()
    assert saved.profiles["prod"].id_token == PROD_ID_TOKEN
    assert saved.profiles["dev"].refresh_token == "dev-refresh"
    assert client.profile.id_token == NEW_DEV_ID_TOKEN  # refreshed in memory only
