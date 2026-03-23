# Pop Health Check — CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add health check polling and result reporting to `popcorn pop` so users see whether their deployment is healthy, what was auto-fixed, and what's still broken.

**Architecture:** The CLI sends `verify: true` in the publish payload. If the backend returns a `verify_task_id`, the CLI polls a status endpoint until done, rendering progress. Output includes fix details and remaining errors. Degrades gracefully if the backend hasn't been updated.

**Tech Stack:** Python, argparse, httpx (via existing APIClient)

**Spec:** `docs/superpowers/specs/2026-03-23-pop-health-check-design.md`

---

### File Structure

```
src/popcorn_core/
├── errors.py           (MODIFY) — add EXIT_UNHEALTHY = 5
├── operations.py       (MODIFY) — add verify param to deploy_publish, add deploy_verify_status

src/popcorn_cli/
├── cli.py              (MODIFY) — add --skip-check flag, verify poll loop, result formatting

tests/
├── test_deploy.py      (MODIFY) — add verify tests for operations
├── test_verify.py      (NEW)    — tests for poll loop and output formatting
```

---

### Task 1: Add EXIT_UNHEALTHY Exit Code

**Files:**
- Modify: `src/popcorn_core/errors.py:8-14`
- Test: `tests/test_errors.py`

- [ ] **Step 1: Write failing test**

```python
# Add to tests/test_errors.py
from popcorn_core.errors import EXIT_UNHEALTHY


def test_exit_unhealthy_is_5():
    assert EXIT_UNHEALTHY == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_errors.py::test_exit_unhealthy_is_5 -v`
Expected: FAIL — `ImportError: cannot import name 'EXIT_UNHEALTHY'`

- [ ] **Step 3: Add EXIT_UNHEALTHY to errors.py**

In `src/popcorn_core/errors.py`, after line 13 (`EXIT_SERVER = 4`), add:

```python
EXIT_UNHEALTHY = 5  # Deploy succeeded but site is unhealthy
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_errors.py::test_exit_unhealthy_is_5 -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/popcorn_core/errors.py tests/test_errors.py
git commit -m "feat: add EXIT_UNHEALTHY=5 exit code for unhealthy deployments"
```

---

### Task 2: Add verify Parameter to deploy_publish

**Files:**
- Modify: `src/popcorn_core/operations.py:495-508`
- Test: `tests/test_deploy.py`

- [ ] **Step 1: Write failing test**

```python
# Add to tests/test_deploy.py, inside class TestDeployPublish
def test_deploy_publish_with_verify(self, mock_client):
    mock_client.post.return_value = {
        "conversation_id": "conv-1",
        "site_name": "my-site",
        "version": 3,
        "commit_hash": "abc123",
        "verify_task_id": "task-uuid",
    }
    result = operations.deploy_publish(mock_client, "conv-1", "s3-key-1", verify=True)
    call_data = mock_client.post.call_args[1]["data"]
    assert call_data["verify"] is True
    assert result["verify_task_id"] == "task-uuid"

def test_deploy_publish_without_verify(self, mock_client):
    mock_client.post.return_value = {
        "conversation_id": "conv-1",
        "site_name": "my-site",
        "version": 3,
        "commit_hash": "abc123",
    }
    result = operations.deploy_publish(mock_client, "conv-1", "s3-key-1")
    call_data = mock_client.post.call_args[1]["data"]
    assert "verify" not in call_data
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_deploy.py::TestDeployPublish::test_deploy_publish_with_verify -v`
Expected: FAIL — `TypeError: deploy_publish() got an unexpected keyword argument 'verify'`

- [ ] **Step 3: Add verify parameter to deploy_publish**

In `src/popcorn_core/operations.py`, modify `deploy_publish()` at line 495:

