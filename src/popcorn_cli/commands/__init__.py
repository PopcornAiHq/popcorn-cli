"""Registry-declared command families. Importing this module registers them."""

from __future__ import annotations

from . import flow, table

__all__ = ["flow", "table"]
