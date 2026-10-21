"""`table row create` and `table import` — the agent-store writes.

Both go through one insert endpoint that answers with ids and counts, never
rows. The handler tests run through `main()` so parsing, dispatch and the
error envelope are the real ones; only `operations` is faked.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from popcorn_cli.commands.table import (
    _batches,
    _coerce_cell,
    _coerce_csv_rows,
    _csv_rows,
    _input_format,
    _json_rows,
    _jsonl_rows,
)
from popcorn_core import operations
from popcorn_core.errors import APIError, PopcornError

CHANNEL = "00000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _no_channel_lookup(monkeypatch):
    """`import` resolves the channel once itself; the tests' client is a stub."""
    monkeypatch.setattr("popcorn_cli.commands.table.resolve_conversation", lambda _c, ref: ref)


def _run(monkeypatch, argv: list[str]) -> None:
    from popcorn_cli import cli

    monkeypatch.setattr(cli, "_check_and_update", lambda: None)
    monkeypatch.setattr(cli, "_get_client", lambda args: object())
    monkeypatch.setattr(sys, "argv", ["popcorn", *argv])
    cli.main()


def _error(stderr: str) -> dict:
    """The JSON error envelope, after any progress lines printed before it."""
    return json.loads(stderr[stderr.index("{") :])


def _run_failing(monkeypatch, argv: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, argv)
    return int(exc.value.code)


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


class TestInputFormat:
    @pytest.mark.parametrize(
        ("path", "fmt"),
        [
            ("rows.jsonl", "jsonl"),
            ("rows.ndjson", "jsonl"),
            ("rows.json", "json"),
            ("ROWS.CSV", "csv"),
        ],
    )
    def test_extension_decides(self, path, fmt):
        assert _input_format(path, None) == fmt

    def test_explicit_format_wins(self):
        assert _input_format("rows.txt", "csv") == "csv"

    def test_unknown_extension_asks_for_format(self):
        with pytest.raises(PopcornError, match="--format"):
            _input_format("rows.txt", None)

    def test_stdin_needs_a_format(self):
        with pytest.raises(PopcornError, match="stdin"):
            _input_format("-", None)


class TestJsonRows:
    def test_array_of_objects(self):
        assert _json_rows('[{"a": 1}, {"a": 2}]', "f.json") == [{"a": 1}, {"a": 2}]

    def test_a_bare_object_is_refused(self):
        """One object is `row create`'s input; an import file is a list."""
        with pytest.raises(PopcornError, match="array"):
            _json_rows('{"a": 1}', "f.json")

    def test_names_the_item_that_is_not_an_object(self):
        with pytest.raises(PopcornError, match="item 2"):
            _json_rows('[{"a": 1}, 7]', "f.json")

    def test_invalid_json(self):
        with pytest.raises(PopcornError, match="not valid JSON"):
            _json_rows("[{", "f.json")

    def test_non_finite_number_is_refused(self):
        with pytest.raises(PopcornError, match="item 2: NaN, Infinity"):
            _json_rows('[{"a": 1}, {"a": NaN}]', "f.json")


class TestJsonlRows:
    def test_one_object_per_line_blank_lines_skipped(self):
        assert _jsonl_rows('{"a": 1}\n\n{"a": 2}\n', "f.jsonl") == [{"a": 1}, {"a": 2}]

    def test_names_the_bad_line(self):
        with pytest.raises(PopcornError, match="line 3: not a JSON object"):
            _jsonl_rows('{"a": 1}\n\n[1]\n', "f.jsonl")

    def test_names_the_unparseable_line(self):
        with pytest.raises(PopcornError, match="line 2: not valid JSON"):
            _jsonl_rows('{"a": 1}\n{"a":\n', "f.jsonl")

    @pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e400"])
    def test_non_finite_numbers_are_refused(self, value):
        """json.loads takes them, but the request could not be serialised."""
        with pytest.raises(PopcornError, match="line 2: NaN, Infinity") as exc:
            _jsonl_rows(f'{{"a": 1}}\n{{"a": {{"deep": [{value}]}}}}\n', "f.jsonl")
        assert exc.value.error_code == "validation"

    def test_values_are_sent_as_written(self):
        """JSON input carries its own types; nothing is coerced."""
        assert _jsonl_rows('{"Score": "42"}', "f.jsonl") == [{"Score": "42"}]


