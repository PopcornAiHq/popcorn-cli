"""Tests for the environment/issuer safety gate (prod-fallback footgun).

`popcorn -e dev` selects a profile literally named "dev". With no dev endpoints
configured it falls back to the prod defaults, so a prod-issued token can end up
running "dev" commands against production. Two guards prevent that:

- Layer A (issuer gate): a token's `iss` claim must match the environment's
  configured Clerk issuer, enforced at request time and at login.
- Layer B (prod-default guard): logging into a non-"default" profile that
  resolves to the prod defaults requires explicit confirmation.
"""

from __future__ import annotations

import argparse
from unittest.mock import patch

import jwt
import pytest

from popcorn_cli.cli import cmd_auth_login
from popcorn_core.auth import assert_token_env_match, decode_token_issuer
from popcorn_core.client import APIClient
from popcorn_core.config import Config, Profile
from popcorn_core.errors import AuthError, PopcornError

PROD_ISS = "https://clerk.popcorn.ai"
DEV_ISS = "https://clerk.dev.popcorn.ai"

_AUTH_ENV_VARS = (
    "POPCORN_API_URL",
    "POPCORN_CLERK_ISSUER",
    "POPCORN_CLERK_CLIENT_ID",
    "POPCORN_ASSUME_YES",
    "POPCORN_PROXY_MODE",
)


_TEST_SIGNING_KEY = "x" * 32  # length only matters to silence PyJWT's key-length warning


def _jwt(iss: str, email: str = "u@popcorn.ai", exp: int = 9999999999) -> str:
    return jwt.encode(
        {"iss": iss, "email": email, "exp": exp}, _TEST_SIGNING_KEY, algorithm="HS256"
    )


def _login_args(**over: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "env": None,
        "force": False,
        "with_token": False,
        "workspace": None,
        "yes": False,
    }
    base.update(over)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# decode_token_issuer
# ---------------------------------------------------------------------------


def test_decode_issuer_reads_iss():
    assert decode_token_issuer(_jwt(PROD_ISS)) == PROD_ISS


def test_decode_issuer_opaque_token_is_none():
    assert decode_token_issuer("not-a-jwt") is None


def test_decode_issuer_missing_claim_is_none():
    tok = jwt.encode({"email": "u@popcorn.ai"}, _TEST_SIGNING_KEY, algorithm="HS256")
    assert decode_token_issuer(tok) is None


# ---------------------------------------------------------------------------
# assert_token_env_match
# ---------------------------------------------------------------------------


def test_match_ok_when_issuers_equal():
    assert_token_env_match(_jwt(DEV_ISS), DEV_ISS)  # no raise


def test_match_ignores_trailing_slash_and_case():
    assert_token_env_match(_jwt(DEV_ISS), DEV_ISS + "/")  # no raise


def test_mismatch_raises_auth_error():
    with pytest.raises(AuthError, match="Token/environment mismatch"):
        assert_token_env_match(_jwt(PROD_ISS), DEV_ISS, env="dev")


def test_no_raise_when_expected_issuer_empty():
    # Older profiles may not record an issuer — we can't classify, so don't block.
    assert_token_env_match(_jwt(PROD_ISS), "")


def test_no_raise_for_opaque_token():
    # A non-JWT / issuer-less token can't be classified — don't block.
    assert_token_env_match("opaque-token", DEV_ISS)


def test_no_raise_for_empty_token():
    assert_token_env_match("", DEV_ISS)


# ---------------------------------------------------------------------------
# APIClient._token — request-time enforcement
# ---------------------------------------------------------------------------


def test_client_token_rejects_cross_env_token(monkeypatch):
    monkeypatch.delenv("POPCORN_PROXY_MODE", raising=False)
    profile = Profile(
        api_url="https://api.dev.popcorn.ai",
        clerk_issuer=DEV_ISS,
        id_token=_jwt(PROD_ISS),  # prod token in a dev profile
        expires_at=9999999999,
    )
    client = APIClient(profile)
    with pytest.raises(AuthError, match="mismatch"):
        client._token()


