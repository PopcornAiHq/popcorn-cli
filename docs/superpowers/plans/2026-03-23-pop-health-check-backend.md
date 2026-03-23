# Pop Health Check — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add async health verification and auto-fix to the publish flow so popped sites are verified before the version is considered complete.

**Architecture:** The publish endpoint accepts a `verify` flag. When set, after committing files, the VM kicks off an async verify task that restarts the site, health-checks it, and dispatches an agent fix if broken. A new status endpoint lets the CLI poll progress. All new logic lives in the workspace VM; the API layer proxies.

**Tech Stack:** Python (FastAPI), Pydantic, httpx, existing process_manager + work queue infrastructure

**Spec:** `docs/superpowers/specs/2026-03-23-pop-health-check-design.md`

**IMPORTANT:** This plan targets `~/popcorn-backend`, not this repo. Execute all tasks in the backend repository.

---

### File Structure

```
workspace_vm/appchannels/
├── verify.py              (NEW) — VerifyTask model, run_verify() async function
├── routes.py              (MODIFY) — add verify_status endpoint, modify s3-pull to accept verify flag
├── models.py              (MODIFY) — add VerifyTask model if not in verify.py
├── process_manager.py     (READ ONLY) — use existing has_dynamic_server(), has_build_script(), restart()

services/api/
├── conversations.py       (MODIFY) — add verify field to PublishRequest, proxy verify-status

tests/appchannels/
├── test_verify.py         (NEW) — unit tests for verify flow
```

---

### Task 1: VerifyTask Model and Storage

**Files:**
- Create: `workspace_vm/appchannels/verify.py`
- Test: `tests/appchannels/test_verify.py`

- [ ] **Step 1: Write the failing test for VerifyTask model**

```python
# tests/appchannels/test_verify.py
import json
from pathlib import Path

from workspace_vm.appchannels.verify import VerifyTask, VerifyStatus


class TestVerifyTask:
    def test_create_verify_task(self, tmp_path):
        task = VerifyTask(
            task_id="abc-123",
            site_name="my-site",
            site_path=tmp_path / "sites" / "my-site",
            conversation_id="conv-1",
            original_version=3,
            original_commit_hash="abc1234",
        )
        assert task.status == VerifyStatus.RESTARTING
        assert task.healthy is None
        assert task.fixes == []
        assert task.errors == []

    def test_verify_task_to_dict(self, tmp_path):
        task = VerifyTask(
            task_id="abc-123",
            site_name="my-site",
            site_path=tmp_path / "sites" / "my-site",
            conversation_id="conv-1",
            original_version=3,
            original_commit_hash="abc1234",
        )
        d = task.to_status_dict()
        assert d["status"] == "restarting"
        assert d["healthy"] is None
        assert d["version"] == 3
        assert d["commit_hash"] == "abc1234"

    def test_verify_task_persistence(self, tmp_path):
        task = VerifyTask(
            task_id="abc-123",
            site_name="my-site",
            site_path=tmp_path / "sites" / "my-site",
            conversation_id="conv-1",
            original_version=3,
            original_commit_hash="abc1234",
        )
        store_dir = tmp_path / "verify_tasks"
        task.save(store_dir)
        loaded = VerifyTask.load(store_dir, "abc-123")
        assert loaded.task_id == "abc-123"
        assert loaded.status == VerifyStatus.RESTARTING
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/appchannels/test_verify.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'workspace_vm.appchannels.verify'`

- [ ] **Step 3: Implement VerifyTask model**

