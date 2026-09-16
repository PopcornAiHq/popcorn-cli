"""`popcorn webhook` — intake webhooks on a channel.

Handlers import their `..cli` helpers *inside* the function body: cli.py
imports this package at module load to build the parser, so a module-level
import would be a cycle.

Two shapes of subcommand live here and the split is the API's, not ours.
`create`, `list` and `deliveries` are channel-scoped — a webhook is created
in a channel and listed with its siblings. Everything else addresses one
webhook directly, because the server authorizes those against the webhook's
own channel, looked up from the id. `_resolve_id` is what lets the second
group still be driven by the name the first group prints.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from popcorn_core import operations
from popcorn_core.errors import PopcornError

from ..registry import Argument, Command, Subcommand, register

_CHANNEL = Argument("conversation", "Channel name or UUID", positional=True, flag_alias="--channel")
# Every by-id subcommand takes this. Named `webhook` rather than `webhook_id`
# because a name is accepted too — `_resolve_id` turns either into the UUID
# the route needs.
_WEBHOOK = Argument("webhook", "Webhook UUID, or its name with --channel", positional=True)
_WEBHOOK_CHANNEL = Argument("channel", "Channel holding the webhook — only needed for a name")

_ACTION_MODES = ["silent", "as_is", "ai_enhanced", "trigger_workflow"]


def _resolve_id(args: argparse.Namespace, client: Any) -> str:
    """The UUID of the webhook this invocation addresses."""
    return operations.resolve_webhook_id(client, args.webhook, getattr(args, "channel", None))


def _hook_lines(hook: dict[str, Any], *, show_url: bool = False) -> list[str]:
    """Render one webhook for humans.

    The ingest URL is opt-in for the same reason `list` hides it: the token in
    it IS the credential, so printing it by default puts a working key into
    terminal scrollback of anyone who only wanted to read a name.
    """
    lines = [
        f"{hook.get('name', '?')}  ({hook.get('id', '?')})",
        f"  active:       {hook.get('is_active')}",
        f"  action mode:  {hook.get('action_mode', '?')}",
        f"  enforce HMAC: {hook.get('enforce_hmac')}",
    ]
    if hook.get("trigger_flow_id"):
        lines.append(f"  triggers:     {hook['trigger_flow_id']}")
    if hook.get("description"):
        lines.append(f"  description:  {hook['description']}")
    if hook.get("created_at"):
        lines.append(f"  created:      {hook['created_at']}")
    if show_url and hook.get("url"):
        lines.append(f"  url:          {hook['url']}")
    elif hook.get("url"):
        lines.append("")
        lines.append("Ingest URL hidden (it carries a secret token) — pass --show-url.")
    return lines


# ---------------------------------------------------------------------------
# Channel-scoped
# ---------------------------------------------------------------------------


def _webhook_create(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    flow_id = getattr(args, "trigger_flow_id", None)
    flow_name = getattr(args, "trigger_flow_name", None)
    if getattr(args, "action_mode", None) == "trigger_workflow" and not (flow_id or flow_name):
        e = PopcornError("--action-mode=trigger_workflow needs the flow to start")
        e.hint = "pass --trigger-flow-name <name> (see `popcorn flow list`)"
        raise e
    client = _get_client(args)
    resp = operations.create_webhook(
        client,
        args.conversation,
        args.name,
        description=getattr(args, "description", None),
        avatar_url=getattr(args, "avatar_url", None),
        action_mode=getattr(args, "action_mode", None),
        trigger_flow_id=flow_id,
        trigger_flow_name=flow_name,
    )
    _output(args, resp, f"Created webhook '{args.name}' for {args.conversation}")


def _webhook_list(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    client = _get_client(args)
    resp = operations.list_webhooks(client, args.conversation)
    hooks = resp if isinstance(resp, list) else resp.get("webhooks", [resp])
    show_url = getattr(args, "show_url", False)
    lines = [f"Webhooks for {args.conversation} ({len(hooks)}):"]
    for h in hooks:
        lines.append(f"  {h.get('id', '?')}  {h.get('name', '?')}")
        if show_url and h.get("url"):
            lines.append(f"    {h['url']}")
    # The ingest URL's token IS the credential — anyone holding it can post
    # to the channel — so it is opt-in rather than printed by default, and
    # the footer is what stops that decision from sending people back to
    # `--json` to find it.
    if not show_url and any(h.get("url") for h in hooks):
        lines.append("")
        lines.append("Ingest URLs hidden (they carry a secret token) — pass --show-url.")
    _output(args, resp, "\n".join(lines))


def _webhook_deliveries(args: argparse.Namespace) -> None:
    from ..cli import _format_payload_preview, _get_client, _output

    client = _get_client(args)
    resp = operations.list_webhook_deliveries(
        client,
        args.conversation,
        limit=getattr(args, "limit", 50),
        since=getattr(args, "since", None),
        after=getattr(args, "after", None),
        status=getattr(args, "status", None),
        include=getattr(args, "include", None),
    )
    deliveries = resp if isinstance(resp, list) else resp.get("deliveries", [resp])
    lines = [f"Deliveries for {args.conversation} ({len(deliveries)}):"]
    for d in deliveries:
        wh_name = d.get("webhook_name", d.get("webhook_id", "?"))
        ts = d.get("created_at", "?")
        lines.append(f"  {d.get('id', '?')}  {wh_name}  {ts}")
        if "payload_raw" in d:
            lines.append(f"    payload: {_format_payload_preview(d['payload_raw'])}")
    _output(args, resp, "\n".join(lines))


def _webhook_event_types(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    client = _get_client(args)
    resp = operations.webhook_event_types(client)
    sources = resp.get("sources", [])
    modes = resp.get("action_modes", [])
    source_names = [s.get("name", str(s)) if isinstance(s, dict) else str(s) for s in sources]
    lines = ["Webhook event types:"]
    lines.append("  sources: " + (", ".join(source_names) or "—"))
    lines.append("  action_modes: " + (", ".join(map(str, modes)) or "—"))
    _output(args, resp, "\n".join(lines))


# ---------------------------------------------------------------------------
# Webhook-scoped
# ---------------------------------------------------------------------------


def _webhook_get(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    client = _get_client(args)
    resp = operations.get_webhook(client, _resolve_id(args, client))
    hook = resp.get("webhook") or resp
    _output(args, resp, "\n".join(_hook_lines(hook, show_url=getattr(args, "show_url", False))))


def _webhook_update(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    is_active = _tri_state(args, "activate", "deactivate", "--activate/--deactivate")
    enforce_hmac = _tri_state(
        args, "enforce_hmac", "no_enforce_hmac", "--enforce-hmac/--no-enforce-hmac"
    )
    fields: dict[str, Any] = {
        "name": getattr(args, "name", None),
        "description": getattr(args, "description", None),
        "avatar_url": getattr(args, "avatar_url", None),
        "action_mode": getattr(args, "action_mode", None),
        "is_active": is_active,
        "enforce_hmac": enforce_hmac,
    }
    if all(v is None for v in fields.values()):
        raise PopcornError(
            "Nothing to update — name one of --name, --description, --avatar-url, "
            "--action-mode, --activate/--deactivate, --enforce-hmac/--no-enforce-hmac.",
            error_code="validation",
            hint="popcorn webhook get <webhook>",
        )
    client = _get_client(args)
    resp = operations.update_webhook(client, _resolve_id(args, client), **fields)
    hook = resp.get("webhook") or resp
    lines = [f"Updated webhook '{hook.get('name', args.webhook)}'"]
    lines += _hook_lines(hook)
    # Enforcement is conditional on a secret existing, and the server writes
    # False back rather than refusing — so asking for it and not getting it is
    # a silent no-op unless the result is read.
    if enforce_hmac and not hook.get("enforce_hmac"):
        lines.append("")
        lines.append(
            "HMAC enforcement did NOT take: this webhook has no HMAC secret, "
            "and the server writes enforcement back as off rather than failing. "
            "The secret has to be created before this flag does anything."
        )
    _output(args, resp, "\n".join(lines))


def _tri_state(args: argparse.Namespace, on: str, off: str, spelling: str) -> bool | None:
    """Fold a pair of opposing store_true flags into True / False / unset.

    Two flags rather than one `--flag true|false` so the common case reads as
    a verb; unset has to stay distinguishable from False because the PATCH
    leaves out what it is not given, and a False would turn every unrelated
    edit into an unintended disable.
    """
    if getattr(args, on, False) and getattr(args, off, False):
        raise PopcornError(f"{spelling} contradict each other", error_code="validation")
    if getattr(args, on, False):
        return True
    if getattr(args, off, False):
        return False
    return None


def _webhook_delete(args: argparse.Namespace) -> None:
    from ..cli import _confirm, _get_client, _output

    client = _get_client(args)
    webhook_id = _resolve_id(args, client)
    # Named, not just addressed: a UUID in a confirmation prompt tells the
    # reader nothing about what is about to stop receiving deliveries.
    hook = (operations.get_webhook(client, webhook_id) or {}).get("webhook") or {}
    name = hook.get("name", args.webhook)
    if not _confirm(
        args, f"Delete webhook '{name}' ({webhook_id})? Its URL stops accepting posts."
    ):
        _output(args, {"ok": False, "cancelled": True}, "Cancelled.")
        return
    resp = operations.delete_webhook(client, webhook_id)
    _output(args, resp, f"Deleted webhook '{name}' ({webhook_id})")


def _webhook_rules_get(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    client = _get_client(args)
    resp = operations.get_webhook_override_rules(client, _resolve_id(args, client))
    rules = resp.get("rules") or {}
    if not rules:
        _output(args, resp, "No override rules — every event uses the provider's defaults.")
        return
    lines = [f"Override rules ({len(rules)}):"]
    for pattern, patch in sorted(rules.items()):
        lines.append(f"  {pattern}")
        for key, value in sorted(patch.items()):
            lines.append(f"    {key}: {value}")
    _output(args, resp, "\n".join(lines))


def _webhook_rules_set(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output, _read_json_object

    rules = _read_json_object(args.rules, "rules")
    client = _get_client(args)
    resp = operations.set_webhook_override_rules(client, _resolve_id(args, client), rules)
    stored = resp.get("rules") or {}
    _output(args, resp, f"Set {len(stored)} override rule(s).")


# ---------------------------------------------------------------------------
# Unauthenticated
# ---------------------------------------------------------------------------


def _webhook_send(args: argparse.Namespace) -> None:
    """Fire a webhook's ingest URL with a JSON payload.

    The client is built only when the target needs resolving: an ingest URL
    target posts to an unauthenticated host, so it must work without a login.
    """
    from ..cli import _get_client, _output, _read_json_object

    payload = _read_json_object(args.payload, "payload") if args.payload else {}
    target = args.target
    if operations.is_webhook_url(target):
        url = target
    else:
        url = operations.resolve_webhook_url(
            _get_client(args), target, getattr(args, "channel", None)
        )
    result = operations.send_webhook(url, payload)
    body = result["response"]
    rendered = json.dumps(body, indent=2) if isinstance(body, dict | list) else str(body)
    _output(args, result, f"HTTP {result['status']} → {url}\n{rendered}")


register(
    Command(
        name="webhook",
        category="webhooks",
        description=(
            "Webhook commands (create, list, get, update, delete, "
            "override-rules get/set, deliveries, event-types, send)"
        ),
        subcommands=[
            Subcommand(
                "create",
                "Create a webhook",
                _webhook_create,
                [
                    _CHANNEL,
                    Argument("name", "Webhook name", positional=True),
                    Argument("description", "Webhook description", type=str),
                    Argument("avatar-url", "Avatar URL", type=str),
                    Argument(
                        "action-mode",
                        "How deliveries are processed",
                        type=str,
                        choices=_ACTION_MODES,
                    ),
                    # One flow, two ways to name it, never both. `popcorn flow
                    # list` reports a flow's NAME in its `id` field, so for a
                    # bundle flow the id you are handed ("alert_webhook") is
                    # not a UUID and --trigger-flow-id would 422 on it.
                    Argument(
                        "trigger-flow-id",
                        "Flow UUID to start (with --action-mode=trigger_workflow)",
                        type=str,
                        exclusive_group="trigger_flow",
                    ),
                    Argument(
                        "trigger-flow-name",
                        "Flow NAME to start, as shown by `popcorn flow list` "
                        "(with --action-mode=trigger_workflow)",
                        type=str,
                        exclusive_group="trigger_flow",
                    ),
                ],
            ),
            Subcommand(
                "list",
                "List webhooks for a channel",
                _webhook_list,
                [
                    _CHANNEL,
                    Argument(
                        "show-url",
                        "Print each webhook's ingest URL — it embeds a secret token, "
                        "so it is hidden by default",
                        action="store_true",
                    ),
                ],
            ),
            Subcommand(
                "get",
                "Show one webhook's settings",
                _webhook_get,
                [
                    _WEBHOOK,
                    _WEBHOOK_CHANNEL,
                    Argument(
                        "show-url",
                        "Print the ingest URL — it embeds a secret token, "
                        "so it is hidden by default",
                        action="store_true",
                    ),
                ],
            ),
            Subcommand(
                "update",
                "Change a webhook's settings",
                _webhook_update,
                [
                    _WEBHOOK,
                    _WEBHOOK_CHANNEL,
                    Argument("name", "New name", type=str),
                    Argument("description", "New description", type=str),
                    Argument("avatar-url", "New avatar URL", type=str),
                    Argument(
                        "action-mode",
                        "How deliveries are processed",
                        type=str,
                        choices=_ACTION_MODES,
                    ),
                    Argument("activate", "Resume accepting deliveries", action="store_true"),
                    Argument(
                        "deactivate",
                        "Stop accepting deliveries, keeping the webhook",
                        action="store_true",
                    ),
                    Argument(
                        "enforce-hmac",
                        "Reject posts without a valid signature — only takes effect "
                        "once the webhook has an HMAC secret",
                        action="store_true",
                    ),
                    Argument(
                        "no-enforce-hmac",
                        "Accept unsigned posts again",
                        action="store_true",
                    ),
                    # The flow binding is absent on purpose: it is fixed at
                    # creation and bound by name, and the server rejects the id
                    # form outright. Rebinding means a new webhook.
                ],
            ),
            Subcommand(
                "delete",
                "Delete a webhook (prompts; --yes to skip)",
                _webhook_delete,
                [_WEBHOOK, _WEBHOOK_CHANNEL],
            ),
            Subcommand(
                "override-rules",
                "Per-event overrides on one webhook",
                subcommands=[
                    Subcommand(
                        "get",
                        "Show a webhook's override rules",
                        _webhook_rules_get,
                        [_WEBHOOK, _WEBHOOK_CHANNEL],
                    ),
                    Subcommand(
                        "set",
                        "Replace a webhook's override rules wholesale",
                        _webhook_rules_set,
                        [
                            _WEBHOOK,
                            Argument(
                                "rules",
                                'JSON object keyed by "event_type.action" '
                                "('@-' reads stdin, '@path' reads a file)",
                                positional=True,
                            ),
                            _WEBHOOK_CHANNEL,
                        ],
                    ),
                ],
            ),
            Subcommand(
                "deliveries",
                "List webhook deliveries",
                _webhook_deliveries,
                [
                    _CHANNEL,
                    Argument("limit", "Max results (1-100)", type=int),
                    Argument("since", "ISO timestamp — deliveries after this", type=str),
                    Argument(
                        "after", "Delivery UUID — deliveries after this ID (cursor)", type=str
                    ),
                    Argument("status", "Filter: completed,ignored,failed,processing", type=str),
                    Argument(
                        "include",
                        "Comma-separated optional fields to hydrate (e.g. payload_raw)",
                        type=str,
                    ),
                ],
            ),
            Subcommand(
                "event-types",
                "List valid webhook sources and action modes",
                _webhook_event_types,
            ),
            Subcommand(
                "send",
                "Send a payload to a webhook's ingest URL",
                _webhook_send,
                [
                    Argument(
                        "target", "Ingest URL, webhook UUID, or webhook name", positional=True
                    ),
                    Argument(
                        "payload",
                        "JSON body (default {}; '@-' reads stdin, '@path' reads a file)",
                        positional=True,
                        nargs="?",
                    ),
                    Argument(
                        "channel",
                        "Channel name or UUID — needed only when <target> is a name",
                        type=str,
                    ),
                ],
            ),
        ],
    )
)