class TestCsvRows:
    def test_header_names_the_columns(self):
        rows = _csv_rows("Name,Score\nAda,42\n", "f.csv")
        assert rows == [(2, {"Name": "Ada", "Score": "42"})]

    def test_quoted_cells_and_embedded_newlines(self):
        rows = _csv_rows('Name,Note\n"Ada, L","two\nlines"\nBob,x\n', "f.csv")
        assert [r for _, r in rows] == [
            {"Name": "Ada, L", "Note": "two\nlines"},
            {"Name": "Bob", "Note": "x"},
        ]
        # The line a row ENDS on, so an error points at a real line.
        assert [line for line, _ in rows] == [3, 4]

    def test_short_rows_leave_the_missing_columns_out(self):
        assert _csv_rows("a,b\n1\n", "f.csv") == [(2, {"a": "1"})]

    def test_too_many_cells_is_refused(self):
        with pytest.raises(PopcornError, match="line 2: 3 cells"):
            _csv_rows("a,b\n1,2,3\n", "f.csv")

    def test_duplicate_header_is_refused(self):
        with pytest.raises(PopcornError, match="repeats a"):
            _csv_rows("a,b,a\n1,2,3\n", "f.csv")

    def test_empty_header_name_is_refused(self):
        with pytest.raises(PopcornError, match="empty column name"):
            _csv_rows("a,,c\n1,2,3\n", "f.csv")

    def test_blank_lines_are_skipped(self):
        assert _csv_rows("a\n1\n\n2\n", "f.csv") == [(2, {"a": "1"}), (4, {"a": "2"})]

    def test_unterminated_quote_is_refused(self):
        """The lenient reader would swallow every later row into one cell."""
        with pytest.raises(PopcornError, match="malformed CSV") as exc:
            _csv_rows('Name,Note\nAda,"unfinished\nBob,second\n', "f.csv")
        assert exc.value.error_code == "validation"

    def test_stray_quote_is_refused(self):
        with pytest.raises(PopcornError, match=r"f\.csv line 2: malformed CSV"):
            _csv_rows('a,b\n1,"x"y\n', "f.csv")

    def test_empty_file_has_no_rows(self):
        assert _csv_rows("", "f.csv") == []


class TestCoerceCell:
    @pytest.mark.parametrize(
        ("cell", "value"),
        [("42", 42), ("-7", -7), (" 3 ", 3), ("1.5", 1.5), ("2e3", 2000.0), (".5", 0.5)],
    )
    def test_number(self, cell, value):
        result = _coerce_cell(cell, "number")
        assert result == value
        assert type(result) is type(value)

    @pytest.mark.parametrize("cell", ["high", "nan", "inf", "1_000", "1,000", "0x10"])
    def test_not_a_number(self, cell):
        with pytest.raises(ValueError, match="not a number"):
            _coerce_cell(cell, "number")

    @pytest.mark.parametrize(
        ("cell", "value"),
        [("true", True), ("FALSE", False), ("Yes", True), ("no", False), ("1", True), ("0", False)],
    )
    def test_boolean(self, cell, value):
        assert _coerce_cell(cell, "boolean") is value

    def test_not_a_boolean(self):
        with pytest.raises(ValueError, match="not a boolean"):
            _coerce_cell("maybe", "boolean")

    def test_json(self):
        assert _coerce_cell('{"k": [1, 2]}', "json") == {"k": [1, 2]}

    @pytest.mark.parametrize("cell", ["NaN", '{"k": Infinity}', "1e400"])
    def test_json_cell_with_a_non_finite_number(self, cell):
        with pytest.raises(ValueError, match="NaN, Infinity"):
            _coerce_cell(cell, "json")

    def test_bad_json(self):
        with pytest.raises(ValueError, match="not valid JSON"):
            _coerce_cell("{", "json")

    @pytest.mark.parametrize("col_type", ["string", "datetime", None])
    def test_text_is_kept_exactly(self, col_type):
        """A leading zero, a timestamp, an undeclared column: the text as written."""
        assert _coerce_cell("01234", col_type) == "01234"