```python
def deploy_publish(
    client: APIClient,
    conversation_id: str,
    s3_key: str,
    context: str = "",
    force: bool = False,
    verify: bool = False,
) -> dict[str, Any]:
    """Publish a tarball from S3 to the conversation's site."""
    data: dict[str, Any] = {"conversation_id": conversation_id, "s3_key": s3_key}
    if context:
        data["context"] = context
    if force:
        data["force"] = True
    if verify:
        data["verify"] = True
    return client.post("/api/conversations/publish", data=data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_deploy.py::TestDeployPublish -v`
Expected: All PASS (including existing tests — no breaking changes)

- [ ] **Step 5: Commit**

```bash
git add src/popcorn_core/operations.py tests/test_deploy.py
git commit -m "feat: add verify parameter to deploy_publish"
```

---

### Task 3: Add deploy_verify_status Operation

**Files:**
- Modify: `src/popcorn_core/operations.py` (after line 549)
- Test: `tests/test_deploy.py`

- [ ] **Step 1: Write failing test**

```python
# Add to tests/test_deploy.py
class TestDeployVerifyStatus:
    def test_deploy_verify_status(self, mock_client):
        mock_client.get.return_value = {
            "status": "done",
            "healthy": True,
            "site_type": "node",
            "fixes": [],
            "errors": [],
            "version": 3,
            "commit_hash": "abc123",
        }
        result = operations.deploy_verify_status(mock_client, "conv-1", "task-uuid")
        mock_client.get.assert_called_once_with(
            "/api/conversations/conv-1/verify-status",
            {"task_id": "task-uuid"},
        )
        assert result["status"] == "done"
        assert result["healthy"] is True

    def test_deploy_verify_status_in_progress(self, mock_client):
        mock_client.get.return_value = {
            "status": "fixing",
            "healthy": None,
            "site_type": "node",
            "fixes": [],
            "errors": [],
            "version": 3,
            "commit_hash": "abc123",
        }
        result = operations.deploy_verify_status(mock_client, "conv-1", "task-uuid")
        assert result["status"] == "fixing"
        assert result["healthy"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_deploy.py::TestDeployVerifyStatus -v`
Expected: FAIL — `AttributeError: module 'popcorn_core.operations' has no attribute 'deploy_verify_status'`

- [ ] **Step 3: Implement deploy_verify_status**

Add to `src/popcorn_core/operations.py` after `get_site_status()` (around line 549):

```python
def deploy_verify_status(
    client: APIClient, conversation_id: str, task_id: str
) -> dict[str, Any]:
    """Poll the verify task status after a publish with verify=true."""
    return client.get(
        f"/api/conversations/{conversation_id}/verify-status",
        {"task_id": task_id},
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_deploy.py::TestDeployVerifyStatus -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/popcorn_core/operations.py tests/test_deploy.py
git commit -m "feat: add deploy_verify_status operation"
```

---

### Task 4: Add --skip-check Flag and Thread verify Through

**Files:**
- Modify: `src/popcorn_cli/cli.py:1919-1923` (argument parsing)
- Modify: `src/popcorn_cli/cli.py:1000-1022` (`_publish_with_retry`)
- Modify: `src/popcorn_cli/cli.py:1122-1127` (publish call in `cmd_pop`)
- Test: `tests/test_verify.py` (new file)

- [ ] **Step 1: Write failing test for --skip-check**

