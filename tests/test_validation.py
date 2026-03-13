"""Tests for response validation helpers."""

from __future__ import annotations

import pytest

from popcorn_core.errors import PopcornError
from popcorn_core.validation import extract


class TestExtract:
    def test_nested_key_extraction(self):
        resp = {"conversation": {"id": "c-123", "name": "my-site"}}
        assert extract(resp, "conversation", "id", label="deploy_create") == "c-123"

    def test_single_key_extraction(self):
        resp = {"id": "c-123"}
        assert extract(resp, "id", label="test") == "c-123"

    def test_missing_top_level_key_raises(self):
        resp = {"other": "value"}
        with pytest.raises(PopcornError, match="deploy_create"):
            extract(resp, "conversation", label="deploy_create")

    def test_missing_top_level_key_includes_missing_key_name(self):
        resp = {"other": "value"}
        with pytest.raises(PopcornError, match="missing 'conversation'"):
            extract(resp, "conversation", label="test")

    def test_missing_nested_key_raises(self):
        resp = {"conversation": {"name": "my-site"}}
        with pytest.raises(PopcornError, match="missing 'id'"):
            extract(resp, "conversation", "id", label="deploy_create")

    def test_non_dict_intermediate_raises(self):
        resp = {"conversation": "not-a-dict"}
        with pytest.raises(PopcornError, match="missing 'id'"):
            extract(resp, "conversation", "id", label="deploy_create")

    def test_truncates_long_response_in_error(self):
        resp = {"data": "x" * 500}
        with pytest.raises(PopcornError) as exc_info:
            extract(resp, "missing_key", label="test")
        # The full JSON would be >500 chars; error message truncates to 200
        error_msg = str(exc_info.value)
        # Extract the part after "Response: " — it should be at most 200 chars
        response_part = error_msg.split("Response: ", 1)[1]
        assert len(response_part) <= 200

    def test_returns_nested_dict(self):
        resp = {"a": {"b": {"c": 42}}}
        assert extract(resp, "a", "b", label="test") == {"c": 42}

    def test_no_keys_returns_response(self):
        resp = {"a": 1}
        assert extract(resp, label="test") == {"a": 1}