class TestCoerceCsvRows:
    TYPES = {"Name": "string", "Score": "number", "Active": "boolean"}  # noqa: RUF012

    def test_types_each_cell_from_the_schema(self):
        rows = [(2, {"Name": "Ada", "Score": "42", "Active": "yes", "Extra": "x"})]
        assert _coerce_csv_rows(rows, self.TYPES, "f.csv") == [
            {"Name": "Ada", "Score": 42, "Active": True, "Extra": "x"}
        ]

    def test_empty_cell_is_left_out(self):
        """Whether a column may be absent is the server's call (`required`)."""
        rows = [(2, {"Name": "Ada", "Score": ""})]
        assert _coerce_csv_rows(rows, self.TYPES, "f.csv") == [{"Name": "Ada"}]

    def test_error_names_line_column_and_type(self):
        rows = [(17, {"Name": "Ada", "Score": "high"})]
        with pytest.raises(PopcornError) as exc:
            _coerce_csv_rows(rows, self.TYPES, "leads.csv")
        msg = str(exc.value)
        assert "leads.csv line 17" in msg
        assert "'Score'" in msg
        assert "column type: number" in msg
        assert exc.value.error_code == "validation"


class TestBatches:
    def test_sizes(self):
        rows = [{"n": i} for i in range(1240)]
        assert [len(b) for b in _batches(rows, 500)] == [500, 500, 240]

    def test_order_is_kept(self):
        rows = [{"n": i} for i in range(5)]
        assert [r["n"] for b in _batches(rows, 2) for r in b] == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# `table row create`
# ---------------------------------------------------------------------------


