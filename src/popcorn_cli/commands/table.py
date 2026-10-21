"""`popcorn table` — the channel's agent-store data (tables, rows, scalars).

Thin, faithful views over /api/v1/conversations/{id}/data-store. These are the
observation commands a template author needs: after a flow runs, did the row
change?

The response keys are the API's, not the obvious ones — rows come back under
`records`, audit entries under `events`, a scalar's value under `scalar.value`,
and a table's columns under `table.schema_version.schema_def.columns`.

`row create` and `import` are the writes: both go through the one insert
endpoint, which answers with ids and counts rather than rows.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import shlex
import sys
from pathlib import Path
from typing import Any

from popcorn_core import operations
from popcorn_core.errors import APIError, PopcornError
from popcorn_core.resolve import resolve_conversation

from ..registry import Argument, Command, Subcommand, register

_CHANNEL = Argument("channel", "Channel name (#general) or UUID", required=True)
_NAME = Argument("name", "Table name", positional=True)
_RECORD_ID = Argument("record_id", "Record id", positional=True)
# Repeatable because the API takes a list: several columns together identify
# a row. The help says "replaces" because that is what this endpoint does on a
# hit — it takes no conflict policy, so columns the new row omits are emptied.
_MERGE_ON = Argument(
    "merge-on",
    "Column identifying an existing row to write over instead of adding one "
    "(repeatable). A match REPLACES that row: columns the new row omits are emptied",
    action="append",
)


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


def _column_line(c: dict) -> str:
    """Render one column: name, type, governance flags, merge policy.

    `merge` is a design-time attribute that changes what a write DOES — concat
    accumulates onto the existing value, replace overwrites — but it leaves no
    trace in the row data, so it has to be visible here. Only `concat` is
    printed: `replace` is the default on every column and listing it everywhere
    would bury the one column that accumulates.
    """
    flags = [k for k in ("unique", "required", "internal", "pii", "restricted") if c.get(k)]
    if c.get("merge") == "concat":
        separator = c.get("merge_separator")
        # None means the server applies its "\n" default — don't render an
        # explicit separator we cannot vouch for.
        flags.append(f"concat sep={separator!r}" if separator else "concat")
    suffix = f"  [{', '.join(flags)}]" if flags else ""
    return f"  {c.get('name', '?'):<24} {c.get('type', '?'):<10}{suffix}"


def _table_schema(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output

    resp = operations.get_table(_get_client(args), args.channel, args.name)
    table = resp.get("table", resp)
    schema_version = table.get("schema_version") or {}
    cols = (schema_version.get("schema_def") or {}).get("columns", [])
    lines = [f"{args.name} (v{schema_version.get('version', '?')}, {len(cols)} columns):"]
    lines.extend(_column_line(c) for c in cols)
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


def _row_create(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output, _read_json_object, _status

    data = _read_json_object(args.data, "--data")
    try:
        _require_finite(data)
    except ValueError as e:
        raise PopcornError(f"--data: {e}", error_code="validation") from None
    client = _get_client(args)
    check = (
        f"check with `{_command(args, 'table', 'rows', args.name, '--channel', args.channel)}` "
        "before creating it again"
    )
    try:
        resp = operations.insert_records(
            client, args.channel, args.name, [data], merge_on=getattr(args, "merge_on", None)
        )
    except APIError as e:
        if _outcome_unknown(e):
            e.hint = f"the row may or may not have been written; {check}"
        raise
    try:
        ids, created, updated = _insert_result(resp)
    except ValueError as e:
        raise _uncertain_answer(e, f"the row may or may not have been written; {check}") from None
    if not ids:
        raise _uncertain_answer(
            ValueError("a record id"), f"the row may or may not have been written; {check}"
        )
    record_id = ids[0]
    counts = {"created": created, "updated": updated}
    # A table with its own merge key upserts even without --merge-on, so the
    # response, not the flag, says whether this was a new row.
    verb = "Merged into existing" if counts["updated"] else "Created"
    # The insert answers with ids only; reading the row back is what lets this
    # print exactly what `row get` would — the stored row, after the server's
    # key trimming, computed columns and any merge.
    try:
        got = operations.get_record(client, args.channel, args.name, record_id)
    except APIError as e:
        # The write already happened. Failing here would invite a retry that
        # adds the row twice.
        _output(
            args,
            {"record": {"id": record_id}, **counts, "read_back_error": str(e)},
            f"{verb} {args.name} record {record_id} (read-back failed: {e})",
        )
        return
    record = got.get("record", got)
    _status(f"{verb} {args.name} record {record_id}")
    _output(args, {"record": record, **counts}, json.dumps(record, indent=2, default=str))


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

    # Filters go to the server, which holds the whole log; narrowing here would
    # only ever search the page `limit` already cut, so a row that changed
    # further back would read as "never touched".
    resp = operations.list_store_audit(
        _get_client(args),
        args.channel,
        limit=getattr(args, "limit", None) or 50,
        cursor=getattr(args, "cursor", None),
        entity_type=getattr(args, "entity_type", None),
        entity_id=getattr(args, "entity_id", None),
        since=getattr(args, "since", None),
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


# ---------------------------------------------------------------------------
# `table import`
# ---------------------------------------------------------------------------

# Rows per insert request. The server caps a request well above this; 500
# keeps a request of wide rows under a proxy's body limit, and `--batch-size`
# moves it either way. No local ceiling: an oversized batch gets the server's
# own refusal on the first request, before anything is written.
DEFAULT_BATCH_SIZE = 500

_FORMAT_BY_SUFFIX = {".jsonl": "jsonl", ".ndjson": "jsonl", ".json": "json", ".csv": "csv"}

# Strict decimal, so a cell Python's float() would also take ("1_000", "nan",
# "infinity") is refused rather than stored as something the author never
# wrote.
_INT_RE = re.compile(r"[+-]?\d+")
_FLOAT_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_BOOLEANS = {"true": True, "yes": True, "1": True, "false": False, "no": False, "0": False}


def _require_finite(value: Any) -> None:
    """Refuse NaN, Infinity and numbers too large for a double (``1e400``).

    `json.loads` accepts all three, but JSON has no spelling for them, so the
    request could not be serialised. During an import that would surface as a
    crash mid-way, after earlier batches were already written.
    """
    try:
        json.dumps(value, allow_nan=False)
    except ValueError:
        raise ValueError("NaN, Infinity or an out-of-range number has no JSON form") from None


def _outcome_unknown(err: APIError) -> bool:
    """Whether a failed write may still have been applied.

    No response (status 0), a server error, or a success status whose body
    couldn't be read: in all three the server may have committed the write,
    so only a 4xx is a definite refusal.
    """
    return err.status_code == 0 or err.status_code >= 500 or 200 <= err.status_code < 300


def _insert_result(resp: Any) -> tuple[list[Any], int, int]:
    """`ids`, `created`, `updated` from an insert answer; ValueError if any is missing."""
    body = resp if isinstance(resp, dict) else {}
    ids, created, updated = body.get("ids"), body.get("created"), body.get("updated")
    if not isinstance(ids, list) or not isinstance(created, int) or not isinstance(updated, int):
        raise ValueError("its ids or created/updated counts")
    return ids, created, updated


def _uncertain_answer(err: ValueError, hint: str) -> APIError:
    # 200 rather than the real status, which `operations` doesn't hand back: it
    # was some success, and a 2xx is what `_outcome_unknown` reads as "the
    # write may have happened".
    wrapped = APIError(f"the server accepted the write but answered without {err}", status_code=200)
    if hint:
        wrapped.hint = hint
    return wrapped


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {value!r}") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {n}")
    return n


def _non_negative_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {value!r}") from None
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be 0 or more, got {n}")
    return n


# `commands --json` reports an argument's type by its converter's name.
_positive_int.__name__ = "int"
_non_negative_int.__name__ = "int"


def _input_format(path: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    fmt = _FORMAT_BY_SUFFIX.get(Path(path).suffix.lower()) if path != "-" else None
    if fmt is None:
        source = "stdin" if path == "-" else path
        raise PopcornError(
            f"cannot tell the format of {source}; pass --format jsonl|json|csv",
            error_code="validation",
        )
    return fmt


def _read_input(path: str) -> str:
    if path == "-":
        text = sys.stdin.read()
    else:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise PopcornError(f"Cannot read --file {path}: {e}", error_code="validation") from e
    # A spreadsheet's CSV export starts with a byte-order mark, which would
    # otherwise become part of the first column's name.
    return text.removeprefix("﻿")


def _json_rows(text: str, label: str) -> list[dict[str, Any]]:
    """A JSON array of row objects."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise PopcornError(f"{label} is not valid JSON: {e}", error_code="validation") from e
    if not isinstance(parsed, list):
        raise PopcornError(
            f"{label} must be a JSON array of row objects (one object per line is .jsonl)",
            error_code="validation",
        )
    for i, row in enumerate(parsed, 1):
        if not isinstance(row, dict):
            raise PopcornError(f"{label} item {i}: not a JSON object", error_code="validation")
        try:
            _require_finite(row)
        except ValueError as e:
            raise PopcornError(f"{label} item {i}: {e}", error_code="validation") from None
    return parsed


