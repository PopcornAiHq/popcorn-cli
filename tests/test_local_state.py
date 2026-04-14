"""Tests for popcorn_core.local_state — deploy target tracking."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from popcorn_core.local_state import (
    AmbiguousTargetError,
    LocalState,
    Target,
    load_local_state,
    make_target,
    resolve_target,
    save_local_state,
    upsert_target,
)

# ---------------------------------------------------------------------------
# load_local_state
# ---------------------------------------------------------------------------


class TestLoadLocalState:
    def test_no_file(self, tmp_path: Path) -> None:
        state = load_local_state(tmp_path / ".popcorn.local.json")
        assert state.targets == {}
        assert state.default_target == ""

    def test_invalid_json(self, tmp_path: Path) -> None:
        p = tmp_path / ".popcorn.local.json"
        p.write_text("not json")
        state = load_local_state(p)
        assert state.targets == {}

    def test_non_dict(self, tmp_path: Path) -> None:
        p = tmp_path / ".popcorn.local.json"
        p.write_text('"just a string"')
        state = load_local_state(p)
        assert state.targets == {}

    def test_v1_migration(self, tmp_path: Path) -> None:
        p = tmp_path / ".popcorn.local.json"
        p.write_text(json.dumps({"conversation_id": "conv_1", "site_name": "my-app"}))

        state = load_local_state(p)
        assert state.version == 2
        assert state.default_target == "my-app"
        assert "my-app" in state.targets
        t = state.targets["my-app"]
        assert t.conversation_id == "conv_1"
        assert t.site_name == "my-app"
        assert t.workspace_id == ""  # unknown until next deploy

    def test_v1_migration_no_site_name(self, tmp_path: Path) -> None:
        p = tmp_path / ".popcorn.local.json"
        p.write_text(json.dumps({"conversation_id": "conv_1"}))

        state = load_local_state(p)
        assert state.default_target == "default"
        assert "default" in state.targets

    def test_v1_migration_no_conversation_id(self, tmp_path: Path) -> None:
        p = tmp_path / ".popcorn.local.json"
        p.write_text(json.dumps({"site_name": "my-app"}))

        state = load_local_state(p)
        assert state.targets == {}

    def test_v2_load(self, tmp_path: Path) -> None:
        p = tmp_path / ".popcorn.local.json"
        p.write_text(
            json.dumps(
                {
                    "version": 2,
                    "default_target": "my-app",
                    "targets": {
                        "my-app": {
                            "workspace_id": "ws_1",
                            "workspace_name": "Acme",
                            "conversation_id": "conv_1",
                            "site_name": "my-app",
                            "profile": "default",
                            "deployed_at": "2026-03-31T10:00:00Z",
                        }
                    },
                }
            )
        )

        state = load_local_state(p)
        assert state.version == 2
        assert state.default_target == "my-app"
        t = state.targets["my-app"]
        assert t.workspace_id == "ws_1"
        assert t.workspace_name == "Acme"
        assert t.conversation_id == "conv_1"
        assert t.profile == "default"

    def test_v2_multiple_targets(self, tmp_path: Path) -> None:
        p = tmp_path / ".popcorn.local.json"
        p.write_text(
            json.dumps(
                {
                    "version": 2,
                    "default_target": "staging",
                    "targets": {
                        "staging": {
                            "workspace_id": "ws_1",
                            "conversation_id": "conv_1",
                            "site_name": "pop-app",
                            "profile": "dev",
                        },
                        "production": {
                            "workspace_id": "ws_2",
                            "conversation_id": "conv_2",
                            "site_name": "app",
                            "profile": "default",
                        },
                    },
                }
            )
        )

        state = load_local_state(p)
        assert len(state.targets) == 2
        assert state.targets["staging"].profile == "dev"
        assert state.targets["production"].workspace_id == "ws_2"

    def test_v2_skips_non_dict_targets(self, tmp_path: Path) -> None:
        p = tmp_path / ".popcorn.local.json"
        p.write_text(
            json.dumps(
                {
                    "version": 2,
                    "targets": {
                        "good": {"workspace_id": "ws_1", "conversation_id": "c", "site_name": "s"},
                        "bad": "not a dict",
                    },
                }
            )
        )

        state = load_local_state(p)
        assert len(state.targets) == 1
        assert "good" in state.targets


# ---------------------------------------------------------------------------
# save_local_state
# ---------------------------------------------------------------------------


class TestSaveLocalState:
    def test_round_trip(self, tmp_path: Path) -> None:
        p = tmp_path / ".popcorn.local.json"
        state = LocalState(
            default_target="my-app",
            targets={
                "my-app": Target(
                    workspace_id="ws_1",
                    workspace_name="Acme",
                    conversation_id="conv_1",
                    site_name="my-app",
                    profile="default",
                    deployed_at="2026-03-31T10:00:00Z",
                )
            },
        )
        save_local_state(state, p)
        loaded = load_local_state(p)
        assert loaded.default_target == "my-app"
        assert loaded.targets["my-app"].workspace_id == "ws_1"

    def test_empty_fields_omitted(self, tmp_path: Path) -> None:
        p = tmp_path / ".popcorn.local.json"
        state = LocalState(
            default_target="x",
            targets={
                "x": Target(
                    workspace_id="ws_1",
                    conversation_id="conv_1",
                    site_name="x",
                )
            },
        )
        save_local_state(state, p)
        raw = json.loads(p.read_text())
        target_data = raw["targets"]["x"]
        assert "profile" not in target_data
        assert "deployed_at" not in target_data
        assert "workspace_name" not in target_data


# ---------------------------------------------------------------------------
# resolve_target
# ---------------------------------------------------------------------------


class TestResolveTarget:
    @pytest.fixture()
    def two_target_state(self) -> LocalState:
        return LocalState(
            default_target="staging",
            targets={
                "staging": Target(
                    workspace_id="ws_1",
                    conversation_id="conv_1",
                    site_name="pop-app",
                ),
                "production": Target(
                    workspace_id="ws_2",
                    conversation_id="conv_2",
                    site_name="app",
                ),
            },
        )

    def test_explicit_target_name(self, two_target_state: LocalState) -> None:
        t = resolve_target(two_target_state, target_name="production")
        assert t is not None
        assert t.conversation_id == "conv_2"

    def test_explicit_target_not_found(self, two_target_state: LocalState) -> None:
        t = resolve_target(two_target_state, target_name="nonexistent")
        assert t is None

    def test_default_target(self, two_target_state: LocalState) -> None:
        t = resolve_target(two_target_state)
        assert t is not None
        assert t.conversation_id == "conv_1"  # staging is default

    def test_workspace_match(self, two_target_state: LocalState) -> None:
        two_target_state.default_target = ""  # no default
        t = resolve_target(two_target_state, workspace_id="ws_2")
        assert t is not None
        assert t.conversation_id == "conv_2"

    def test_single_target_fallback(self) -> None:
        state = LocalState(
            targets={
                "only": Target(
                    workspace_id="ws_1",
                    conversation_id="conv_1",
                    site_name="only",
                )
            }
        )
        t = resolve_target(state)
        assert t is not None
        assert t.site_name == "only"

    def test_no_targets(self) -> None:
        t = resolve_target(LocalState())
        assert t is None

    def test_multiple_no_match(self, two_target_state: LocalState) -> None:
        two_target_state.default_target = ""
        with pytest.raises(AmbiguousTargetError) as exc_info:
            resolve_target(two_target_state, workspace_id="ws_other")
        assert "staging" in exc_info.value.available
        assert "production" in exc_info.value.available


# ---------------------------------------------------------------------------
# upsert_target
# ---------------------------------------------------------------------------


class TestUpsertTarget:
    def test_new_target(self) -> None:
        state = LocalState()
        target = Target(
            workspace_id="ws_1",
            conversation_id="conv_1",
            site_name="my-app",
        )
        key = upsert_target(state, target)
        assert key == "my-app"
        assert state.default_target == "my-app"
        assert state.targets["my-app"] is target

    def test_update_existing(self) -> None:
        old = Target(
            workspace_id="ws_1",
            conversation_id="conv_1",
            site_name="my-app",
            deployed_at="2026-01-01T00:00:00Z",
        )
        state = LocalState(default_target="my-app", targets={"my-app": old})

        new = Target(
            workspace_id="ws_1",
            conversation_id="conv_1",
            site_name="my-app",
            deployed_at="2026-03-31T00:00:00Z",
        )
        key = upsert_target(state, new)
        assert key == "my-app"
        assert state.targets["my-app"].deployed_at == "2026-03-31T00:00:00Z"

    def test_collision_uses_profile(self) -> None:
        state = LocalState(
            targets={
                "my-app": Target(
                    workspace_id="ws_1",
                    conversation_id="conv_1",
                    site_name="my-app",
                )
            }
        )
        new = Target(
            workspace_id="ws_2",
            conversation_id="conv_2",
            site_name="my-app",
            profile="dev",
        )
        key = upsert_target(state, new)
        assert key == "my-app@dev"
        assert len(state.targets) == 2

    def test_collision_uses_workspace_name(self) -> None:
        state = LocalState(
            targets={
                "my-app": Target(
                    workspace_id="ws_1",
                    conversation_id="conv_1",
                    site_name="my-app",
                )
            }
        )
        new = Target(
            workspace_id="ws_2",
            conversation_id="conv_2",
            site_name="my-app",
            workspace_name="Dev Team",
        )
        key = upsert_target(state, new)
        assert key == "my-app@Dev Team"

    def test_collision_uses_workspace_id_prefix(self) -> None:
        state = LocalState(
            targets={
                "my-app": Target(
                    workspace_id="ws_1",
                    conversation_id="conv_1",
                    site_name="my-app",
                ),
                "my-app@dev": Target(
                    workspace_id="ws_2",
                    conversation_id="conv_2",
                    site_name="my-app",
                    profile="dev",
                ),
            }
        )
        new = Target(
            workspace_id="ws_3_long_id_here",
            conversation_id="conv_3",
            site_name="my-app",
            profile="dev",  # same profile as existing collision key
        )
        key = upsert_target(state, new)
        assert key == "my-app@ws_3_lon"

    def test_set_default_false(self) -> None:
        state = LocalState(default_target="old")
        target = Target(
            workspace_id="ws_1",
            conversation_id="conv_1",
            site_name="new-app",
        )
        upsert_target(state, target, set_default=False)
        assert state.default_target == "old"

    def test_sets_default_on_upsert(self) -> None:
        state = LocalState(
            default_target="old",
            targets={
                "old": Target(workspace_id="ws_1", conversation_id="c1", site_name="old"),
                "new": Target(workspace_id="ws_2", conversation_id="c2", site_name="new"),
            },
        )
        updated = Target(
            workspace_id="ws_2", conversation_id="c2", site_name="new", deployed_at="now"
        )
        upsert_target(state, updated)
        assert state.default_target == "new"


# ---------------------------------------------------------------------------
# make_target
# ---------------------------------------------------------------------------


class TestMakeTarget:
    def test_sets_deployed_at(self) -> None:
        t = make_target(
            workspace_id="ws_1",
            conversation_id="conv_1",
            site_name="app",
        )
        assert t.deployed_at  # non-empty
        assert t.deployed_at.endswith("Z")

    def test_all_fields(self) -> None:
        t = make_target(
            workspace_id="ws_1",
            conversation_id="conv_1",
            site_name="app",
            workspace_name="Acme",
            profile="dev",
        )
        assert t.workspace_name == "Acme"
        assert t.profile == "dev"
