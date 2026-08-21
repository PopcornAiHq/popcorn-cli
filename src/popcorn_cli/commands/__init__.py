"""Registry-declared command families. Importing this module registers them."""

from __future__ import annotations

from . import app, flow, table, template

__all__ = ["app", "flow", "table", "template"]