def _jsonl_rows(text: str, label: str) -> list[dict[str, Any]]:
    """One row object per line; blank lines are skipped."""
    rows: list[dict[str, Any]] = []
    for line_num, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as e:
            raise PopcornError(
                f"{label} line {line_num}: not valid JSON: {e}", error_code="validation"
            ) from e
        if not isinstance(row, dict):
            raise PopcornError(
                f"{label} line {line_num}: not a JSON object", error_code="validation"
            )
        try:
            _require_finite(row)
        except ValueError as e:
            raise PopcornError(f"{label} line {line_num}: {e}", error_code="validation") from None
        rows.append(row)
    return rows


def _csv_rows(text: str, label: str) -> list[tuple[int, dict[str, str]]]:
    """Header-keyed rows, each with the file line it ends on, cells still text.

    Coercion is a separate step because it needs the table's schema, and a
    file that cannot even be parsed should fail before any request is made.
    """
    # Strict, because the lenient reader takes an unterminated quote as a cell
    # running to the end of the file: every row after it would be swallowed
    # into one value and the import would report success.
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        return _csv_records(reader, label)
    except csv.Error as e:
        raise PopcornError(
            f"{label} line {reader.line_num}: malformed CSV ({e})", error_code="validation"
        ) from e


def _csv_records(reader: Any, label: str) -> list[tuple[int, dict[str, str]]]:
    header = next(reader, None)
    if header is None:
        return []
    names = [h.strip() for h in header]
    if any(not n for n in names):
        raise PopcornError(
            f"{label}: the header row has an empty column name", error_code="validation"
        )
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise PopcornError(
            f"{label}: the header row repeats {', '.join(duplicates)}", error_code="validation"
        )
    rows: list[tuple[int, dict[str, str]]] = []
    for cells in reader:
        if not any(c.strip() for c in cells):
            continue
        if len(cells) > len(names):
            raise PopcornError(
                f"{label} line {reader.line_num}: {len(cells)} cells but the header names "
                f"{len(names)} columns",
                error_code="validation",
            )
        rows.append((reader.line_num, dict(zip(names, cells, strict=False))))
    return rows