class TestRowCreate:
    RECORD = {"id": 1187, "data": {"Name": "Ada"}, "rev": 1}  # noqa: RUF012

    def _fake(self, monkeypatch, insert_resp, get=None):
        calls: dict = {}

        def fake_insert(client, conversation, name, records, merge_on=None):
            calls["insert"] = {"name": name, "records": records, "merge_on": merge_on}
            return insert_resp

        def fake_get(client, conversation, name, record_id):
            calls["get"] = record_id
            if isinstance(get, Exception):
                raise get
            return {"ok": True, "record": get or self.RECORD}

        monkeypatch.setattr(operations, "insert_records", fake_insert)
        monkeypatch.setattr(operations, "get_record", fake_get)
        return calls

    def test_creates_one_row_and_prints_it_as_row_get_does(self, monkeypatch, capsys):
        calls = self._fake(monkeypatch, {"ids": [1187], "created": 1, "updated": 0})
        _run(
            monkeypatch,
            ["table", "row", "create", "leads", "--channel", CHANNEL, "--data", '{"Name":"Ada"}'],
        )
        assert calls["insert"] == {"name": "leads", "records": [{"Name": "Ada"}], "merge_on": None}
        assert calls["get"] == 1187
        captured = capsys.readouterr()
        assert json.loads(captured.out) == self.RECORD
        assert "Created leads record 1187" in captured.err

    def test_json_envelope_carries_record_and_counts(self, monkeypatch, capsys):
        self._fake(monkeypatch, {"ids": [1187], "created": 1, "updated": 0})
        _run(
            monkeypatch,
            ["table", "row", "create", "leads", "--channel", CHANNEL, "--data", "{}", "--json"],
        )
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["ok"] is True
        out = envelope["data"]
        assert out["record"] == self.RECORD
        assert (out["created"], out["updated"]) == (1, 0)

    def test_data_from_a_file(self, monkeypatch, tmp_path):
        calls = self._fake(monkeypatch, {"ids": [1187], "created": 1, "updated": 0})
        path = tmp_path / "lead.json"
        path.write_text('{"Name": "Ada"}')
        _run(
            monkeypatch,
            ["table", "row", "create", "leads", "--channel", CHANNEL, "--data", f"@{path}"],
        )
        assert calls["insert"]["records"] == [{"Name": "Ada"}]

    def test_merge_on_is_repeatable(self, monkeypatch):
        calls = self._fake(monkeypatch, {"ids": [1042], "created": 0, "updated": 1})
        _run(
            monkeypatch,
            [
                "table",
                "row",
                "create",
                "leads",
                "--channel",
                CHANNEL,
                "--data",
                "{}",
                "--merge-on",
                "Email",
                "--merge-on",
                "Org",
            ],
        )
        assert calls["insert"]["merge_on"] == ["Email", "Org"]

    def test_a_merge_says_so(self, monkeypatch, capsys):
        """A table with its own merge key upserts even without --merge-on."""
        self._fake(monkeypatch, {"ids": [1042], "created": 0, "updated": 1})
        _run(monkeypatch, ["table", "row", "create", "leads", "--channel", CHANNEL, "--data", "{}"])
        assert "Merged into existing leads record 1042" in capsys.readouterr().err

    def test_read_back_failure_still_succeeds(self, monkeypatch, capsys):
        """The row is written; failing now would invite a retry that adds it twice."""
        self._fake(
            monkeypatch,
            {"ids": [1187], "created": 1, "updated": 0},
            get=APIError("boom", status_code=503),
        )
        _run(monkeypatch, ["table", "row", "create", "leads", "--channel", CHANNEL, "--data", "{}"])
        out = capsys.readouterr().out
        assert "Created leads record 1187" in out
        assert "read-back failed: boom" in out

    def test_server_validation_error_exits_non_zero_with_its_message(self, monkeypatch, capsys):
        def refuse(*a, **kw):
            raise APIError("missing required field: Name", status_code=422)

        monkeypatch.setattr(operations, "insert_records", refuse)
        code = _run_failing(
            monkeypatch,
            ["table", "row", "create", "leads", "--channel", CHANNEL, "--data", "{}", "--json"],
        )
        assert code == 3
        err = _error(capsys.readouterr().err)
        assert err["error"] == "missing required field: Name"
        assert err["error_code"] == "validation"

    def test_non_finite_data_is_refused_before_any_request(self, monkeypatch, capsys):
        monkeypatch.setattr(
            operations, "insert_records", lambda *a, **kw: pytest.fail("no request expected")
        )
        code = _run_failing(
            monkeypatch,
            ["table", "row", "create", "leads", "--channel", CHANNEL, "--data", '{"a": NaN}'],
        )
        assert code == 1
        assert "NaN, Infinity" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "answer", [{}, {"ids": [1187]}, {"ids": [], "created": 0, "updated": 0}]
    )
    def test_a_malformed_answer_is_an_uncertain_write(self, monkeypatch, capsys, answer):
        """A success without ids/counts may still have written the row; saying
        nothing, or "Created", would invite a retry that adds it twice."""
        self._fake(monkeypatch, answer)
        code = _run_failing(
            monkeypatch,
            ["table", "row", "create", "leads", "--channel", CHANNEL, "--data", "{}", "--json"],
        )
        assert code != 0
        err = _error(capsys.readouterr().err)
        assert "answered without" in err["error"]
        assert "may or may not have been written" in err["hint"]
        assert "table rows leads" in err["hint"]

    def test_an_unreadable_success_is_an_uncertain_write(self, monkeypatch, capsys):
        def garbled(*a, **kw):
            raise APIError("Invalid JSON in response from /x", status_code=200)

        monkeypatch.setattr(operations, "insert_records", garbled)
        _run_failing(
            monkeypatch,
            ["table", "row", "create", "leads", "--channel", CHANNEL, "--data", "{}", "--json"],
        )
        assert "may or may not have been written" in _error(capsys.readouterr().err)["hint"]

    def test_a_refusal_is_not_called_uncertain(self, monkeypatch, capsys):
        def refuse(*a, **kw):
            raise APIError("missing required field: Name", status_code=422)

        monkeypatch.setattr(operations, "insert_records", refuse)
        _run_failing(
            monkeypatch,
            ["table", "row", "create", "leads", "--channel", CHANNEL, "--data", "{}", "--json"],
        )
        assert "hint" not in _error(capsys.readouterr().err)

    def test_non_object_data_is_refused_before_any_request(self, monkeypatch):
        monkeypatch.setattr(
            operations, "insert_records", lambda *a, **kw: pytest.fail("no request expected")
        )
        code = _run_failing(
            monkeypatch,
            ["table", "row", "create", "leads", "--channel", CHANNEL, "--data", "[1]"],
        )
        assert code == 1


