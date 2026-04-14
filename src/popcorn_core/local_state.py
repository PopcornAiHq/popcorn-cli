"""Manage .popcorn.local.json — tracks deploy targets per project directory.

Schema v2 supports multiple named targets across workspaces and environments:

    {
        "version": 2,
        "default_target": "pop-my-app",
        "targets": {
            "pop-my-app": {
                "profile": "default",
                "workspace_id": "ws_abc",
                "workspace_name": "Acme",
                "conversation_id": "conv_def",
                "site_name": "pop-my-app",
                "deployed_at": "2026-03-31T10:00:00Z"
            }
        }
    }

Target keys default to site_name.  On collision (same site_name, different
workspace/env), the key becomes site_name@profile or site_name@workspace_name.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

LOCAL_STATE_FILE = ".popcorn.local.json"
_VERSION = 2


@dataclass
class Target:
    workspace_id: str
    conversation_id: str
    site_name: str
    workspace_name: str = ""
    profile: str = ""
    deployed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v}


@dataclass
class LocalState:
    version: int = _VERSION
    default_target: str = ""
    targets: dict[str, Target] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"version": self.version}
        if self.default_target:
            d["default_target"] = self.default_target
        d["targets"] = {k: v.to_dict() for k, v in self.targets.items()}
        return d


def load_local_state(path: Path | None = None) -> LocalState:
    """Load .popcorn.local.json, migrating v1 if needed.

    Returns empty LocalState if file doesn't exist or is unreadable.
    """
    p = path or Path(LOCAL_STATE_FILE)
    if not p.exists():
        return LocalState()

    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return LocalState()

    if not isinstance(data, dict):
        return LocalState()

    # v2
    if data.get("version") == 2:
        return _parse_v2(data)

    # v1: {"conversation_id": "...", "site_name": "..."}
    if "conversation_id" in data:
        return _migrate_v1(data)

    return LocalState()


def save_local_state(state: LocalState, path: Path | None = None) -> None:
    """Write state to .popcorn.local.json."""
    p = path or Path(LOCAL_STATE_FILE)
    p.write_text(json.dumps(state.to_dict(), indent=2) + "\n")


class AmbiguousTargetError(Exception):
    """Raised when multiple targets exist but none can be auto-selected."""

    def __init__(self, available: list[str]) -> None:
        self.available = available
        super().__init__(f"Multiple targets, none matching: {', '.join(available)}")


def resolve_target(
    state: LocalState,
    *,
    workspace_id: str = "",
    target_name: str = "",
) -> Target | None:
    """Find a target by explicit name or by matching workspace.

    Priority:
    1. Explicit target_name (--target flag)
    2. default_target
    3. First target matching workspace_id
    4. Single target (unambiguous)

    Raises AmbiguousTargetError when multiple targets exist but none
    match via default or workspace — prevents silent new-channel creation.
    Returns None only when no targets exist at all.
    """
    if target_name:
        return state.targets.get(target_name)

    # Try default
    if state.default_target and state.default_target in state.targets:
        return state.targets[state.default_target]

    # Match by workspace
    if workspace_id:
        for t in state.targets.values():
            if t.workspace_id == workspace_id:
                return t

    # Single target — just use it
    if len(state.targets) == 1:
        return next(iter(state.targets.values()))

    # Multiple targets, no match — ambiguous
    if len(state.targets) > 1:
        raise AmbiguousTargetError(list(state.targets.keys()))

    return None


def upsert_target(
    state: LocalState,
    target: Target,
    *,
    set_default: bool = True,
) -> str:
    """Add or update a target, returning the key used.

    If a target with matching (workspace_id, site_name) exists, updates it.
    Otherwise creates a new entry with a collision-safe key.
    """
    # Find existing by (workspace_id, site_name)
    for key, existing in state.targets.items():
        if existing.workspace_id == target.workspace_id and existing.site_name == target.site_name:
            state.targets[key] = target
            if set_default:
                state.default_target = key
            return key

    # New target — pick a key
    key = _pick_key(state, target)
    state.targets[key] = target
    if set_default:
        state.default_target = key
    return key


def make_target(
    *,
    workspace_id: str,
    conversation_id: str,
    site_name: str,
    workspace_name: str = "",
    profile: str = "",
) -> Target:
    """Create a Target with deployed_at set to now."""
    return Target(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        site_name=site_name,
        workspace_name=workspace_name,
        profile=profile,
        deployed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _pick_key(state: LocalState, target: Target) -> str:
    """Choose a unique key for a new target.

    site_name → site_name@profile → site_name@workspace_name → site_name@ws_id_prefix
    """
    base = target.site_name

    if base not in state.targets:
        return base

    # Collision — qualify with profile or workspace
    if target.profile:
        qualified = f"{base}@{target.profile}"
        if qualified not in state.targets:
            return qualified

    if target.workspace_name:
        qualified = f"{base}@{target.workspace_name}"
        if qualified not in state.targets:
            return qualified

    # Last resort — workspace_id prefix
    short_id = target.workspace_id[:8] if target.workspace_id else "unknown"
    qualified = f"{base}@{short_id}"
    # If even this collides, append digits
    if qualified not in state.targets:
        return qualified
    i = 2
    while f"{qualified}-{i}" in state.targets:
        i += 1
    return f"{qualified}-{i}"


def _parse_v2(data: dict[str, Any]) -> LocalState:
    state = LocalState(
        version=2,
        default_target=data.get("default_target", ""),
    )
    for key, tdata in data.get("targets", {}).items():
        if not isinstance(tdata, dict):
            continue
        state.targets[key] = Target(
            workspace_id=tdata.get("workspace_id", ""),
            conversation_id=tdata.get("conversation_id", ""),
            site_name=tdata.get("site_name", ""),
            workspace_name=tdata.get("workspace_name", ""),
            profile=tdata.get("profile", ""),
            deployed_at=tdata.get("deployed_at", ""),
        )
    return state


def _migrate_v1(data: dict[str, Any]) -> LocalState:
    """Convert v1 format to v2.

    v1: {"conversation_id": "...", "site_name": "..."}
    Profile and workspace are unknown — filled in on next deploy.
    """
    site_name = data.get("site_name", "")
    conversation_id = data.get("conversation_id", "")

    if not conversation_id:
        return LocalState()

    key = site_name or "default"
    target = Target(
        workspace_id="",
        conversation_id=conversation_id,
        site_name=site_name,
    )
    return LocalState(
        version=_VERSION,
        default_target=key,
        targets={key: target},
    )