def _coerce_cell(cell: str, col_type: str | None) -> Any:
    """One CSV cell as the JSON value its column's type takes.

    Only number, boolean and json columns change anything; string, datetime
    and a column the schema doesn't declare keep the text exactly as written,
    so an id like "01234" in a string column keeps its leading zero. Raises
    ValueError naming what the cell should have been.
    """
    if col_type == "number":
        value = cell.strip()
        if _INT_RE.fullmatch(value):
            return int(value)
        if _FLOAT_RE.fullmatch(value):
            f = float(value)
            if math.isfinite(f):
                return f
        raise ValueError(f"{cell!r} is not a number")
    if col_type == "boolean":
        try:
            return _BOOLEANS[cell.strip().lower()]
        except KeyError:
            raise ValueError(f"{cell!r} is not a boolean (true/false, yes/no, 1/0)") from None
    if col_type == "json":
        try:
            value = json.loads(cell)
        except json.JSONDecodeError as e:
            raise ValueError(f"{cell!r} is not valid JSON ({e.msg})") from None
        _require_finite(value)
        return value
    return cell


def _coerce_csv_rows(
    rows: list[tuple[int, dict[str, str]]], column_types: dict[str, str], label: str
) -> list[dict[str, Any]]:
    """Typed rows. An empty cell is left out of its row, so whether the column
    may be absent is the server's call (`required`), not a guess here."""
    out: list[dict[str, Any]] = []
    for line_num, cells in rows:
        row: dict[str, Any] = {}
        for name, cell in cells.items():
            if cell == "":
                continue
            col_type = column_types.get(name)
            try:
                row[name] = _coerce_cell(cell, col_type)
            except ValueError as e:
                raise PopcornError(
                    f"{label} line {line_num}, column {name!r}: {e} (column type: {col_type})",
                    error_code="validation",
                ) from None
        out.append(row)
    return out