```python
# tests/test_verify.py
"""Tests for pop health verification."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from popcorn_cli.cli import build_parser
from popcorn_core.errors import APIError


# Shared mocks
_PUBLISH_RESULT = {
    "conversation_id": "conv-1",
    "site_name": "my-site",
    "version": 3,
    "commit_hash": "abc123",
}

_PUBLISH_RESULT_WITH_VERIFY = {
    **_PUBLISH_RESULT,
    "verify_task_id": "task-uuid",
}

_SITE_STATUS = {"url": "https://my-site.popcorn.ai"}


class TestSkipCheck:
    def test_skip_check_omits_verify_from_payload(self, tmp_path, monkeypatch):
        """--skip-check should not send verify in publish payload."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".popcorn.local.json").write_text(
            json.dumps({"conversation_id": "conv-1", "site_name": "my-site"})
        )

        mock_client = MagicMock()
        mock_client.get.side_effect = [
            {},  # validate_channel
            _SITE_STATUS,  # get_site_status
        ]
        mock_client.post.side_effect = [
            {"upload_url": "https://s3/", "upload_fields": {}, "s3_key": "k"},  # presign
            _PUBLISH_RESULT,  # publish
        ]

        with patch("popcorn_cli.cli._get_client", return_value=mock_client), \
             patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")), \
             patch("popcorn_cli.cli.operations.deploy_upload"), \
             patch("os.unlink"):
            (tmp_path / "t.tar.gz").write_bytes(b"fake")

            parser = build_parser()
            args = parser.parse_args(["pop", "--skip-check"])
            from popcorn_cli.cli import cmd_pop
            cmd_pop(args)

        # Check the publish call — verify should NOT be in the data
        publish_call = mock_client.post.call_args_list[1]
        publish_data = publish_call[1]["data"] if "data" in publish_call[1] else publish_call[0][1]
        assert "verify" not in publish_data

    def test_no_skip_check_sends_verify(self, tmp_path, monkeypatch):
        """Without --skip-check, verify=true should be in publish payload."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".popcorn.local.json").write_text(
            json.dumps({"conversation_id": "conv-1", "site_name": "my-site"})
        )

        mock_client = MagicMock()
        mock_client.get.side_effect = [
            {},  # validate_channel
            # verify-status returns done immediately
            {"status": "done", "healthy": True, "site_type": "node",
             "fixes": [], "errors": [], "version": 3, "commit_hash": "abc123"},
            _SITE_STATUS,  # get_site_status
        ]
        mock_client.post.side_effect = [
            {"upload_url": "https://s3/", "upload_fields": {}, "s3_key": "k"},  # presign
            _PUBLISH_RESULT_WITH_VERIFY,  # publish with verify_task_id
        ]

        with patch("popcorn_cli.cli._get_client", return_value=mock_client), \
             patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")), \
             patch("popcorn_cli.cli.operations.deploy_upload"), \
             patch("os.unlink"):
            (tmp_path / "t.tar.gz").write_bytes(b"fake")

            parser = build_parser()
            args = parser.parse_args(["pop"])
            from popcorn_cli.cli import cmd_pop
            cmd_pop(args)

        # Check the publish call — verify SHOULD be in the data
        publish_call = mock_client.post.call_args_list[1]
        publish_data = publish_call[1]["data"]  # keyword arg, not positional
        assert publish_data["verify"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_verify.py::TestSkipCheck -v`
Expected: FAIL

- [ ] **Step 3: Add --skip-check argument**

In `src/popcorn_cli/cli.py`, after the existing pop arguments (around line 1923), add:

```python
pop_p.add_argument("--skip-check", action="store_true", help="Skip health verification")
```

- [ ] **Step 4: Thread verify through _publish_with_retry**

Modify `_publish_with_retry` at line 1000:

```python
def _publish_with_retry(
    client: APIClient,
    conversation_id: str,
    s3_key: str,
    context: str,
    force: bool,
    json_mode: bool,
    verify: bool = False,
) -> dict[str, Any]:
    """Call deploy_publish with retry on 502 (up to 3 retries, exponential backoff)."""
    import time

    max_retries = 3
    for attempt in range(max_retries + 1):
        try:
            return operations.deploy_publish(
                client, conversation_id, s3_key, context, force=force, verify=verify
            )
        except APIError as e:
            if e.status_code != 502 or attempt == max_retries:
                raise
            delay = 2**attempt  # 1, 2, 4
            if not json_mode:
                _status(f"Retrying publish (attempt {attempt + 2}/{max_retries + 1})...")
            time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
```

- [ ] **Step 5: Pass verify flag in cmd_pop publish call**

In `cmd_pop()`, modify the publish call at line 1125:

```python
        skip_check = getattr(args, "skip_check", False)
        try:
            result = _publish_with_retry(
                client, conversation_id, s3_key, args.context, force, json_mode,
                verify=not skip_check,
            )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_verify.py::TestSkipCheck -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/popcorn_cli/cli.py tests/test_verify.py
git commit -m "feat: add --skip-check flag, thread verify through publish"
```

---

### Task 5: Poll Loop Implementation

**Files:**
- Modify: `src/popcorn_cli/cli.py` (add `_poll_verify` helper, call from `cmd_pop`)
- Test: `tests/test_verify.py`

- [ ] **Step 1: Write failing tests for poll loop**

```python
# Add to tests/test_verify.py
import time
from popcorn_cli.cli import _poll_verify


class TestPollVerify:
    def test_poll_verify_immediate_done(self):
        """Backend returns done on first poll."""
        mock_client = MagicMock()
        mock_client.get.return_value = {
            "status": "done",
            "healthy": True,
            "site_type": "node",
            "fixes": [],
            "errors": [],
            "version": 3,
            "commit_hash": "abc123",
        }
        result = _poll_verify(mock_client, "conv-1", "task-uuid", json_mode=False, timeout=10)
        assert result["status"] == "done"
        assert result["healthy"] is True

    def test_poll_verify_progression(self):
        """Backend progresses through statuses before done."""
        mock_client = MagicMock()
        mock_client.get.side_effect = [
            {"status": "restarting", "healthy": None},
            {"status": "checking", "healthy": None},
            {"status": "done", "healthy": True, "site_type": "node",
             "fixes": [], "errors": [], "version": 3, "commit_hash": "abc"},
        ]
        result = _poll_verify(
            mock_client, "conv-1", "task-uuid", json_mode=False, timeout=10, poll_interval=0.01
        )
        assert result["status"] == "done"
        assert mock_client.get.call_count == 3

    def test_poll_verify_timeout(self):
        """Returns timeout status when deadline exceeded."""
        mock_client = MagicMock()
        mock_client.get.return_value = {"status": "fixing", "healthy": None}

        # Use deterministic time: monotonic returns 0, then past deadline
        with patch("time.monotonic", side_effect=[0, 0, 999]), \
             patch("time.sleep"):
            result = _poll_verify(
                mock_client, "conv-1", "task-uuid", json_mode=False, timeout=10, poll_interval=2.0
            )
        assert result["status"] == "timeout"
        assert result["healthy"] is None

    def test_poll_verify_404_graceful_degradation(self):
        """404 means backend doesn't support verify — degrade gracefully."""
        mock_client = MagicMock()
        mock_client.get.side_effect = APIError("Not found", status_code=404)
        result = _poll_verify(mock_client, "conv-1", "task-uuid", json_mode=False, timeout=10)
        assert result is None  # Signals: no verify data available

    def test_poll_verify_transient_errors_retry(self):
        """Transient 500s are retried silently."""
        mock_client = MagicMock()
        mock_client.get.side_effect = [
            APIError("Server error", status_code=500),
            APIError("Server error", status_code=500),
            {"status": "done", "healthy": True, "site_type": "node",
             "fixes": [], "errors": [], "version": 3, "commit_hash": "abc"},
        ]
        result = _poll_verify(
            mock_client, "conv-1", "task-uuid", json_mode=False, timeout=10, poll_interval=0.01
        )
        assert result["status"] == "done"
        assert result["healthy"] is True

    def test_poll_verify_persistent_errors(self):
        """3+ consecutive errors → stop polling."""
        mock_client = MagicMock()
        mock_client.get.side_effect = APIError("Server error", status_code=500)
        result = _poll_verify(
            mock_client, "conv-1", "task-uuid", json_mode=False, timeout=10, poll_interval=0.01
        )
        assert result["status"] == "error"
        assert result["healthy"] is None
        assert mock_client.get.call_count == 3  # Stops after 3 consecutive failures
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_verify.py::TestPollVerify -v`
Expected: FAIL — `ImportError: cannot import name '_poll_verify'`