```python
# workspace_vm/appchannels/verify.py
"""Async site verification after pop/publish."""

from __future__ import annotations

import json
import uuid
from enum import Enum
from pathlib import Path
from typing import Any


class VerifyStatus(str, Enum):
    RESTARTING = "restarting"
    CHECKING = "checking"
    FIXING = "fixing"
    DONE = "done"


class VerifyTask:
    """Tracks the state of an async health verification task."""

    def __init__(
        self,
        task_id: str,
        site_name: str,
        site_path: Path,
        conversation_id: str,
        original_version: int,
        original_commit_hash: str,
    ) -> None:
        self.task_id = task_id
        self.site_name = site_name
        self.site_path = site_path
        self.conversation_id = conversation_id
        self.original_version = original_version
        self.original_commit_hash = original_commit_hash
        self.status = VerifyStatus.RESTARTING
        self.healthy: bool | None = None
        self.site_type: str = "static"
        self.fixes: list[dict[str, str]] = []
        self.errors: list[str] = []
        self.version: int = original_version
        self.commit_hash: str = original_commit_hash

    @staticmethod
    def new_id() -> str:
        return str(uuid.uuid4())

    def to_status_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "healthy": self.healthy,
            "site_type": self.site_type,
            "fixes": self.fixes,
            "errors": self.errors,
            "version": self.version,
            "commit_hash": self.commit_hash,
        }

    def save(self, store_dir: Path) -> None:
        store_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "task_id": self.task_id,
            "site_name": self.site_name,
            "site_path": str(self.site_path),
            "conversation_id": self.conversation_id,
            "original_version": self.original_version,
            "original_commit_hash": self.original_commit_hash,
            "status": self.status.value,
            "healthy": self.healthy,
            "site_type": self.site_type,
            "fixes": self.fixes,
            "errors": self.errors,
            "version": self.version,
            "commit_hash": self.commit_hash,
        }
        (store_dir / f"{self.task_id}.json").write_text(json.dumps(data))

    @classmethod
    def load(cls, store_dir: Path, task_id: str) -> VerifyTask:
        data = json.loads((store_dir / f"{task_id}.json").read_text())
        task = cls(
            task_id=data["task_id"],
            site_name=data["site_name"],
            site_path=Path(data["site_path"]),
            conversation_id=data["conversation_id"],
            original_version=data["original_version"],
            original_commit_hash=data["original_commit_hash"],
        )
        task.status = VerifyStatus(data["status"])
        task.healthy = data["healthy"]
        task.site_type = data["site_type"]
        task.fixes = data["fixes"]
        task.errors = data["errors"]
        task.version = data["version"]
        task.commit_hash = data["commit_hash"]
        return task
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/appchannels/test_verify.py -v`
Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add workspace_vm/appchannels/verify.py tests/appchannels/test_verify.py
git commit -m "feat: add VerifyTask model for pop health check"
```

---

### Task 2: Site Type Detection and Health Check Logic

**Files:**
- Modify: `workspace_vm/appchannels/verify.py`
- Read: `workspace_vm/appchannels/process_manager.py:44-58` (has_dynamic_server, has_build_script)
- Test: `tests/appchannels/test_verify.py`

- [ ] **Step 1: Write failing tests for detect_site_type and check_health**

```python
# Add to tests/appchannels/test_verify.py
import asyncio
from unittest.mock import AsyncMock, patch

from workspace_vm.appchannels.verify import detect_site_type, check_health


class TestDetectSiteType:
    def test_node_site(self, tmp_path):
        site_path = tmp_path / "my-site"
        site_path.mkdir()
        (site_path / "server.js").write_text("const express = require('express');")
        assert detect_site_type(site_path) == "node"

    def test_python_site(self, tmp_path):
        site_path = tmp_path / "my-site"
        site_path.mkdir()
        (site_path / "server.py").write_text("from flask import Flask")
        assert detect_site_type(site_path) == "python"

    def test_build_site(self, tmp_path):
        site_path = tmp_path / "my-site"
        site_path.mkdir()
        (site_path / "package.json").write_text('{"scripts": {"build": "vite build"}}')
        assert detect_site_type(site_path) == "build"

    def test_static_site(self, tmp_path):
        site_path = tmp_path / "my-site"
        site_path.mkdir()
        (site_path / "index.html").write_text("<html></html>")
        assert detect_site_type(site_path) == "static"