# ---------------------------------------------------------------------------
# `table import`
# ---------------------------------------------------------------------------


class _Store:
    """A fake insert endpoint: records each batch, optionally refuses one."""

    def __init__(
        self,
        fail_on: int | None = None,
        error: APIError | None = None,
        answer_on: int | None = None,
        answer: dict | None = None,
    ):
        self.answer_on = answer_on
        self.answer = answer
        self.batches: list[list[dict]] = []
        self.merge_on: list | None = None
        self.fail_on = fail_on
        self.error = error or APIError("missing required field: Name", status_code=422)
        self.next_id = 1

    def insert(self, client, conversation, name, records, merge_on=None):
        if self.fail_on is not None and len(self.batches) + 1 == self.fail_on:
            raise self.error
        self.batches.append(records)
        self.merge_on = merge_on
        if self.answer_on == len(self.batches):
            return self.answer
        ids = list(range(self.next_id, self.next_id + len(records)))
        self.next_id += len(records)
        return {"ids": ids, "created": len(records), "updated": 0, "schema_version_id": 1}


SCHEMA = {
    "table": {
        "schema_version": {
            "schema_def": {
                "columns": [
                    {"name": "Name", "type": "string"},
                    {"name": "Score", "type": "number"},
                    {"name": "Active", "type": "boolean"},
                ]
            }
        }
    }
}


