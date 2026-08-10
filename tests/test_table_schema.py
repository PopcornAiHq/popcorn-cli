"""`table schema` column rendering — flags and per-column merge policy.

A column's `merge` policy is a design-time attribute that changes what a write
DOES (concat accumulates, replace overwrites) but is invisible in the row data
itself. Omitting it from `table schema` cost a real debugging session.
"""

from __future__ import annotations

from popcorn_cli.commands.table import _column_line


class TestFlags:
    def test_plain_column_has_no_bracket(self):
        assert "[" not in _column_line({"name": "note", "type": "string"})

    def test_name_and_type_always_shown(self):
        line = _column_line({"name": "note", "type": "string"})
        assert "note" in line and "string" in line

    def test_governance_flags_are_listed(self):
        line = _column_line({"name": "phone", "type": "string", "pii": True, "required": True})
        assert "pii" in line and "required" in line

    def test_false_flags_are_omitted(self):
        line = _column_line({"name": "note", "type": "string", "pii": False})
        assert "pii" not in line


class TestMergePolicy:
    def test_concat_is_shown(self):
        line = _column_line({"name": "notes", "type": "string", "merge": "concat"})
        assert "concat" in line

    def test_replace_is_not_shown(self):
        """`replace` is the default on every column — printing it on all of
        them would bury the one column that accumulates."""
        line = _column_line({"name": "notes", "type": "string", "merge": "replace"})
        assert "concat" not in line
        assert "replace" not in line

    def test_absent_merge_key_is_treated_as_replace(self):
        line = _column_line({"name": "notes", "type": "string"})
        assert "merge" not in line

    def test_custom_separator_is_shown(self):
        """Separator changes the stored value's shape, so an author needs it."""
        line = _column_line(
            {"name": "notes", "type": "string", "merge": "concat", "merge_separator": "; "}
        )
        assert "concat" in line
        assert "; " in line or repr("; ") in line

    def test_default_separator_is_not_claimed_as_custom(self):
        """merge_separator=None means the "\\n" default is applied server-side;
        rendering an empty separator would be a lie."""
        line = _column_line(
            {"name": "notes", "type": "string", "merge": "concat", "merge_separator": None}
        )
        assert "concat" in line
        assert "sep=" not in line

    def test_merge_and_governance_flags_coexist(self):
        line = _column_line({"name": "notes", "type": "string", "merge": "concat", "pii": True})
        assert "concat" in line and "pii" in line
