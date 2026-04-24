"""Configuration management for Popcorn."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import PopcornError

# ---------------------------------------------------------------------------
# Keyring helpers — optional secure token storage
# ---------------------------------------------------------------------------

_SERVICE_NAME = "popcorn-cli"
_keyring_available: bool | None = None


def _warn(msg: str) -> None:
    print(f"Warning: {msg}", file=sys.stderr)


def _has_keyring() -> bool:
    """Check if keyring is importable and functional."""
    global _keyring_available
    if _keyring_available is not None:
        return _keyring_available
    try:
        import keyring as _kr  # type: ignore[import-not-found]

        # Probe — some backends (e.g. chainer with no backends) silently fail
        _kr.get_credential(_SERVICE_NAME, None)
        _keyring_available = True
    except ImportError:
        _keyring_available = False
    except Exception as exc:
        _warn(
            f"keyring is installed but not functional ({type(exc).__name__}: {exc}). "
            "Tokens will be stored in plaintext."
        )
        _keyring_available = False
    return _keyring_available


def _keyring_set(key: str, value: str) -> bool:
    """Store a secret in the system keychain. Returns False on failure."""
    try:
        import keyring as _kr  # type: ignore[import-not-found]

        _kr.set_password(_SERVICE_NAME, key, value)
        return True
    except Exception as exc:
        _warn(f"Failed to store {key.split('/')[-1]} in keyring ({exc}). Using plaintext.")
        return False


def _keyring_get(key: str) -> str | None:
    """Retrieve a secret from the system keychain."""
    try:
        import keyring as _kr  # type: ignore[import-not-found]

        result: str | None = _kr.get_password(_SERVICE_NAME, key)
        return result
    except Exception as exc:
        _warn(f"Failed to read {key.split('/')[-1]} from keyring ({exc}).")
        return None


def _keyring_delete(key: str) -> None:
    """Delete a secret from the system keychain."""
    import contextlib

    import keyring as _kr  # type: ignore[import-not-found]
    import keyring.errors  # type: ignore[import-not-found]

    with contextlib.suppress(keyring.errors.PasswordDeleteError):
        _kr.delete_password(_SERVICE_NAME, key)


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


def resolve_auth_env(profile: Profile) -> dict[str, str]:
    """Resolve OAuth config for a login with priority: env var > profile > default.

    All three fields (api_url, clerk_issuer, clerk_client_id) must resolve
    together so a stored dev profile doesn't get paired with the default prod
    issuer when env vars aren't set.
    """
    return {
        "api_url": os.environ.get("POPCORN_API_URL") or profile.api_url or DEFAULT_ENV["api_url"],
        "clerk_issuer": (
            os.environ.get("POPCORN_CLERK_ISSUER")
            or profile.clerk_issuer
            or DEFAULT_ENV["clerk_issuer"]
        ),
        "clerk_client_id": (
            os.environ.get("POPCORN_CLERK_CLIENT_ID")
            or profile.clerk_client_id
            or DEFAULT_ENV["clerk_client_id"]
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
        use_kr = _has_keyring()
        cfg = Config(
            version=data.get("version", 1),
            default_profile=data.get("default_profile", "default"),
        )
        for name, pdata in data.get("profiles", {}).items():
            if use_kr:
                for fld in _KEYRING_FIELDS:
                    if pdata.get(fld) == _KEYRING_SENTINEL:
                        val = _keyring_get(f"{name}/{fld}")
                        if val is None:
                            _warn(
                                f"Token '{fld}' was stored in keyring but is no longer "
                                "available. Run: popcorn auth login"
                            )
                        pdata[fld] = val or ""
            cfg.profiles[name] = Profile.from_dict(pdata)
        return cfg
    except (KeyError, TypeError, AttributeError) as e:
        raise PopcornError(
            f"Config file has unexpected structure: {CONFIG_FILE}\n"
            f"  Error: {e}\n"
            f"  Fix: Delete the file and run 'popcorn auth login'"
        ) from e


_KEYRING_FIELDS = ("id_token", "refresh_token", "access_token")
_KEYRING_SENTINEL = "__keyring__"


def save_config(cfg: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    use_kr = _has_keyring()
    profiles_data: dict[str, Any] = {}
    for name, profile in cfg.profiles.items():
        d = profile.to_dict()
        if use_kr:
            for fld in _KEYRING_FIELDS:
                val = d.get(fld, "")
                if val and _keyring_set(f"{name}/{fld}", val):
                    d[fld] = _KEYRING_SENTINEL
        profiles_data[name] = d
    data = {
        "version": cfg.version,
        "default_profile": cfg.default_profile,
        "profiles": profiles_data,
    }
    CONFIG_FILE.write_text(json.dumps(data, indent=2) + "\n")
    CONFIG_FILE.chmod(0o600)
