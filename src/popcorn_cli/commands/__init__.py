"""Registry-declared command families. Importing this module registers them."""

from __future__ import annotations

from . import (
    app,
    auth,
    flow,
    message,
    project,
    project_config,
    schedule,
    table,
    template,
    webhook,
    workspace,
)

__all__ = [
    "app",
    "auth",
    "flow",
    "message",
    "project",
    "project_config",
    "schedule",
    "table",
    "template",
    "webhook",
    "workspace",
]
