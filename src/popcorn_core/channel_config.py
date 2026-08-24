"""Parameter merging and the config lint, for `popcorn channel-config`.

The one thing this module exists to get right: `PUT
/channel-config/parameters` REPLACES the whole `channel_parameters` section.
A `params set tone=crisp` that sent only `{"tone": "crisp"}` would delete
every other parameter on the channel, silently and successfully. So every
per-key edit here is read-merge-write, and inherits the backend's documented
last-write-wins — two concurrent edits race on the whole document and the
loser's keys vanish with no error.

The lint is not computed here. `inspect_channel_config` returns `comparison`
from `backend:lib/temporal/dsl/channel_usage.py — compare_channel_usage`;
this module only decides which of its five fields mean "a run will fail"
versus "untidy".
"""

from __future__ import annotations

import json
from typing import Any

from .errors import PopcornError

# The three comparison fields that make a run fail. The other two —
# unused_parameters, unused_integrations — are legal: a config shared across
# flows carries keys any single flow does not read, so failing on them would
# make --strict useless on every real channel.
FATAL_COMPARISON_FIELDS = (
    "missing_parameters",
    "missing_integrations",
    "provider_mismatches",
)

_RESERVED_PARAMETER_NAMES = frozenset({"integrations"})


def parse_assignments(pairs: list[str]) -> dict[str, Any]:
    """`["tone=crisp", "retries=3"]` → `{"tone": "crisp", "retries": 3}`.

    Values are parsed as JSON when they parse, so `retries=3` is a number and
    `enabled=true` a bool — matching how the same value would be written in
    the config's YAML. A bare word that is not valid JSON stays a string,
    which is why `tone=crisp` works without quoting.
    """
    out: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise PopcornError(
                f"{pair!r} is not key=value",
                error_code="validation",
                hint="write: popcorn channel-config params set tone=crisp",
            )
        key, _, raw = pair.partition("=")
        key = key.strip()
        if not key:
            raise PopcornError(f"{pair!r} has an empty key", error_code="validation")
        if key in _RESERVED_PARAMETER_NAMES:
            # The backend rejects this on the round trip; saying so here
            # names the rule instead of surfacing a 400.
            raise PopcornError(
                f"{key!r} is reserved — named integrations are set with "
                "'channel-config integrations set'",
                error_code="validation",
            )
        try:
            out[key] = json.loads(raw)
        except json.JSONDecodeError:
            out[key] = raw
    return out


def merge_parameters(current: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    """The whole section to PUT after setting `updates`.

    A shallow merge on purpose: the endpoint takes one flat map and the CLI
    offers no path syntax, so a key whose value is an object is replaced
    wholesale rather than deep-merged. Deep-merging would make `--replace`
    the only way to ever shrink a nested value.
    """
    merged = dict(current)
    merged.update(updates)
    return merged


def remove_parameters(current: dict[str, Any], keys: list[str]) -> tuple[dict[str, Any], list[str]]:
    """The whole section to PUT after unsetting `keys`, plus those not present.

    Absent keys are reported rather than raised: unsetting something already
    gone is the desired end state, and failing would make the command
    non-idempotent for no gain.
    """
    remaining = dict(current)
    missing = [key for key in keys if key not in remaining]
    for key in keys:
        remaining.pop(key, None)
    return remaining, missing


def fatal_findings(comparison: dict[str, Any]) -> dict[str, list[str]]:
    """Only the comparison fields that mean a run will fail, non-empty ones."""
    out: dict[str, list[str]] = {}
    for field in FATAL_COMPARISON_FIELDS:
        values = comparison.get(field) or []
        if values:
            out[field] = list(values)
    return out


def parameters_of(response: dict[str, Any]) -> dict[str, Any]:
    """`channel_parameters` from an inspect or update response."""
    params = response.get("channel_parameters")
    return dict(params) if isinstance(params, dict) else {}