def _column_types(client: Any, conversation: str, name: str) -> dict[str, str]:
    resp = operations.get_table(client, conversation, name)
    table = resp.get("table", resp)
    cols = ((table.get("schema_version") or {}).get("schema_def") or {}).get("columns", [])
    return {c["name"]: c.get("type", "") for c in cols if isinstance(c, dict) and "name" in c}


def _batches(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]


# The top-level flags a suggested command repeats, as (dest, flag). `--env`
# and `--workspace` are in-memory overrides, so without them the command runs
# against the default profile's workspace, where the same channel name can be
# a different channel; the rest keep it behaving like the run that failed.
_GLOBAL_VALUE_FLAGS = (("env", "--env"), ("workspace", "--workspace"), ("timeout", "--timeout"))
_GLOBAL_BOOLEAN_FLAGS = (
    ("json", "--json"),
    ("quiet", "--quiet"),
    ("no_color", "--no-color"),
    ("debug", "--debug"),
    ("yes", "--yes"),
)


def _popcorn(args: argparse.Namespace) -> list[str]:
    """`popcorn` plus every global flag this run was given."""
    argv = ["popcorn"]
    for dest, flag in _GLOBAL_VALUE_FLAGS:
        value = getattr(args, dest, None)
        if value is not None:
            argv += [flag, str(value)]
    argv += [flag for dest, flag in _GLOBAL_BOOLEAN_FLAGS if getattr(args, dest, False)]
    return argv


def _command(args: argparse.Namespace, *rest: str) -> str:
    return " ".join(shlex.quote(a) for a in [*_popcorn(args), *rest])


def _resume_command(args: argparse.Namespace, skip: int) -> str:
    argv = [*_popcorn(args), "table", "import", args.name, "--channel", args.channel]
    argv += ["--file", args.file]
    if getattr(args, "format", None):
        argv += ["--format", args.format]
    for column in getattr(args, "merge_on", None) or []:
        argv += ["--merge-on", column]
    if getattr(args, "batch_size", None):
        argv += ["--batch-size", str(args.batch_size)]
    argv += ["--skip", str(skip)]
    return " ".join(shlex.quote(a) for a in argv)


def _batch_failed(
    err: APIError,
    args: argparse.Namespace,
    *,
    batch: int,
    batches: int,
    first: int,
    last: int,
    written_through: int,
    skip: int,
    created: int,
    updated: int,
) -> APIError:
    """The failing batch's error, restated with what the import already wrote.

    Each batch is its own transaction, so a refused batch wrote nothing and
    every earlier one is in the table. A response that never arrived (a
    timeout), a 5xx, or a success whose answer can't be read leaves that
    batch's fate unknown, and says so. Status,
    request id and body are kept, so error_code, exit code and retryable stay
    the server's.
    """
    unknown = _outcome_unknown(err)
    lines = [f"batch {batch}/{batches} (rows {first}-{last}) failed: {err}"]
    if written_through > skip:
        lines.append(
            f"Rows {skip + 1}-{written_through} were written ({created} created, "
            f"{updated} updated)."
        )
    if unknown:
        lines.append(
            f"Rows {first}-{last} may or may not have been written; nothing after "
            f"row {last} was sent."
        )
    elif written_through > skip:
        lines.append(f"Nothing after row {written_through} was written.")
    else:
        lines.append("Nothing was written.")
    wrapped = APIError(
        "\n".join(lines),
        status_code=err.status_code,
        body=err.body,
        retry_after=err.retry_after,
        request_id=err.request_id,
    )
    if unknown:
        rows_cmd = _command(args, "table", "rows", args.name, "--channel", args.channel)
        wrapped.hint = (
            f"check with `{rows_cmd}`, "
            f"then resume with --skip {written_through} "
            f"if rows {first}-{last} are absent or --skip {last} if they are there"
        )
    elif written_through > 0:
        wrapped.hint = f"fix the file, then: {_resume_command(args, written_through)}"
    return wrapped


