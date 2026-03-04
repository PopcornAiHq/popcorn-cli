"""Popcorn Core — shared library for Popcorn messaging tools."""

from importlib.metadata import version

__version__ = version("popcorn-cli")

# Re-export operations module for convenience: `from popcorn_core import operations`
from popcorn_core import operations
from popcorn_core.client import APIClient
from popcorn_core.config import Config, Profile, load_config, save_config
from popcorn_core.errors import APIError, AuthError, PopcornError
from popcorn_core.resolve import resolve_conversation

__all__ = [
    "APIClient",
    "APIError",
    "AuthError",
    "Config",
    "PopcornError",
    "Profile",
    "load_config",
    "operations",
    "resolve_conversation",
    "save_config",
]
