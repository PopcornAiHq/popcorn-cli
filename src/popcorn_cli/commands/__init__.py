"""Registry-declared command families. Importing this module registers them."""

from __future__ import annotations

from . import (
    app,
    auth,
    channel,
    channel_config,
    flow,
    message,
    schedule,
    table,
    template,
    webhook,
    workspace,
)

__all__ = [
    "app",
    "auth",
    "channel",
    "channel_config",
    "flow",
    "message",
    "schedule",
    "table",
    "template",
    "webhook",
    "workspace",
]