- [ ] **Step 3: Implement _poll_verify**

Add to `src/popcorn_cli/cli.py`, before `cmd_pop()`:

```python
def _poll_verify(
    client: APIClient,
    conversation_id: str,
    task_id: str,
    json_mode: bool,
    timeout: float = 300.0,
    poll_interval: float = 2.0,
) -> dict[str, Any] | None:
    """Poll verify-status until done, timeout, or failure.

    Returns the final verify status dict, or None on graceful degradation
    (404, persistent errors).
    """
    import time

    _STATUS_MESSAGES = {
        "restarting": "Restarting site...",
        "checking": "Checking health...",
        "fixing": "Fixing issues...",
    }

    deadline = time.monotonic() + timeout
    consecutive_errors = 0
    last_status = None

    while time.monotonic() < deadline:
        try:
            result = operations.deploy_verify_status(client, conversation_id, task_id)
            consecutive_errors = 0  # Reset on success
        except APIError as e:
            if e.status_code == 404:
                # Backend doesn't support verify — degrade gracefully
                return None
            consecutive_errors += 1
            if consecutive_errors >= 3:
                if not json_mode:
                    _status("Health check unavailable — skipping.")
                return {"status": "error", "healthy": None}
            time.sleep(poll_interval)
            continue

        status = result.get("status", "")

        # Show progress (only when status changes)
        if status != last_status and not json_mode:
            msg = _STATUS_MESSAGES.get(status)
            if msg:
                _status(msg)
            last_status = status

        if status == "done":
            return result

        # Increase interval during fixing phase (agent work takes minutes)
        interval = 5.0 if status == "fixing" else poll_interval
        time.sleep(interval)

    # Timeout
    if not json_mode:
        _status("Health check timed out — site may still be verifying.")
    return {"status": "timeout", "healthy": None}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_verify.py::TestPollVerify -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/popcorn_cli/cli.py tests/test_verify.py
git commit -m "feat: implement _poll_verify with progress, timeout, and error handling"
```

---

### Task 6: Integrate Poll Loop and Result Formatting into cmd_pop

**Files:**
- Modify: `src/popcorn_cli/cli.py:1150-1182` (after publish, before output)
- Test: `tests/test_verify.py`

- [ ] **Step 1: Write failing tests for full cmd_pop verify integration**

