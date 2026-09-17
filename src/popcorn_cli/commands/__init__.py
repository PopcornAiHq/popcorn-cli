"""Registry-declared command families. Importing this module registers them."""

from __future__ import annotations

from . import app, auth, channel_config, flow, schedule, table, template, webhook, workspace

__all__ = [
    "app",
    "auth",
    "channel_config",
    "flow",
    "schedule",
    "table",
    "template",
    "webhook",
    "workspace",
]
