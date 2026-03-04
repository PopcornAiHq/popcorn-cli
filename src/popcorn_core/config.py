"""Configuration management for Popcorn."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import PopcornError

DEFAULT_ENV: dict[str, str] = {
    "api_url": "https://api.popcorn.ai",
    "clerk_issuer": "https://clerk.popcorn.ai",
    "clerk_client_id": "MDs9UavwNLuGSgJR",
}


def resolve_env() -> dict[str, str]:
    """Return environment config, with env var overrides."""
    return {
        "api_url": os.environ.get("POPCORN_API_URL", DEFAULT_ENV["api_url"]),
        "clerk_issuer": os.environ.get("POPCORN_CLERK_ISSUER", DEFAULT_ENV["clerk_issuer"]),
        "clerk_client_id": os.environ.get(
            "POPCORN_CLERK_CLIENT_ID", DEFAULT_ENV["clerk_client_id"]
        ),
    }


CONFIG_DIR = Path.home() / ".config" / "popcorn"
CONFIG_FILE = CONFIG_DIR / "auth.json"

OAUTH_CALLBACK_PORT = 28771  # Fixed port for Clerk redirect URI (ASCII "pc")


@dataclass
class Profile:
    api_url: str = ""
    clerk_issuer: str = ""
    clerk_client_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    email: str = ""
    expires_at: int = 0
    workspace_id: str = ""
    workspace_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "api_url": self.api_url,
            "clerk_issuer": self.clerk_issuer,
            "clerk_client_id": self.clerk_client_id,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "email": self.email,
            "expires_at": self.expires_at,
            "workspace_id": self.workspace_id,
            "workspace_name": self.workspace_name,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Profile:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Config:
    version: int = 1
    default_profile: str = "default"
    profiles: dict[str, Profile] = field(default_factory=dict)

    def active_profile(self) -> Profile:
        if self.default_profile not in self.profiles:
            self.profiles[self.default_profile] = Profile()
        return self.profiles[self.default_profile]


def load_config() -> Config:
    if not CONFIG_FILE.exists():
        return Config()
    try:
        raw = CONFIG_FILE.read_text()
    except PermissionError as e:
        raise PopcornError(f"Cannot read config file: {CONFIG_FILE} (permission denied)") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise PopcornError(
            f"Config file is corrupted: {CONFIG_FILE}\n"
            f"  Error: {e}\n"
            f"  Fix: Delete the file and run 'popcorn auth login'"
        ) from e
    try:
        cfg = Config(
            version=data.get("version", 1),
            default_profile=data.get("default_profile", "default"),
        )
        for name, pdata in data.get("profiles", {}).items():
            cfg.profiles[name] = Profile.from_dict(pdata)
        return cfg
    except (KeyError, TypeError, AttributeError) as e:
        raise PopcornError(
            f"Config file has unexpected structure: {CONFIG_FILE}\n"
            f"  Error: {e}\n"
            f"  Fix: Delete the file and run 'popcorn auth login'"
        ) from e


def save_config(cfg: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "version": cfg.version,
        "default_profile": cfg.default_profile,
        "profiles": {k: v.to_dict() for k, v in cfg.profiles.items()},
    }
    CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n")
    CONFIG_FILE.chmod(0o600)