def test_client_token_ok_when_issuer_matches(monkeypatch):
    monkeypatch.delenv("POPCORN_PROXY_MODE", raising=False)
    tok = _jwt(DEV_ISS)
    profile = Profile(
        api_url="https://api.dev.popcorn.ai",
        clerk_issuer=DEV_ISS,
        id_token=tok,
        expires_at=9999999999,
    )
    client = APIClient(profile)
    assert client._token() == tok


# ---------------------------------------------------------------------------
# cmd_auth_login — Layer B prod-default guard
# ---------------------------------------------------------------------------


def test_login_blocks_named_profile_on_prod_defaults_non_tty(monkeypatch):
    for var in _AUTH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    args = _login_args(env="dev")
    with (
        patch("popcorn_cli.cli.load_config", return_value=Config()),
        patch("popcorn_cli.cli.sys.stdin") as stdin,
    ):
        stdin.isatty.return_value = False
        # _confirm refuses to prompt in non-interactive mode → loud failure.
        with pytest.raises(PopcornError, match="non-interactive"):
            cmd_auth_login(args)


def test_login_default_profile_on_prod_is_not_blocked(monkeypatch):
    for var in _AUTH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    args = _login_args(env=None, with_token=True)  # env_name defaults to "default"
    with (
        patch("popcorn_cli.cli.load_config", return_value=Config()),
        patch("popcorn_cli.cli.save_config"),
        patch("popcorn_cli.cli._select_workspace"),
        patch("popcorn_cli.cli.APIClient"),
        patch("popcorn_cli.cli.sys.stdin") as stdin,
    ):
        stdin.read.return_value = _jwt(PROD_ISS)  # prod token, prod profile → matches
        stdin.isatty.return_value = False  # would raise if the guard fired
        cmd_auth_login(args)  # no raise — "default" is exempt


def test_login_named_prod_profile_allowed_with_yes(monkeypatch):
    for var in _AUTH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    args = _login_args(env="dev", with_token=True, yes=True)
    with (
        patch("popcorn_cli.cli.load_config", return_value=Config()),
        patch("popcorn_cli.cli.save_config"),
        patch("popcorn_cli.cli._select_workspace"),
        patch("popcorn_cli.cli.APIClient"),
        patch("popcorn_cli.cli.sys.stdin") as stdin,
    ):
        stdin.read.return_value = _jwt(PROD_ISS)
        stdin.isatty.return_value = False
        cmd_auth_login(args)  # --yes bypasses the guard


# ---------------------------------------------------------------------------
# cmd_auth_login — Layer A issuer gate at login (--with-token)
# ---------------------------------------------------------------------------


def test_login_with_token_rejects_cross_env_token(monkeypatch):
    # Dev endpoints configured via env vars; a prod token is pasted in.
    monkeypatch.setenv("POPCORN_API_URL", "https://api.dev.popcorn.ai")
    monkeypatch.setenv("POPCORN_CLERK_ISSUER", DEV_ISS)
    monkeypatch.setenv("POPCORN_CLERK_CLIENT_ID", "dev-client")
    monkeypatch.delenv("POPCORN_ASSUME_YES", raising=False)
    monkeypatch.delenv("POPCORN_PROXY_MODE", raising=False)
    args = _login_args(env="dev", with_token=True)
    with (
        patch("popcorn_cli.cli.load_config", return_value=Config()),
        patch("popcorn_cli.cli.save_config"),
        patch("popcorn_cli.cli.sys.stdin") as stdin,
    ):
        stdin.read.return_value = _jwt(PROD_ISS)  # wrong environment
        stdin.isatty.return_value = True
        with pytest.raises(AuthError, match="mismatch"):
            cmd_auth_login(args)
