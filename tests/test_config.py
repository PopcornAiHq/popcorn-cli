"""Tests for popcorn_core.config."""

from __future__ import annotations

import json

import pytest

from popcorn_core.config import (
    DEFAULT_ENV,
    Config,
    Profile,
    load_config,
    resolve_auth_env,
    save_config,
)


class TestProfile:
    def test_defaults(self):
        p = Profile()
        assert p.api_url == ""
        assert p.id_token == ""
        assert p.expires_at == 0

    def test_roundtrip(self):
        p = Profile(api_url="https://api.test", email="a@b.com", expires_at=123)
        d = p.to_dict()
        p2 = Profile.from_dict(d)
        assert p2.api_url == p.api_url
        assert p2.email == p.email
        assert p2.expires_at == p.expires_at

    def test_from_dict_ignores_unknown_keys(self):
        p = Profile.from_dict({"api_url": "https://x", "unknown_field": "ignored"})
        assert p.api_url == "https://x"


class TestConfig:
    def test_active_profile_creates_default(self):
        cfg = Config()
        assert "default" not in cfg.profiles
        profile = cfg.active_profile()
        assert "default" in cfg.profiles
        assert profile.api_url == ""

    def test_active_profile_returns_existing(self):
        cfg = Config()
        cfg.profiles["default"] = Profile(email="a@b.com")
        assert cfg.active_profile().email == "a@b.com"