```python
# Add to tests/test_verify.py
from popcorn_core.errors import EXIT_UNHEALTHY


class TestCmdPopVerifyIntegration:
    """Test the full cmd_pop flow with verify enabled."""

    def _run_pop(self, tmp_path, monkeypatch, publish_result, verify_responses, capsys):
        """Helper to run cmd_pop with mocked responses."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".popcorn.local.json").write_text(
            json.dumps({"conversation_id": "conv-1", "site_name": "my-site"})
        )

        mock_client = MagicMock()
        get_responses = [{}]  # validate_channel
        if isinstance(verify_responses, list):
            get_responses.extend(verify_responses)
        get_responses.append(_SITE_STATUS)  # get_site_status
        mock_client.get.side_effect = get_responses
        mock_client.post.side_effect = [
            {"upload_url": "https://s3/", "upload_fields": {}, "s3_key": "k"},
            publish_result,
        ]

        with patch("popcorn_cli.cli._get_client", return_value=mock_client), \
             patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")), \
             patch("popcorn_cli.cli.operations.deploy_upload"), \
             patch("os.unlink"), \
             patch("time.sleep"):
            (tmp_path / "t.tar.gz").write_bytes(b"fake")

            parser = build_parser()
            args = parser.parse_args(["pop"])
            from popcorn_cli.cli import cmd_pop
            cmd_pop(args)

        return capsys.readouterr()

    def test_verify_healthy_no_warning(self, tmp_path, monkeypatch, capsys):
        out, err = self._run_pop(
            tmp_path, monkeypatch,
            publish_result=_PUBLISH_RESULT_WITH_VERIFY,
            verify_responses=[
                {"status": "done", "healthy": True, "site_type": "node",
                 "fixes": [], "errors": [], "version": 3, "commit_hash": "abc123"},
            ],
            capsys=capsys,
        )
        assert "Published to #my-site (v3)" in out
        assert "Fixed" not in out
        assert "issue" not in out

    def test_verify_fixed_shows_fixes(self, tmp_path, monkeypatch, capsys):
        out, err = self._run_pop(
            tmp_path, monkeypatch,
            publish_result=_PUBLISH_RESULT_WITH_VERIFY,
            verify_responses=[
                {"status": "done", "healthy": True, "site_type": "node",
                 "fixes": [{"file": "server.js", "description": "added express import"}],
                 "errors": [], "version": 4, "commit_hash": "def456"},
            ],
            capsys=capsys,
        )
        assert "Published to #my-site (v4)" in out
        assert "Fixed 1 issue" in out or "Fixed 1 issues" in out
        assert "server.js" in out

    def test_verify_still_broken_exits_5(self, tmp_path, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc_info:
            self._run_pop(
                tmp_path, monkeypatch,
                publish_result=_PUBLISH_RESULT_WITH_VERIFY,
                verify_responses=[
                    {"status": "done", "healthy": False, "site_type": "node",
                     "fixes": [], "errors": ["Cannot find module 'foo'"],
                     "version": 3, "commit_hash": "abc123"},
                ],
                capsys=capsys,
            )
        assert exc_info.value.code == EXIT_UNHEALTHY

    def test_static_site_no_polling(self, tmp_path, monkeypatch, capsys):
        """Static site response — no verify_task_id, no polling."""
        static_publish = {
            **_PUBLISH_RESULT,
            "verify": {"skipped": True, "reason": "static"},
        }
        out, err = self._run_pop(
            tmp_path, monkeypatch,
            publish_result=static_publish,
            verify_responses=[],  # No verify-status calls
            capsys=capsys,
        )
        assert "Published to #my-site (v3)" in out

    def test_json_output_includes_verify(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".popcorn.local.json").write_text(
            json.dumps({"conversation_id": "conv-1", "site_name": "my-site"})
        )

        mock_client = MagicMock()
        mock_client.get.side_effect = [
            {},  # validate_channel
            {"status": "done", "healthy": True, "site_type": "node",
             "fixes": [], "errors": [], "version": 3, "commit_hash": "abc123"},
            _SITE_STATUS,
        ]
        mock_client.post.side_effect = [
            {"upload_url": "https://s3/", "upload_fields": {}, "s3_key": "k"},
            _PUBLISH_RESULT_WITH_VERIFY,
        ]

        with patch("popcorn_cli.cli._get_client", return_value=mock_client), \
             patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")), \
             patch("popcorn_cli.cli.operations.deploy_upload"), \
             patch("os.unlink"), \
             patch("time.sleep"):
            (tmp_path / "t.tar.gz").write_bytes(b"fake")

            parser = build_parser()
            args = parser.parse_args(["pop", "--json"])
            from popcorn_cli.cli import cmd_pop
            cmd_pop(args)

        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["ok"] is True
        assert "verify" in data["data"]
        assert data["data"]["verify"]["healthy"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_verify.py::TestCmdPopVerifyIntegration -v`
Expected: FAIL

- [ ] **Step 3: Integrate verify into cmd_pop**

In `src/popcorn_cli/cli.py`, modify `cmd_pop()` after the publish result (around line 1150). Replace the output section (lines 1172-1182) with:

