"""Tests for popcorn_core.config."""

from __future__ import annotations

import json

import pytest

from popcorn_core.config import Config, Profile, load_config, save_config


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