class TestLoadSave:
    def test_load_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("popcorn_core.config.CONFIG_FILE", tmp_path / "nope.json")
        cfg = load_config()
        assert isinstance(cfg, Config)
        assert cfg.default_profile == "default"

    def test_save_and_load(self, tmp_path, monkeypatch):
        config_file = tmp_path / "auth.json"
        monkeypatch.setattr("popcorn_core.config.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("popcorn_core.config.CONFIG_FILE", config_file)

        cfg = Config()
        cfg.profiles["default"] = Profile(email="test@popcorn.ai", workspace_id="ws-123")
        save_config(cfg)

        assert config_file.exists()
        assert oct(config_file.stat().st_mode & 0o777) == "0o600"

        loaded = load_config()
        assert loaded.profiles["default"].email == "test@popcorn.ai"
        assert loaded.profiles["default"].workspace_id == "ws-123"

    def test_load_corrupt_json(self, tmp_path, monkeypatch):
        config_file = tmp_path / "auth.json"
        config_file.write_text("not json{{{")
        monkeypatch.setattr("popcorn_core.config.CONFIG_FILE", config_file)

        from popcorn_core.errors import PopcornError

        with pytest.raises(PopcornError, match="corrupted"):
            load_config()

    def test_load_bad_structure(self, tmp_path, monkeypatch):
        config_file = tmp_path / "auth.json"
        config_file.write_text(json.dumps({"profiles": {"bad": "not-a-dict"}}))
        monkeypatch.setattr("popcorn_core.config.CONFIG_FILE", config_file)

        from popcorn_core.errors import PopcornError

        with pytest.raises(PopcornError, match="unexpected structure"):
            load_config()


class TestKeyringIntegration:
    """Test that keyring storage works when available (mocked)."""

    def test_save_uses_keyring_when_available(self, tmp_path, monkeypatch):
        from popcorn_core.config import _KEYRING_SENTINEL

        config_file = tmp_path / "auth.json"
        monkeypatch.setattr("popcorn_core.config.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("popcorn_core.config.CONFIG_FILE", config_file)

        store: dict[str, str] = {}

        def mock_set(k: str, v: str) -> bool:
            store[k] = v
            return True

        monkeypatch.setattr("popcorn_core.config._keyring_available", True)
        monkeypatch.setattr("popcorn_core.config._keyring_set", mock_set)
        monkeypatch.setattr("popcorn_core.config._keyring_get", lambda k: store.get(k))

        cfg = Config()
        cfg.profiles["default"] = Profile(
            email="a@b.com", id_token="secret-tok", refresh_token="secret-ref"
        )
        save_config(cfg)

        # Tokens stored in keyring
        assert store["default/id_token"] == "secret-tok"
        assert store["default/refresh_token"] == "secret-ref"

        # File has sentinel, not real tokens
        raw = json.loads(config_file.read_text())
        assert raw["profiles"]["default"]["id_token"] == _KEYRING_SENTINEL
        assert raw["profiles"]["default"]["refresh_token"] == _KEYRING_SENTINEL

        # Load reads back from keyring
        loaded = load_config()
        assert loaded.profiles["default"].id_token == "secret-tok"
        assert loaded.profiles["default"].refresh_token == "secret-ref"

    def test_save_falls_back_without_keyring(self, tmp_path, monkeypatch):
        config_file = tmp_path / "auth.json"
        monkeypatch.setattr("popcorn_core.config.CONFIG_DIR", tmp_path)
        monkeypatch.setattr("popcorn_core.config.CONFIG_FILE", config_file)
        monkeypatch.setattr("popcorn_core.config._keyring_available", False)

        cfg = Config()
        cfg.profiles["default"] = Profile(email="a@b.com", id_token="plain-tok")
        save_config(cfg)

        # Token stored in plaintext
        raw = json.loads(config_file.read_text())
        assert raw["profiles"]["default"]["id_token"] == "plain-tok"


class TestResolveAuthEnv:
    """Regression: all three OAuth fields must resolve together so a stored dev
    profile doesn't get paired with the default prod issuer when env vars
    aren't set (observed as Clerk `invalid_client` against prod with a dev
    client_id)."""

    def test_stored_profile_values_take_precedence_over_defaults(self, monkeypatch):
        for var in ("POPCORN_API_URL", "POPCORN_CLERK_ISSUER", "POPCORN_CLERK_CLIENT_ID"):
            monkeypatch.delenv(var, raising=False)
        p = Profile(
            api_url="https://api.dev.popcorn.ai",
            clerk_issuer="https://clerk.dev.popcorn.ai",
            clerk_client_id="dev-client",
        )
        r = resolve_auth_env(p)
        assert r["api_url"] == "https://api.dev.popcorn.ai"
        assert r["clerk_issuer"] == "https://clerk.dev.popcorn.ai"
        assert r["clerk_client_id"] == "dev-client"

    def test_env_vars_override_stored_profile(self, monkeypatch):
        monkeypatch.setenv("POPCORN_API_URL", "https://api.override")
        monkeypatch.setenv("POPCORN_CLERK_ISSUER", "https://clerk.override")
        monkeypatch.setenv("POPCORN_CLERK_CLIENT_ID", "override-client")
        p = Profile(
            api_url="https://api.dev.popcorn.ai",
            clerk_issuer="https://clerk.dev.popcorn.ai",
            clerk_client_id="dev-client",
        )
        r = resolve_auth_env(p)
        assert r["api_url"] == "https://api.override"
        assert r["clerk_issuer"] == "https://clerk.override"
        assert r["clerk_client_id"] == "override-client"

    def test_empty_profile_falls_through_to_defaults(self, monkeypatch):
        for var in ("POPCORN_API_URL", "POPCORN_CLERK_ISSUER", "POPCORN_CLERK_CLIENT_ID"):
            monkeypatch.delenv(var, raising=False)
        r = resolve_auth_env(Profile())
        assert r == DEFAULT_ENV

    def test_partial_profile_resolves_each_field_independently(self, monkeypatch):
        # Profile has only issuer set; api_url and client_id come from env/default.
        for var in ("POPCORN_API_URL", "POPCORN_CLERK_ISSUER", "POPCORN_CLERK_CLIENT_ID"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("POPCORN_CLERK_CLIENT_ID", "from-env")
        p = Profile(clerk_issuer="https://clerk.dev.popcorn.ai")
        r = resolve_auth_env(p)
        assert r["api_url"] == DEFAULT_ENV["api_url"]
        assert r["clerk_issuer"] == "https://clerk.dev.popcorn.ai"
        assert r["clerk_client_id"] == "from-env"