```python
    # --- Verify health (if backend returned verify_task_id) ---
    verify_data = None
    verify_task_id = result.get("verify_task_id")
    original_version = result.get("version")

    if verify_task_id:
        verify_data = _poll_verify(client, result_conv_id, verify_task_id, json_mode)

    # If publish returned a static skip, capture that
    if not verify_data and "verify" in result:
        verify_data = result["verify"]

    # Use final version from verify if available
    display_version = result.get("version")
    if verify_data and verify_data.get("version"):
        display_version = verify_data["version"]

    # Fetch site URL for output (non-fatal)
    site_url = None
    try:
        site_status = operations.get_site_status(client, result_conv_id)
        site_url = site_status.get("url")
    except PopcornError:
        pass

    # Build output
    output_data: dict[str, Any] = {**result}
    if site_url:
        output_data["site_url"] = site_url
    if suggested_name:
        output_data["suggested_name"] = suggested_name
    if verify_data:
        output_data["verify"] = verify_data
        if verify_data.get("version"):
            output_data["version"] = verify_data["version"]
        if verify_data.get("commit_hash"):
            output_data["commit_hash"] = verify_data["commit_hash"]

    # Format human output
    human_line = f"Published to #{result_site_name} (v{display_version})"
    if site_url:
        human_line += f"\n{site_url}"

    # Append verify results to human output
    if verify_data and verify_data.get("status") == "done":
        fixes = verify_data.get("fixes", [])
        errors = verify_data.get("errors", [])
        healthy = verify_data.get("healthy")

        if fixes and healthy:
            n = len(fixes)
            human_line += f"\n⚠ Fixed {n} issue{'s' if n != 1 else ''} (v{original_version} → v{display_version}):"
            for fix in fixes:
                human_line += f"\n  • {fix['file']}: {fix['description']}"
        elif errors:
            n = len(errors)
            if fixes:
                human_line += f"\n⚠ {n} issue{'s' if n != 1 else ''} remain{'s' if n == 1 else ''} after auto-fix (v{original_version} → v{display_version}):"
            else:
                human_line += f"\n⚠ {n} issue{'s' if n != 1 else ''}:"
            for error in errors:
                human_line += f"\n  • {error}"

    _output(args, output_data, human_line)

    # Exit code based on health
    if verify_data and verify_data.get("status") == "done" and verify_data.get("healthy") is False:
        sys.exit(EXIT_UNHEALTHY)
```

Also add the import at the top of `cli.py`:

```python
from popcorn_core.errors import EXIT_UNHEALTHY
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_verify.py -v`
Expected: All PASS

- [ ] **Step 5: Run full test suite to verify no regressions**