class TestImport:
    def _install(self, monkeypatch, store: _Store) -> list[str]:
        schema_reads: list[str] = []

        def fake_get_table(client, conversation, name):
            schema_reads.append(name)
            return SCHEMA

        monkeypatch.setattr(operations, "insert_records", store.insert)
        monkeypatch.setattr(operations, "get_table", fake_get_table)
        return schema_reads

    def _jsonl(self, tmp_path, n: int) -> str:
        path = tmp_path / "rows.jsonl"
        path.write_text("".join(json.dumps({"Name": f"n{i}"}) + "\n" for i in range(n)))
        return str(path)

    def test_batches_and_reports_counts(self, monkeypatch, tmp_path, capsys):
        store = _Store()
        self._install(monkeypatch, store)
        path = self._jsonl(tmp_path, 1240)
        _run(monkeypatch, ["table", "import", "leads", "--channel", CHANNEL, "--file", path])
        assert [len(b) for b in store.batches] == [500, 500, 240]
        captured = capsys.readouterr()
        assert "Imported 1240 rows into leads (3 batches): 1240 created, 0 updated." in captured.out
        assert "Batch 3/3: rows 1001-1240" in captured.err

    def test_json_envelope(self, monkeypatch, tmp_path, capsys):
        store = _Store()
        self._install(monkeypatch, store)
        path = self._jsonl(tmp_path, 3)
        _run(
            monkeypatch,
            ["table", "import", "leads", "--channel", CHANNEL, "--file", path, "--json"],
        )
        envelope = json.loads(capsys.readouterr().out)
        assert envelope["ok"] is True
        out = envelope["data"]
        assert out["table"] == "leads"
        assert (out["rows"], out["batches"], out["created"], out["updated"]) == (3, 1, 3, 0)
        assert out["ids"] == [1, 2, 3]

    def test_batch_size_and_merge_on(self, monkeypatch, tmp_path):
        store = _Store()
        self._install(monkeypatch, store)
        path = self._jsonl(tmp_path, 5)
        _run(
            monkeypatch,
            [
                "table",
                "import",
                "leads",
                "--channel",
                CHANNEL,
                "--file",
                path,
                "--batch-size",
                "2",
                "--merge-on",
                "Name",
            ],
        )
        assert [len(b) for b in store.batches] == [2, 2, 1]
        assert store.merge_on == ["Name"]

    def test_json_array_file(self, monkeypatch, tmp_path):
        store = _Store()
        self._install(monkeypatch, store)
        path = tmp_path / "rows.json"
        path.write_text('[{"Name": "a"}, {"Name": "b"}]')
        _run(monkeypatch, ["table", "import", "leads", "--channel", CHANNEL, "--file", str(path)])
        assert store.batches == [[{"Name": "a"}, {"Name": "b"}]]

    def test_csv_is_typed_from_the_schema(self, monkeypatch, tmp_path):
        store = _Store()
        schema_reads = self._install(monkeypatch, store)
        path = tmp_path / "leads.csv"
        # A spreadsheet export's byte-order mark must not reach the first name.
        path.write_text("﻿Name,Score,Active\nAda,42,yes\nBob,,no\n", encoding="utf-8")
        _run(monkeypatch, ["table", "import", "leads", "--channel", CHANNEL, "--file", str(path)])
        assert schema_reads == ["leads"]
        assert store.batches == [
            [{"Name": "Ada", "Score": 42, "Active": True}, {"Name": "Bob", "Active": False}]
        ]

    def test_json_input_reads_no_schema(self, monkeypatch, tmp_path):
        store = _Store()
        schema_reads = self._install(monkeypatch, store)
        _run(
            monkeypatch,
            ["table", "import", "leads", "--channel", CHANNEL, "--file", self._jsonl(tmp_path, 2)],
        )
        assert schema_reads == []

    def test_stdin_with_format(self, monkeypatch, capsys):
        store = _Store()
        self._install(monkeypatch, store)
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"Name": "a"}\n{"Name": "b"}\n'))
        _run(
            monkeypatch,
            ["table", "import", "leads", "--channel", CHANNEL, "--file", "-", "--format", "jsonl"],
        )
        assert store.batches == [[{"Name": "a"}, {"Name": "b"}]]

    def test_skip(self, monkeypatch, tmp_path, capsys):
        store = _Store()
        self._install(monkeypatch, store)
        path = self._jsonl(tmp_path, 5)
        _run(
            monkeypatch,
            ["table", "import", "leads", "--channel", CHANNEL, "--file", path, "--skip", "3"],
        )
        assert store.batches == [[{"Name": "n3"}, {"Name": "n4"}]]
        # Row numbers stay the file's, so they line up with the earlier run's.
        assert "rows 4-5" in capsys.readouterr().err

    def test_an_unparseable_file_sends_nothing(self, monkeypatch, tmp_path):
        store = _Store()
        schema_reads = self._install(monkeypatch, store)
        path = tmp_path / "rows.jsonl"
        path.write_text('{"Name": "a"}\nnot json\n')
        code = _run_failing(
            monkeypatch, ["table", "import", "leads", "--channel", CHANNEL, "--file", str(path)]
        )
        assert code == 1
        assert store.batches == []
        assert schema_reads == []

    def test_a_bad_csv_cell_sends_nothing(self, monkeypatch, tmp_path, capsys):
        store = _Store()
        self._install(monkeypatch, store)
        path = tmp_path / "leads.csv"
        path.write_text("Name,Score\nAda,1\nBob,high\n")
        code = _run_failing(
            monkeypatch, ["table", "import", "leads", "--channel", CHANNEL, "--file", str(path)]
        )
        assert code == 1
        assert store.batches == []
        assert "line 3, column 'Score'" in capsys.readouterr().err

    def test_an_empty_file_imports_nothing(self, monkeypatch, tmp_path, capsys):
        store = _Store()
        self._install(monkeypatch, store)
        path = tmp_path / "rows.jsonl"
        path.write_text("")
        _run(monkeypatch, ["table", "import", "leads", "--channel", CHANNEL, "--file", str(path)])
        assert store.batches == []
        assert "Nothing to import" in capsys.readouterr().out

    def test_a_failed_batch_says_what_was_written(self, monkeypatch, tmp_path, capsys):
        store = _Store(
            fail_on=3,
            error=APIError("missing required field: Name", status_code=422, request_id="req-1"),
        )
        self._install(monkeypatch, store)
        path = self._jsonl(tmp_path, 1240)
        code = _run_failing(
            monkeypatch,
            ["table", "import", "leads", "--channel", CHANNEL, "--file", path, "--json"],
        )
        assert code == 3
        assert len(store.batches) == 2
        err = _error(capsys.readouterr().err)
        msg = err["error"]
        assert "batch 3/3 (rows 1001-1240) failed: missing required field: Name" in msg
        assert "Rows 1-1000 were written (1000 created, 0 updated)." in msg
        assert "Nothing after row 1000 was written." in msg
        # The server's classification survives the restatement.
        assert err["error_code"] == "validation"
        assert err["request_id"] == "req-1"
        assert err["hint"].endswith(f"--file {path} --skip 1000")

    def test_a_refused_first_batch_wrote_nothing(self, monkeypatch, tmp_path, capsys):
        store = _Store(fail_on=1)
        self._install(monkeypatch, store)
        path = self._jsonl(tmp_path, 3)
        _run_failing(
            monkeypatch,
            ["table", "import", "leads", "--channel", CHANNEL, "--file", path, "--json"],
        )
        err = _error(capsys.readouterr().err)
        assert "Nothing was written." in err["error"]
        assert "hint" not in err

    def test_a_lost_response_is_reported_as_unknown(self, monkeypatch, tmp_path, capsys):
        store = _Store(fail_on=2, error=APIError("Request timed out: POST ..."))
        self._install(monkeypatch, store)
        path = self._jsonl(tmp_path, 4)
        _run_failing(
            monkeypatch,
            [
                "table",
                "import",
                "leads",
                "--channel",
                CHANNEL,
                "--file",
                path,
                "--batch-size",
                "2",
                "--json",
            ],
        )
        err = _error(capsys.readouterr().err)
        assert "Rows 3-4 may or may not have been written" in err["error"]
        assert "--skip 2" in err["hint"] and "--skip 4" in err["hint"]

    def test_resume_hint_keeps_the_original_flags(self, monkeypatch, tmp_path, capsys):
        store = _Store(fail_on=2)
        self._install(monkeypatch, store)
        path = self._jsonl(tmp_path, 4)
        _run_failing(
            monkeypatch,
            [
                "table",
                "import",
                "leads",
                "--channel",
                "#example sales",
                "--file",
                path,
                "--batch-size",
                "2",
                "--merge-on",
                "Name",
                "--json",
            ],
        )
        hint = _error(capsys.readouterr().err)["hint"]
        assert "--channel '#example sales'" in hint
        assert "--merge-on Name --batch-size 2 --skip 2" in hint

    def test_hints_keep_the_profile_and_workspace(self, monkeypatch, tmp_path, capsys):
        """`--env`/`--workspace` only override in memory; a hint without them
        would resume against the default workspace's same-named channel."""
        store = _Store(fail_on=2)
        self._install(monkeypatch, store)
        path = self._jsonl(tmp_path, 4)
        _run_failing(
            monkeypatch,
            [
                "--env",
                "dev",
                "--workspace",
                "Example Co",
                "table",
                "import",
                "leads",
                "--channel",
                CHANNEL,
                "--file",
                path,
                "--batch-size",
                "2",
                "--json",
            ],
        )
        hint = _error(capsys.readouterr().err)["hint"]
        assert "popcorn --env dev --workspace 'Example Co' --json table import leads" in hint

    def test_unknown_hint_keeps_the_profile_too(self, monkeypatch, tmp_path, capsys):
        store = _Store(fail_on=1, error=APIError("Request timed out: POST ..."))
        self._install(monkeypatch, store)
        _run_failing(
            monkeypatch,
            [
                "--env",
                "dev",
                "table",
                "import",
                "leads",
                "--channel",
                CHANNEL,
                "--file",
                self._jsonl(tmp_path, 1),
                "--json",
            ],
        )
        hint = _error(capsys.readouterr().err)["hint"]
        assert "`popcorn --env dev --json table rows leads" in hint

    def test_hints_carry_every_global_flag(self, monkeypatch, tmp_path, capsys):
        store = _Store(fail_on=2)
        self._install(monkeypatch, store)
        path = self._jsonl(tmp_path, 4)
        _run_failing(
            monkeypatch,
            [
                "--env",
                "dev",
                "--timeout",
                "5",
                "--quiet",
                "--no-color",
                "--debug",
                "table",
                "import",
                "leads",
                "--channel",
                CHANNEL,
                "--file",
                path,
                "--batch-size",
                "2",
                "--json",
            ],
        )
        hint = _error(capsys.readouterr().err)["hint"]
        assert (
            "popcorn --env dev --timeout 5.0 --json --quiet --no-color --debug table import" in hint
        )

    def test_a_non_finite_number_late_in_the_file_sends_nothing(self, monkeypatch, tmp_path):
        """Caught in the whole-file pass, not as a crash after batch 1 is written."""
        store = _Store()
        self._install(monkeypatch, store)
        path = tmp_path / "rows.jsonl"
        path.write_text('{"Name": "a"}\n{"Name": "b"}\n{"Score": Infinity}\n')
        code = _run_failing(
            monkeypatch,
            [
                "table",
                "import",
                "leads",
                "--channel",
                CHANNEL,
                "--file",
                str(path),
                "--batch-size",
                "1",
            ],
        )
        assert code == 1
        assert store.batches == []

    def test_a_non_finite_json_cell_in_a_csv_sends_nothing(self, monkeypatch, tmp_path, capsys):
        store = _Store()
        monkeypatch.setattr(operations, "insert_records", store.insert)
        monkeypatch.setattr(
            operations,
            "get_table",
            lambda *a: {
                "table": {
                    "schema_version": {"schema_def": {"columns": [{"name": "M", "type": "json"}]}}
                }
            },
        )
        path = tmp_path / "rows.csv"
        path.write_text('M\n{"v": 1}\n{"v": NaN}\n')
        code = _run_failing(
            monkeypatch, ["table", "import", "leads", "--channel", CHANNEL, "--file", str(path)]
        )
        assert code == 1
        assert store.batches == []
        assert "line 3, column 'M'" in capsys.readouterr().err

    @pytest.mark.parametrize("answer", [{}, {"ids": [1, 2]}, {"created": 2, "updated": 0}])
    def test_a_malformed_answer_is_an_uncertain_write(self, monkeypatch, tmp_path, capsys, answer):
        """A success without ids/counts may have committed the batch; resuming
        before it would write those rows twice."""
        store = _Store(answer_on=2, answer=answer)
        self._install(monkeypatch, store)
        path = self._jsonl(tmp_path, 6)
        _run_failing(
            monkeypatch,
            [
                "table",
                "import",
                "leads",
                "--channel",
                CHANNEL,
                "--file",
                path,
                "--batch-size",
                "2",
                "--json",
            ],
        )
        assert len(store.batches) == 2  # nothing after the uncertain batch was sent
        err = _error(capsys.readouterr().err)
        assert "batch 2/3 (rows 3-4) failed: the server accepted the write" in err["error"]
        assert "Rows 1-2 were written" in err["error"]
        assert "Rows 3-4 may or may not have been written" in err["error"]
        assert "--skip 2 if rows 3-4 are absent or --skip 4" in err["hint"]

    def test_an_unreadable_success_is_an_uncertain_write(self, monkeypatch, tmp_path, capsys):
        store = _Store(
            fail_on=2, error=APIError("Invalid JSON in response from /x", status_code=200)
        )
        self._install(monkeypatch, store)
        path = self._jsonl(tmp_path, 4)
        _run_failing(
            monkeypatch,
            [
                "table",
                "import",
                "leads",
                "--channel",
                CHANNEL,
                "--file",
                path,
                "--batch-size",
                "2",
                "--json",
            ],
        )
        err = _error(capsys.readouterr().err)
        assert "Rows 3-4 may or may not have been written" in err["error"]
        assert "fix the file" not in err["hint"]

    def test_unknown_extension_sends_nothing(self, monkeypatch, tmp_path):
        store = _Store()
        self._install(monkeypatch, store)
        path = tmp_path / "rows.txt"
        path.write_text("{}")
        code = _run_failing(
            monkeypatch, ["table", "import", "leads", "--channel", CHANNEL, "--file", str(path)]
        )
        assert code == 1
        assert store.batches == []