def _table_import(args: argparse.Namespace) -> None:
    from ..cli import _get_client, _output, _status

    label = "stdin" if args.file == "-" else args.file
    fmt = _input_format(args.file, getattr(args, "format", None))
    text = _read_input(args.file)
    skip = getattr(args, "skip", None) or 0
    size = getattr(args, "batch_size", None) or DEFAULT_BATCH_SIZE
    merge_on = getattr(args, "merge_on", None)

    # The whole file is parsed before the first write, so a bad row at the end
    # costs nothing rather than leaving the rows before it half-imported.
    csv_rows: list[tuple[int, dict[str, str]]] = []
    rows: list[dict[str, Any]] = []
    if fmt == "csv":
        csv_rows = _csv_rows(text, label)[skip:]
    elif fmt == "json":
        rows = _json_rows(text, label)[skip:]
    else:
        rows = _jsonl_rows(text, label)[skip:]

    client = _get_client(args)
    conversation = resolve_conversation(client, args.channel)
    if fmt == "csv" and csv_rows:
        rows = _coerce_csv_rows(csv_rows, _column_types(client, conversation, args.name), label)

    if not rows:
        data = {"table": args.name, "rows": 0, "batches": 0, "created": 0, "updated": 0, "ids": []}
        _output(
            args,
            data,
            f"Nothing to import: {label} has no rows" + (f" after row {skip}" if skip else ""),
        )
        return

    batches = _batches(rows, size)
    ids: list[Any] = []
    created = updated = 0
    written_through = skip
    for i, batch in enumerate(batches, 1):
        first, last = written_through + 1, written_through + len(batch)
        _status(f"Batch {i}/{len(batches)}: rows {first}-{last}")
        try:
            resp = operations.insert_records(
                client, conversation, args.name, batch, merge_on=merge_on
            )
            batch_ids, batch_created, batch_updated = _insert_result(resp)
        except (APIError, ValueError) as e:
            err = e if isinstance(e, APIError) else _uncertain_answer(e, "")
            raise _batch_failed(
                err,
                args,
                batch=i,
                batches=len(batches),
                first=first,
                last=last,
                written_through=written_through,
                skip=skip,
                created=created,
                updated=updated,
            ) from e
        ids.extend(batch_ids)
        created += batch_created
        updated += batch_updated
        written_through = last

    data = {
        "table": args.name,
        "rows": len(rows),
        "batches": len(batches),
        "created": created,
        "updated": updated,
        "ids": ids,
    }
    if skip:
        data["skipped"] = skip
    noun = "batch" if len(batches) == 1 else "batches"
    _output(
        args,
        data,
        f"Imported {len(rows)} rows into {args.name} ({len(batches)} {noun}): "
        f"{created} created, {updated} updated.",
    )


register(
    Command(
        name="table",
        category="tables",
        description=(
            "Agent-store commands (list, schema, rows, import, row create/get/patch/delete, "
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
                "import",
                "Create rows in bulk from a .jsonl, .json (array) or .csv file",
                _table_import,
                [
                    _NAME,
                    _CHANNEL,
                    Argument(
                        "file",
                        "Rows to create: .jsonl/.ndjson (an object per line), .json "
                        "(an array of objects) or .csv (header row = column names, "
                        "cells typed from the table's schema); '-' for stdin",
                        type=str,
                        required=True,
                    ),
                    Argument(
                        "format",
                        "Input format when the file's extension doesn't say (required for '-')",
                        choices=["jsonl", "json", "csv"],
                    ),
                    _MERGE_ON,
                    Argument(
                        "batch-size",
                        f"Rows per request (default {DEFAULT_BATCH_SIZE}); each batch "
                        "is written or refused whole",
                        type=_positive_int,
                    ),
                    Argument(
                        "skip",
                        "Skip the first N rows of the file — resume after a failed batch",
                        type=_non_negative_int,
                    ),
                ],
            ),
            Subcommand(
                "row",
                "Operate on one row (create, get, patch, delete)",
                None,
                [],
                [
                    Subcommand(
                        "create",
                        "Create one row and print it as `row get` would",
                        _row_create,
                        [
                            _NAME,
                            _CHANNEL,
                            Argument(
                                "data",
                                "JSON object of the row's columns "
                                "(use '@-' for stdin, '@path' for a file)",
                                type=str,
                                required=True,
                            ),
                            _MERGE_ON,
                        ],
                    ),
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
                "Recent agent-store audit entries",
                _table_audit,
                [
                    _CHANNEL,
                    # No `choices` on entity-type: the entity taxonomy is the
                    # server's, and a copy here would be a second place to keep
                    # current. An unknown value matches nothing, which reads the
                    # same as an entity nothing ever touched.
                    Argument(
                        "entity-type",
                        "Filter by entity type (record, scalar, table, …)",
                        type=str,
                    ),
                    Argument(
                        "entity-id",
                        "Filter by entity id — one row's or scalar's history",
                        type=str,
                    ),
                    Argument(
                        "since",
                        "ISO 8601 datetime; entries at or after it (2026-09-01T00:00:00Z)",
                        type=str,
                    ),
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