Run: `make test`
Expected: All ~205+ tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/popcorn_cli/cli.py src/popcorn_core/errors.py tests/test_verify.py
git commit -m "feat: integrate verify poll loop and result formatting into cmd_pop"
```

---

### Task 7: Edge Cases — Ctrl+C, Version Display, Graceful Degradation

**Files:**
- Modify: `src/popcorn_cli/cli.py` (Ctrl+C handling in poll loop)
- Test: `tests/test_verify.py`

- [ ] **Step 1: Write failing tests for edge cases**

```python
# Add to tests/test_verify.py
class TestPollVerifyEdgeCases:
    def test_version_display_uses_verify_version(self):
        """When agent fixes (v3 → v4), output shows v4."""
        mock_client = MagicMock()
        mock_client.get.return_value = {
            "status": "done", "healthy": True, "site_type": "node",
            "fixes": [{"file": "server.js", "description": "fixed"}],
            "errors": [], "version": 4, "commit_hash": "def456",
        }
        result = _poll_verify(mock_client, "conv-1", "task-uuid", json_mode=False, timeout=10)
        assert result["version"] == 4

    def test_poll_verify_fixing_uses_longer_interval(self):
        """During 'fixing' status, poll interval should be 5s."""
        mock_client = MagicMock()
        mock_client.get.side_effect = [
            {"status": "fixing", "healthy": None},
            {"status": "done", "healthy": True, "site_type": "node",
             "fixes": [], "errors": [], "version": 3, "commit_hash": "abc"},
        ]
        with patch("time.sleep") as mock_sleep:
            _poll_verify(
                mock_client, "conv-1", "task-uuid", json_mode=False, timeout=30, poll_interval=2.0
            )
        # First sleep should be 5.0 (fixing interval), not 2.0
        mock_sleep.assert_any_call(5.0)

    def test_backend_without_verify_support(self):
        """No verify_task_id in publish response — skip polling entirely."""
        publish_result = {
            "conversation_id": "conv-1",
            "site_name": "my-site",
            "version": 3,
            "commit_hash": "abc123",
        }
        # No verify_task_id → _poll_verify should never be called
        assert "verify_task_id" not in publish_result

    def test_ctrl_c_returns_cancelled(self):
        """KeyboardInterrupt during poll returns cancelled status."""
        mock_client = MagicMock()
        mock_client.get.side_effect = KeyboardInterrupt()
        result = _poll_verify(mock_client, "conv-1", "task-uuid", json_mode=False, timeout=10)
        assert result["status"] == "cancelled"
        assert result["healthy"] is None

    def test_skip_check_json_omits_verify_key(self, tmp_path, monkeypatch, capsys):
        """--skip-check --json output should not include verify key."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".popcorn.local.json").write_text(
            json.dumps({"conversation_id": "conv-1", "site_name": "my-site"})
        )

        mock_client = MagicMock()
        mock_client.get.side_effect = [
            {},  # validate_channel
            _SITE_STATUS,
        ]
        mock_client.post.side_effect = [
            {"upload_url": "https://s3/", "upload_fields": {}, "s3_key": "k"},
            _PUBLISH_RESULT,  # no verify_task_id
        ]

        with patch("popcorn_cli.cli._get_client", return_value=mock_client), \
             patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")), \
             patch("popcorn_cli.cli.operations.deploy_upload"), \
             patch("os.unlink"):
            (tmp_path / "t.tar.gz").write_bytes(b"fake")

            parser = build_parser()
            args = parser.parse_args(["pop", "--skip-check", "--json"])
            from popcorn_cli.cli import cmd_pop
            cmd_pop(args)

        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["ok"] is True
        assert "verify" not in data["data"]
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_verify.py::TestPollVerifyEdgeCases -v`

- [ ] **Step 3: Add KeyboardInterrupt handling to _poll_verify**

In `_poll_verify`, wrap the while loop:

```python
    try:
        while time.monotonic() < deadline:
            # ... existing loop body ...
            pass
    except KeyboardInterrupt:
        if not json_mode:
            _status("Health check cancelled.")
        return {"status": "cancelled", "healthy": None}
```

- [ ] **Step 4: Verify Ctrl+C, timeout, and error states are handled correctly in cmd_pop**

The Task 6 code already gates fix/error formatting on `verify_data.get("status") == "done"`, so cancelled/timeout/error states naturally skip that block. The exit code check (`healthy is False`) also works correctly since `healthy` is `None` (not `False`) for these states — exit code remains 0.

No additional code needed, but verify this by running:

Run: `pytest tests/test_verify.py::TestPollVerifyEdgeCases::test_ctrl_c_returns_cancelled -v`
Expected: PASS

- [ ] **Step 5: Run full test suite**

Run: `make test`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/popcorn_cli/cli.py tests/test_verify.py
git commit -m "feat: handle Ctrl+C, fixing interval, graceful degradation edge cases"
```

---

### Task 8: Final Verification

**Files:** None (verification only)

- [ ] **Step 1: Run make check (lint + typecheck + test)**

Run: `make check`
Expected: All pass — ruff, mypy, pytest

- [ ] **Step 2: Manual smoke test with --skip-check**

Run: `popcorn pop --skip-check`
Expected: Current behavior, no polling, no errors

- [ ] **Step 3: Manual smoke test without --skip-check (backend not updated)**

Run: `popcorn pop`
Expected: Graceful degradation — no `verify_task_id` in response, CLI skips polling, same output as today

- [ ] **Step 4: Verify --json output shape**

Run: `popcorn pop --json --skip-check`
Expected: JSON envelope without `verify` key

- [ ] **Step 5: Commit any final fixes**

```bash
git add -A
git commit -m "chore: final cleanup for pop health check CLI"
```
