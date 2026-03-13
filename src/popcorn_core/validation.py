"""Response validation helpers."""

from __future__ import annotations

import json
from typing import Any

from popcorn_core.errors import PopcornError


def extract(response: dict, *keys: str, label: str) -> Any:
    """Traverse nested keys in a response dict.

    >>> extract({"conversation": {"id": "c1"}}, "conversation", "id", label="test")
    'c1'

    Raises PopcornError with a truncated response dump on missing or non-dict
    intermediate keys.
    """
    current: Any = response
    for key in keys:
        if not isinstance(current, dict):
            truncated = json.dumps(response)[:200]
            raise PopcornError(
                f"Unexpected response from {label}: missing '{key}'. Response: {truncated}"
            )
        if key not in current:
            truncated = json.dumps(response)[:200]
            raise PopcornError(
                f"Unexpected response from {label}: missing '{key}'. Response: {truncated}"
            )
        current = current[key]
    return current
