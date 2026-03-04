"""Conversation name resolution — #channel-name to UUID."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .errors import PopcornError

if TYPE_CHECKING:
    from .client import APIClient

_channel_cache: dict[str, tuple[str, float]] = {}
CHANNEL_CACHE_TTL = 300  # seconds


def resolve_conversation(client: APIClient, ref: str) -> str:
    """Resolve #channel-name to UUID, or pass through UUIDs."""
    # Already a UUID
    if len(ref) == 36 and ref.count("-") == 4:
        return ref

    # Strip leading # if present
    name = ref.lstrip("#").lower()

    # Check cache
    now = time.time()
    if name in _channel_cache:
        cached_id, cached_at = _channel_cache[name]
        if now - cached_at < CHANNEL_CACHE_TTL:
            return cached_id

    # Fetch conversation list and match by name
    resp = client.get("/api/conversations/list", {"limit": 1000})
    conversations = resp.get("conversations", [])

    for conv in conversations:
        conv_name = (conv.get("name") or "").lower()
        if conv_name == name:
            conv_id: str = conv["id"]
            _channel_cache[name] = (conv_id, now)
            return conv_id

    raise PopcornError(f"Channel not found: #{name}")
