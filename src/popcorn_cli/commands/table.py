"""`popcorn table` — the channel's agent-store data (tables, rows, scalars).

Thin, faithful views over /api/v1/conversations/{id}/data-store. These are the
observation commands a template author needs: after a flow runs, did the row
change?

The response keys are the API's, not the obvious ones — rows come back under
`records`, audit entries under `events`, a scalar's value under `scalar.value`,
and a table's columns under `table.schema_version.schema_def.columns`.
"""

from __future__ import annotations

import argparse
import json

from popcorn_core import operations

from ..registry import Argument, Command, Subcommand, register

_CHANNEL = Argument("channel", "Channel name (#general) or UUID", required=True)
_NAME = Argument("name", "Table name", positional=True)
_RECORD_ID = Argument("record_id", "Record id", positional=True)


def _table_list(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    resp = operations.list_tables(_get_client(args), args.channel)
    tables = resp.get("tables", [])
    lines = [f"Tables in {args.channel} ({len(tables)}):"]
    for t in tables:
        if isinstance(t, dict):
            lines.append(f"  {t.get('name', '?'):<24} {t.get('record_count', '?')} rows")
        else:
            lines.append(f"  {t}")
    _output(args, resp, "\n".join(lines))


def _table_schema(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    resp = operations.get_table(_get_client(args), args.channel, args.name)
    table = resp.get("table", resp)
    schema_version = table.get("schema_version") or {}
    cols = (schema_version.get("schema_def") or {}).get("columns", [])
    lines = [f"{args.name} (v{schema_version.get('version', '?')}, {len(cols)} columns):"]
    for c in cols:
        flags = [k for k in ("unique", "required", "internal", "pii", "restricted") if c.get(k)]
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        lines.append(f"  {c.get('name', '?'):<24} {c.get('type', '?'):<10}{suffix}")
    _output(args, resp, "\n".join(lines))


def _table_rows(args: argparse.Namespace) -> None:
    from ..cli import _attach_pagination, _get_client, _output, _read_json_object

    raw_filter = getattr(args, "filter", None)
    resp = operations.list_records(
        _get_client(args),
        args.channel,
        args.name,
        filter=_read_json_object(raw_filter, "--filter") if raw_filter else None,
        limit=getattr(args, "limit", None) or 50,
        cursor=getattr(args, "cursor", None),
    )
    rows = resp.get("records", [])
    cursor = resp.get("cursor")
    _attach_pagination(resp, {"cursor": cursor} if resp.get("has_more") and cursor else None)
    lines = [f"{args.name} rows ({len(rows)}):"]
    for r in rows:
        cells = r.get("data", {})
        lines.append(f"  {r.get('id', '?')}  {json.dumps(cells, default=str)[:160]}")
    _output(args, resp, "\n".join(lines))


def _row_get(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    resp = operations.get_record(_get_client(args), args.channel, args.name, args.record_id)
    _output(args, resp, json.dumps(resp.get("record", resp), indent=2, default=str))


def _row_patch(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output, _read_json_object

    data = _read_json_object(args.data, "--data")
    resp = operations.patch_record(_get_client(args), args.channel, args.name, args.record_id, data)
    _output(args, resp, f"Patched {args.name} record {args.record_id}")


def _row_delete(args: argparse.Namespace) -> None:
    from ..cli import _confirm, _get_client, _output, _status

    if not _confirm(args, f"Delete {args.name} record {args.record_id}?"):
        _status("Cancelled.")
        return
    resp = operations.delete_record(_get_client(args), args.channel, args.name, args.record_id)
    _output(args, resp, f"Deleted {args.name} record {args.record_id}")


def _scalar_list(args: argparse.Namespace) -> None:
    from ..cli import _attach_pagination, _get_client, _output

    resp = operations.list_scalars(
        _get_client(args),
        args.channel,
        limit=getattr(args, "limit", None) or 50,
        cursor=getattr(args, "cursor", None),
    )
    scalars = resp.get("scalars", [])
    cursor = resp.get("cursor")
    _attach_pagination(resp, {"cursor": cursor} if resp.get("has_more") and cursor else None)
    lines = [f"Scalars in {args.channel} ({len(scalars)}):"]
    for s in scalars:
        if isinstance(s, dict):
            lines.append(f"  {s.get('key', '?'):<28} {str(s.get('value', ''))[:80]}")
        else:
            lines.append(f"  {s}")
    _output(args, resp, "\n".join(lines))


def _scalar_get(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    resp = operations.get_scalar(_get_client(args), args.channel, args.key)
    scalar = resp.get("scalar") or {}
    _output(args, resp, str(scalar.get("value", "")))


def _scalar_set(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    resp = operations.set_scalar(_get_client(args), args.channel, args.key, args.value)
    _output(args, resp, f"Set {args.key}")


def _table_audit(args: argparse.Namespace) -> None:
    from ..cli import _attach_pagination, _get_client, _output

    resp = operations.list_store_audit(
        _get_client(args),
        args.channel,
        limit=getattr(args, "limit", None) or 50,
        cursor=getattr(args, "cursor", None),
    )
    events = resp.get("events", [])
    cursor = resp.get("cursor")
    _attach_pagination(resp, {"cursor": cursor} if resp.get("has_more") and cursor else None)
    lines = [f"Audit ({len(events)}):"]
    for e in events:
        lines.append(
            f"  {e.get('changed_at', '?')}  {e.get('operation', '?'):<8} "
            f"{e.get('entity_type', '?')}:{e.get('entity_id', '?')}"
        )
    _output(args, resp, "\n".join(lines))


register(
    Command(
        name="table",
        category="tables",
        description=(
            "Data-store commands (list, schema, rows, row get/patch/delete, "
            "scalar list/get/set, audit)"
        ),
        subcommands=[
            Subcommand("list", "List tables in a channel", _table_list, [_CHANNEL]),
            Subcommand("schema", "Show a table's columns", _table_schema, [_NAME, _CHANNEL]),
            Subcommand(
                "rows",
                "List rows in a table",
                _table_rows,
                [
                    _NAME,
                    _CHANNEL,
                    Argument(
                        "filter",
                        'Equality map, e.g. \'{"Status":"firing"}\'',
                        type=str,
                    ),
                    Argument("limit", "Max rows (default 50)", type=int),
                    Argument(
                        "cursor",
                        "Cursor from a previous response's pagination.next",
                        type=str,
                    ),
                ],
            ),
            Subcommand(
                "row",
                "Operate on one row (get, patch, delete)",
                None,
                [],
                [
                    Subcommand("get", "Get one row", _row_get, [_NAME, _RECORD_ID, _CHANNEL]),
                    Subcommand(
                        "patch",
                        "Patch one row's columns",
                        _row_patch,
                        [
                            _NAME,
                            _RECORD_ID,
                            _CHANNEL,
                            Argument(
                                "data",
                                "JSON object of columns to set "
                                "(use '@-' for stdin, '@path' for a file)",
                                type=str,
                                required=True,
                            ),
                        ],
                    ),
                    Subcommand(
                        "delete",
                        "Delete one row",
                        _row_delete,
                        [_NAME, _RECORD_ID, _CHANNEL],
                    ),
                ],
            ),
            Subcommand(
                "scalar",
                "Channel scalars (list, get, set)",
                None,
                [],
                [
                    Subcommand(
                        "list",
                        "List scalars",
                        _scalar_list,
                        [
                            _CHANNEL,
                            Argument("limit", "Max scalars (default 50)", type=int),
                            Argument(
                                "cursor",
                                "Cursor from a previous response's pagination.next",
                                type=str,
                            ),
                        ],
                    ),
                    Subcommand(
                        "get",
                        "Read one scalar",
                        _scalar_get,
                        [Argument("key", "Scalar key", positional=True), _CHANNEL],
                    ),
                    Subcommand(
                        "set",
                        "Write one scalar",
                        _scalar_set,
                        [
                            Argument("key", "Scalar key", positional=True),
                            Argument(
                                "value",
                                "Scalar value (strings on the wire)",
                                positional=True,
                            ),
                            _CHANNEL,
                        ],
                    ),
                ],
            ),
            Subcommand(
                "audit",
                "Recent data-store audit entries",
                _table_audit,
                [
                    _CHANNEL,
                    Argument("limit", "Max entries (default 50)", type=int),
                    Argument(
                        "cursor",
                        "Cursor from a previous response's pagination.next",
                        type=str,
                    ),
                ],
            ),
        ],
    )
)