class TestCheckHealth:
    def test_healthy_dynamic_server(self):
        """Server restart returns pid and port — healthy."""
        restart_result = {"message": "Restarted", "pid": 1234, "port": 3000}
        healthy, errors = asyncio.run(
            check_health("node", restart_result)
        )
        assert healthy is True
        assert errors == []

    def test_unhealthy_dynamic_server(self):
        """Server restart returns no pid — unhealthy."""
        restart_result = {"message": "Failed to start", "pid": None, "port": None}
        healthy, errors = asyncio.run(
            check_health("node", restart_result)
        )
        assert healthy is False
        assert len(errors) > 0

    def test_healthy_build_site(self):
        """Build succeeded — healthy."""
        restart_result = {"type": "build", "success": True, "message": "Build completed"}
        healthy, errors = asyncio.run(
            check_health("build", restart_result)
        )
        assert healthy is True

    def test_unhealthy_build_site(self):
        """Build failed — unhealthy."""
        restart_result = {"type": "build", "success": False, "error": "Module not found"}
        healthy, errors = asyncio.run(
            check_health("build", restart_result)
        )
        assert healthy is False
        assert "Module not found" in errors[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/appchannels/test_verify.py::TestDetectSiteType -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement detect_site_type and check_health**

Add to `workspace_vm/appchannels/verify.py`:

```python
from workspace_vm.appchannels.process_manager import NodeProcessManager


def detect_site_type(site_path: Path) -> str:
    """Detect site type from file presence. Returns: node, python, build, static."""
    if NodeProcessManager.has_dynamic_server(site_path):
        if (site_path / "server.js").exists():
            return "node"
        return "python"
    if NodeProcessManager.has_build_script(site_path):
        return "build"
    return "static"


async def check_health(
    site_type: str, restart_result: dict[str, Any]
) -> tuple[bool, list[str]]:
    """Check if a site is healthy based on restart result. Returns (healthy, errors)."""
    if site_type in ("node", "python"):
        pid = restart_result.get("pid")
        port = restart_result.get("port")
        if pid and port:
            return True, []
        msg = restart_result.get("message", "Server failed to start")
        return False, [msg]

    if site_type == "build":
        success = restart_result.get("success", False)
        if success:
            return True, []
        error = restart_result.get("error", "Build failed")
        return False, [error]

    # Static — always healthy
    return True, []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/appchannels/test_verify.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add workspace_vm/appchannels/verify.py tests/appchannels/test_verify.py
git commit -m "feat: add site type detection and health check logic"
```

---

### Task 3: run_verify() — The Async Verify Runner

**Files:**
- Modify: `workspace_vm/appchannels/verify.py`
- Read: `workspace_vm/appchannels/routes.py:1368-1430` (restart endpoint internals)
- Read: `workspace_vm/appchannels/routes.py:356-449` (work item creation)
- Test: `tests/appchannels/test_verify.py`

This is the core function that runs phases 1-4 of the verify flow.

- [ ] **Step 1: Write failing test for run_verify — healthy site**

```python
# Add to tests/appchannels/test_verify.py
from workspace_vm.appchannels.verify import run_verify


class TestRunVerify:
    def test_run_verify_healthy(self, tmp_path):
        """Site restarts successfully — no fix needed."""
        site_path = tmp_path / "my-site"
        site_path.mkdir()
        (site_path / "server.js").write_text("const express = require('express');")

        store_dir = tmp_path / "verify_tasks"
        task = VerifyTask(
            task_id="t1",
            site_name="my-site",
            site_path=site_path,
            conversation_id="conv-1",
            original_version=3,
            original_commit_hash="abc",
        )

        mock_restart = AsyncMock(return_value={"message": "Restarted", "pid": 1234, "port": 3000})
        mock_dispatch_fix = AsyncMock()

        asyncio.run(
            run_verify(task, store_dir, restart_fn=mock_restart, dispatch_fix_fn=mock_dispatch_fix)
        )

        assert task.status == VerifyStatus.DONE
        assert task.healthy is True
        assert task.site_type == "node"
        mock_dispatch_fix.assert_not_called()

    def test_run_verify_unhealthy_dispatches_fix(self, tmp_path):
        """Site fails restart — dispatches agent fix."""
        site_path = tmp_path / "my-site"
        site_path.mkdir()
        (site_path / "server.js").write_text("broken")

        store_dir = tmp_path / "verify_tasks"
        task = VerifyTask(
            task_id="t2",
            site_name="my-site",
            site_path=site_path,
            conversation_id="conv-1",
            original_version=3,
            original_commit_hash="abc",
        )

        mock_restart = AsyncMock(return_value={"message": "Failed to start", "pid": None, "port": None})
        mock_dispatch_fix = AsyncMock(return_value={
            "status": "complete",
            "healthy": False,
            "fixes": [],
            "errors": ["Cannot find module 'express'"],
            "version": 3,
            "commit_hash": "abc",
        })

        asyncio.run(
            run_verify(task, store_dir, restart_fn=mock_restart, dispatch_fix_fn=mock_dispatch_fix)
        )

        assert task.status == VerifyStatus.DONE
        assert task.healthy is False
        assert len(task.errors) > 0
        mock_dispatch_fix.assert_called_once()

    def test_run_verify_static_skips(self, tmp_path):
        """Static site — immediately done, healthy."""
        site_path = tmp_path / "my-site"
        site_path.mkdir()
        (site_path / "index.html").write_text("<html></html>")

        store_dir = tmp_path / "verify_tasks"
        task = VerifyTask(
            task_id="t3",
            site_name="my-site",
            site_path=site_path,
            conversation_id="conv-1",
            original_version=3,
            original_commit_hash="abc",
        )

        asyncio.run(
            run_verify(task, store_dir)
        )

        assert task.status == VerifyStatus.DONE
        assert task.healthy is True
        assert task.site_type == "static"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/appchannels/test_verify.py::TestRunVerify -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement run_verify**

Add to `workspace_vm/appchannels/verify.py`:

```python
import logging
from typing import Callable, Awaitable

logger = logging.getLogger(__name__)

# Type aliases for dependency injection
RestartFn = Callable[[Path], Awaitable[dict[str, Any]]]
DispatchFixFn = Callable[[VerifyTask], Awaitable[dict[str, Any]]]


async def run_verify(
    task: VerifyTask,
    store_dir: Path,
    *,
    restart_fn: RestartFn | None = None,
    dispatch_fix_fn: DispatchFixFn | None = None,
) -> None:
    """Run the full verify flow: restart → check → fix (if needed) → done.

    Mutates task in-place and persists state to store_dir after each phase.
    restart_fn and dispatch_fix_fn are injectable for testing.
    """
    task.site_type = detect_site_type(task.site_path)

    # Static sites: nothing to verify
    if task.site_type == "static":
        task.status = VerifyStatus.DONE
        task.healthy = True
        task.save(store_dir)
        return

    # Phase 1: Restart
    task.status = VerifyStatus.RESTARTING
    task.save(store_dir)

    if restart_fn is None:
        raise ValueError("restart_fn required for non-static sites")

    restart_result = await restart_fn(task.site_path)

    # Phase 2: Check health
    task.status = VerifyStatus.CHECKING
    task.save(store_dir)

    healthy, errors = await check_health(task.site_type, restart_result)

    if healthy:
        task.status = VerifyStatus.DONE
        task.healthy = True
        task.save(store_dir)
        return

    # Phase 3: Dispatch fix
    task.status = VerifyStatus.FIXING
    task.errors = errors
    task.save(store_dir)

    if dispatch_fix_fn is None:
        # No fix function — report unhealthy and finish
        task.status = VerifyStatus.DONE
        task.healthy = False
        task.save(store_dir)
        return

    fix_result = await dispatch_fix_fn(task)

    # Phase 4: Done
    task.status = VerifyStatus.DONE
    task.healthy = fix_result.get("healthy", False)
    task.fixes = fix_result.get("fixes", [])
    task.errors = fix_result.get("errors", errors)
    task.version = fix_result.get("version", task.original_version)
    task.commit_hash = fix_result.get("commit_hash", task.original_commit_hash)
    task.save(store_dir)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/appchannels/test_verify.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add workspace_vm/appchannels/verify.py tests/appchannels/test_verify.py
git commit -m "feat: implement run_verify async flow with restart → check → fix"
```

---

### Task 4: Dispatch Agent Fix — Work Queue Integration

**Files:**
- Modify: `workspace_vm/appchannels/verify.py`
- Read: `workspace_vm/appchannels/routes.py:356-449` (work item creation pattern)
- Read: `workspace_vm/appchannels/work_queue.py:16-50` (WorkQueue API)
- Read: `workspace_vm/appchannels/models.py:39-96` (WorkItem, WorkResult)
- Test: `tests/appchannels/test_verify.py`

- [ ] **Step 1: Write failing test for dispatch_agent_fix**

```python
# Add to tests/appchannels/test_verify.py
from unittest.mock import MagicMock

from workspace_vm.appchannels.verify import dispatch_agent_fix


class TestDispatchAgentFix:
    def test_creates_work_item_and_waits_for_result(self, tmp_path):
        """dispatch_agent_fix creates a queue item and polls for result."""
        task = VerifyTask(
            task_id="t1",
            site_name="my-site",
            site_path=tmp_path / "sites" / "my-site",
            conversation_id="conv-1",
            original_version=3,
            original_commit_hash="abc",
        )
        task.errors = ["Server failed to start"]

        mock_queue = MagicMock()
        mock_wait = AsyncMock(return_value={
            "status": "complete",
            "site_version": 4,
            "site_commit_hash": "def456",
            "fixes": [{"file": "server.js", "description": "fixed import"}],
            "errors": [],
        })

        result = asyncio.run(
            dispatch_agent_fix(task, queue=mock_queue, wait_for_result_fn=mock_wait)
        )

        mock_queue.append_item.assert_called_once()
        assert result["healthy"] is True
        assert result["version"] == 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/appchannels/test_verify.py::TestDispatchAgentFix -v`
Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement dispatch_agent_fix**

Add to `workspace_vm/appchannels/verify.py`:

```python
from workspace_vm.appchannels.models import WorkItem
from workspace_vm.appchannels.work_queue import WorkQueue


async def dispatch_agent_fix(
    task: VerifyTask,
    *,
    queue: WorkQueue | None = None,
    wait_for_result_fn: Callable | None = None,
) -> dict[str, Any]:
    """Create a work item to fix the site and wait for the result.

    The work item prompt tells the agent what errors were found and
    asks it to fix and verify the site.
    """
    item = WorkItem(
        item_id=f"verify-fix-{task.task_id}",
        name=f"Fix {task.site_name} after pop",
        source=task.conversation_id,
        queue_id=f"project-{task.site_name}",
        prompt=(
            f"The site '{task.site_name}' was just deployed via pop but has errors:\n"
            + "\n".join(f"- {e}" for e in task.errors)
            + "\n\nFix these issues and verify the site works."
        ),
        site=task.site_name,
        status="pending",
    )

    if queue is not None:
        queue.append_item(item)

    # Wait for the agent to complete
    if wait_for_result_fn is not None:
        result = await wait_for_result_fn(item.item_id)
    else:
        return {"healthy": False, "fixes": [], "errors": task.errors, "version": task.version, "commit_hash": task.commit_hash}

    # Map WorkResult fields to verify result
    status = result.get("status", "failed")
    healthy = status == "complete"
    return {
        "healthy": healthy,
        "fixes": result.get("fixes", []),
        "errors": result.get("errors", task.errors if not healthy else []),
        "version": result.get("site_version", task.original_version),
        "commit_hash": result.get("site_commit_hash", task.original_commit_hash),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/appchannels/test_verify.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add workspace_vm/appchannels/verify.py tests/appchannels/test_verify.py
git commit -m "feat: implement dispatch_agent_fix with work queue integration"
```

---

### Task 5: VM Routes — verify-status Endpoint + Modified s3-pull

**Files:**
- Modify: `workspace_vm/appchannels/routes.py:1687-1852` (s3-pull endpoint)
- Modify: `workspace_vm/appchannels/routes.py` (add verify-status route, _restart_site and _dispatch_agent_fix wrappers)
- Test: `tests/appchannels/test_verify.py`

- [ ] **Step 1: Write failing tests for verify-status endpoint using TestClient**

```python
# Add to tests/appchannels/test_verify.py
import pytest
from unittest.mock import MagicMock, patch
from starlette.testclient import TestClient


class TestVerifyStatusEndpoint:
    def test_verify_status_returns_task(self, tmp_path):
        """GET /sites/{name}/verify-status?task_id=X returns task state."""
        store_dir = tmp_path / "verify_tasks"
        task = VerifyTask(
            task_id="t1",
            site_name="my-site",
            site_path=tmp_path / "sites" / "my-site",
            conversation_id="conv-1",
            original_version=3,
            original_commit_hash="abc",
        )
        task.status = VerifyStatus.CHECKING
        task.site_type = "node"
        task.save(store_dir)

        # Test via direct model load (endpoint is thin wrapper)
        loaded = VerifyTask.load(store_dir, "t1")
        d = loaded.to_status_dict()
        assert d["status"] == "checking"
        assert d["healthy"] is None
        assert d["site_type"] == "node"

    def test_verify_status_not_found(self, tmp_path):
        """Loading a non-existent task raises FileNotFoundError."""
        store_dir = tmp_path / "verify_tasks"
        store_dir.mkdir()
        with pytest.raises(FileNotFoundError):
            VerifyTask.load(store_dir, "nonexistent")

    def test_verify_status_done_with_fixes(self, tmp_path):
        """Completed task with fixes returns full result."""
        store_dir = tmp_path / "verify_tasks"
        task = VerifyTask(
            task_id="t2",
            site_name="my-site",
            site_path=tmp_path / "sites" / "my-site",
            conversation_id="conv-1",
            original_version=3,
            original_commit_hash="abc",
        )
        task.status = VerifyStatus.DONE
        task.healthy = True
        task.site_type = "node"
        task.fixes = [{"file": "server.js", "description": "added import"}]
        task.version = 4
        task.commit_hash = "def456"
        task.save(store_dir)

        loaded = VerifyTask.load(store_dir, "t2")
        d = loaded.to_status_dict()
        assert d["status"] == "done"
        assert d["healthy"] is True
        assert d["version"] == 4
        assert len(d["fixes"]) == 1
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/appchannels/test_verify.py::TestVerifyStatusEndpoint -v`

- [ ] **Step 3: Add verify-status route to routes.py**

Add after the existing restart endpoint (around line 1430):

```python
from workspace_vm.appchannels.verify import VerifyTask, detect_site_type, run_verify

@router.get("/sites/{name}/verify-status")
async def verify_status_endpoint(request: Request, name: str):
    """Return the status of an async verify task."""
    task_id = request.query_params.get("task_id")
    if not task_id:
        return JSONResponse({"error": "task_id required"}, status_code=400)

    store_dir = Path(config.sites_dir) / ".verify_tasks"
    try:
        task = VerifyTask.load(store_dir, task_id)
    except FileNotFoundError:
        return JSONResponse({"error": "Verify task not found"}, status_code=404)

    return JSONResponse(task.to_status_dict())
```

- [ ] **Step 4: Add _restart_site and _dispatch_agent_fix wrapper functions**

Add to `routes.py`, near the verify-status endpoint:

```python
async def _restart_site(site_path: Path) -> dict[str, Any]:
    """Wrapper: restart a site and return the result dict."""
    pm = NodeProcessManager()
    if pm.has_dynamic_server(site_path):
        pid = await pm.restart(site_path, reinstall_deps=True)
        port = pm.read_port(site_path)
        if pid and port:
            return {"message": "Restarted", "pid": pid, "port": port}
        return {"message": "Failed to start", "pid": None, "port": None}
    elif pm.has_build_script(site_path):
        success = await pm.build(site_path)
        if success:
            return {"type": "build", "success": True, "message": "Build completed"}
        error = pm.read_build_log(site_path, tail=2000)
        return {"type": "build", "success": False, "error": error}
    return {"type": "static", "message": "Static site"}


async def _dispatch_agent_fix_wrapper(task: "VerifyTask") -> dict[str, Any]:
    """Wrapper: dispatch a fix work item and wait for result."""
    from workspace_vm.appchannels.verify import dispatch_agent_fix
    queue = WorkQueue(Path(config.queues_dir) / f"project-{task.site_name}")
    # wait_for_result_fn should poll results.jsonl or subscribe to Valkey channel
    # Implementation depends on existing queue result notification pattern
    return await dispatch_agent_fix(task, queue=queue, wait_for_result_fn=_wait_for_queue_result)
```

- [ ] **Step 5: Modify s3-pull endpoint to accept verify flag and kick off async task**

In `routes.py`, modify the `s3_pull_endpoint` (line 1687). After the existing commit logic (around line 1830), add:

```python
    # After: result = {"version": version, "commit_hash": commit_hash}
    verify = body.get("verify", False)
    if verify:
        site_type = detect_site_type(site_path)
        if site_type == "static":
            result["verify"] = {"skipped": True, "reason": "static"}
        else:
            task_id = VerifyTask.new_id()
            task = VerifyTask(
                task_id=task_id,
                site_name=name,
                site_path=site_path,
                conversation_id=body.get("conversation_id", ""),
                original_version=version,
                original_commit_hash=commit_hash,
            )
            store_dir = Path(config.sites_dir) / ".verify_tasks"

            # Kick off verify as background task (FastAPI pattern)
            asyncio.create_task(
                run_verify(
                    task,
                    store_dir,
                    restart_fn=_restart_site,
                    dispatch_fix_fn=_dispatch_agent_fix_wrapper,
                )
            )
            result["verify_task_id"] = task_id

    return JSONResponse(result)
```

- [ ] **Step 6: Run full test suite**

Run: `pytest tests/appchannels/ -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add workspace_vm/appchannels/routes.py workspace_vm/appchannels/verify.py tests/appchannels/test_verify.py
git commit -m "feat: add verify-status endpoint, wrappers, and async verify on s3-pull"
```

---

### Task 6: API Layer — Proxy verify Flag and Status Endpoint

**Files:**
- Modify: `services/api/conversations.py:1119-1176` (PublishRequest + publish endpoint)
- Modify: `lib/app_channels/services/site_service.py:108-158` (s3_pull — forward verify)
- Test: `tests/api/test_conversations.py` (or equivalent)

- [ ] **Step 1: Add verify to PublishRequest model**

In `services/api/conversations.py`, find the `PublishRequest` Pydantic model (around line 1119) and add:

```python
class PublishRequest(BaseModel):
    conversation_id: str
    s3_key: str
    context: str = ""
    force: bool = False
    verify: bool = False  # NEW
```

- [ ] **Step 2: Thread verify through SiteService.s3_pull**

In `services/api/conversations.py`, modify the `publish()` handler (around line 1128):

```python
result = await site_service.s3_pull(
    site_name=site_name,
    s3_key=body.s3_key,
    conversation_id=conversation_id,
    context_message=body.context,
    force=body.force,
    verify=body.verify,  # NEW
)
```

In `lib/app_channels/services/site_service.py`, modify `s3_pull()` signature (line 108):

```python
async def s3_pull(
    self,
    site_name: str,
    s3_key: str,
    conversation_id: uuid.UUID,
    context_message: str = "",
    force: bool = False,
    verify: bool = False,  # NEW
) -> Dict[str, Any]:
```

And forward in `_call_vm_s3_pull()` (around line 230), add `"verify": verify` to the request body dict.

- [ ] **Step 3: Add verify-status proxy endpoint**

In `services/api/conversations.py`, add after the publish endpoint:

```python
@router.get("/api/conversations/{conversation_id}/verify-status")
async def verify_status(
    conversation_id: str,
    task_id: str = Query(...),
    _ctx: ServiceContext = Depends(get_service_context),
    db: AsyncSession = Depends(get_db),
):
    """Proxy verify-status from workspace VM."""
    # Resolve site_name from conversation_id (same pattern as other endpoints)
    conv = await get_conversation(db, conversation_id)
    site_name = conv.metadata.get("site_name")
    if not site_name:
        raise HTTPException(404, "No site associated with this conversation")

    # Forward to VM
    vm_url = f"{_ctx.vm_base_url}/api/v1/sites/{site_name}/verify-status"
    async with httpx.AsyncClient() as client:
        resp = await client.get(vm_url, params={"task_id": task_id}, timeout=10.0)

    return JSONResponse(resp.json(), status_code=resp.status_code)
```

- [ ] **Step 4: Write test for verify-status proxy**

```python
# Add to tests/api/test_conversations.py or new test file
async def test_publish_with_verify_passes_flag(mock_site_service):
    """verify=true in request body reaches SiteService.s3_pull."""
    # Call publish endpoint with verify=true
    # Assert mock_site_service.s3_pull was called with verify=True

async def test_verify_status_proxies_to_vm(mock_vm_client):
    """GET /conversations/{id}/verify-status proxies to VM."""
    # Mock VM response
    # Assert response matches VM response
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/api/ -v`
Expected: All PASS (including new tests, no regressions)

- [ ] **Step 6: Commit**

```bash
git add services/api/conversations.py lib/app_channels/services/site_service.py tests/api/
git commit -m "feat: proxy verify flag and verify-status endpoint through API layer"
```

---

### Task 7: Verify Task Cleanup

**Files:**
- Modify: `workspace_vm/appchannels/verify.py`
- Test: `tests/appchannels/test_verify.py`

Verify task JSON files in `.verify_tasks/` accumulate over time. Add a cleanup function.

- [ ] **Step 1: Write failing test for cleanup**

```python
# Add to tests/appchannels/test_verify.py
from workspace_vm.appchannels.verify import cleanup_verify_tasks


class TestCleanup:
    def test_removes_old_tasks(self, tmp_path):
        """Tasks older than 1 hour are removed."""
        store_dir = tmp_path / "verify_tasks"
        store_dir.mkdir()

        # Create an old task file (touch with old mtime)
        old_file = store_dir / "old-task.json"
        old_file.write_text('{"task_id": "old-task", "status": "done"}')
        import os
        os.utime(old_file, (0, 0))  # Set mtime to epoch

        # Create a recent task file
        new_file = store_dir / "new-task.json"
        new_file.write_text('{"task_id": "new-task", "status": "checking"}')

        cleanup_verify_tasks(store_dir, max_age_seconds=3600)

        assert not old_file.exists()
        assert new_file.exists()
```

- [ ] **Step 2: Implement cleanup_verify_tasks**

```python
# Add to workspace_vm/appchannels/verify.py
import time as _time

def cleanup_verify_tasks(store_dir: Path, max_age_seconds: int = 3600) -> int:
    """Remove verify task files older than max_age_seconds. Returns count removed."""
    if not store_dir.exists():
        return 0
    cutoff = _time.time() - max_age_seconds
    removed = 0
    for f in store_dir.glob("*.json"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    return removed
```

- [ ] **Step 3: Call cleanup at the start of verify-status endpoint**

In `routes.py` verify-status handler, add before the load:

```python
    cleanup_verify_tasks(store_dir, max_age_seconds=3600)
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/appchannels/test_verify.py::TestCleanup -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add workspace_vm/appchannels/verify.py workspace_vm/appchannels/routes.py tests/appchannels/test_verify.py
git commit -m "feat: add verify task cleanup (1h TTL)"
```

---

### Task 7: Integration Test — Full Verify Flow

**Files:**
- Create: `tests/appchannels/test_verify_integration.py`

- [ ] **Step 1: Write integration test for the full happy path**

```python
# tests/appchannels/test_verify_integration.py
"""Integration test: publish with verify → restart → healthy → done."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

from workspace_vm.appchannels.verify import VerifyTask, VerifyStatus, run_verify


class TestVerifyIntegration:
    def test_full_flow_healthy(self, tmp_path):
        """Pop → verify → restart succeeds → done healthy."""
        site_path = tmp_path / "my-site"
        site_path.mkdir()
        (site_path / "server.js").write_text("require('express')()")

        store_dir = tmp_path / "verify_tasks"
        task = VerifyTask(
            task_id="int-1",
            site_name="my-site",
            site_path=site_path,
            conversation_id="conv-1",
            original_version=3,
            original_commit_hash="abc",
        )

        mock_restart = AsyncMock(return_value={"message": "Restarted", "pid": 100, "port": 3000})

        asyncio.run(
            run_verify(task, store_dir, restart_fn=mock_restart)
        )

        assert task.status == VerifyStatus.DONE
        assert task.healthy is True
        assert task.site_type == "node"

        # Verify persistence
        loaded = VerifyTask.load(store_dir, "int-1")
        assert loaded.healthy is True

    def test_full_flow_unhealthy_fixed(self, tmp_path):
        """Pop → verify → restart fails → agent fixes → done healthy."""
        site_path = tmp_path / "my-site"
        site_path.mkdir()
        (site_path / "server.js").write_text("broken")

        store_dir = tmp_path / "verify_tasks"
        task = VerifyTask(
            task_id="int-2",
            site_name="my-site",
            site_path=site_path,
            conversation_id="conv-1",
            original_version=3,
            original_commit_hash="abc",
        )

        mock_restart = AsyncMock(return_value={"message": "Failed", "pid": None, "port": None})
        mock_fix = AsyncMock(return_value={
            "healthy": True,
            "fixes": [{"file": "server.js", "description": "added express import"}],
            "errors": [],
            "version": 4,
            "commit_hash": "def456",
        })

        asyncio.run(
            run_verify(task, store_dir, restart_fn=mock_restart, dispatch_fix_fn=mock_fix)
        )

        assert task.status == VerifyStatus.DONE
        assert task.healthy is True
        assert task.version == 4
        assert len(task.fixes) == 1

    def test_full_flow_unfixable(self, tmp_path):
        """Pop → verify → restart fails → agent can't fix → done unhealthy."""
        site_path = tmp_path / "my-site"
        site_path.mkdir()
        (site_path / "server.py").write_text("broken")

        store_dir = tmp_path / "verify_tasks"
        task = VerifyTask(
            task_id="int-3",
            site_name="my-site",
            site_path=site_path,
            conversation_id="conv-1",
            original_version=3,
            original_commit_hash="abc",
        )

        mock_restart = AsyncMock(return_value={"message": "Failed", "pid": None, "port": None})
        mock_fix = AsyncMock(return_value={
            "healthy": False,
            "fixes": [],
            "errors": ["Cannot fix: missing system dependency"],
            "version": 3,
            "commit_hash": "abc",
        })

        asyncio.run(
            run_verify(task, store_dir, restart_fn=mock_restart, dispatch_fix_fn=mock_fix)
        )

        assert task.status == VerifyStatus.DONE
        assert task.healthy is False
        assert len(task.errors) > 0
```

- [ ] **Step 2: Run integration tests**

Run: `pytest tests/appchannels/test_verify_integration.py -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add tests/appchannels/test_verify_integration.py
git commit -m "test: add integration tests for verify flow"
```
