"""Tests for pop command and deploy operations."""

from __future__ import annotations

import json
import os
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from popcorn_cli.cli import build_parser
from popcorn_core import operations
from popcorn_core.archive import create_tarball
from popcorn_core.config import Config, Profile
from popcorn_core.errors import APIError, PopcornError
from popcorn_core.local_state import LocalState, Target, save_local_state

# Shared mock response for site-status (appended to post side_effect lists)
_SITE_STATUS = {"url": "https://pop-test.popcorn.ai", "site_name": "pop-test", "version": 1}

# Default workspace for tests (matches conftest.py profile)
_WS_ID = "ws-0000-0000-0000-000000000000"
_WS_NAME = "Test Workspace"


def _write_v2_local(tmp_path: Path, conversation_id: str, site_name: str) -> None:
    """Write a v2 .popcorn.local.json for tests."""
    state = LocalState(
        default_target=site_name,
        targets={
            site_name: Target(
                workspace_id=_WS_ID,
                workspace_name=_WS_NAME,
                conversation_id=conversation_id,
                site_name=site_name,
                profile="default",
            )
        },
    )
    save_local_state(state, tmp_path / ".popcorn.local.json")


def _mock_load_config():
    """Return a Config with a default profile matching the test workspace."""
    cfg = Config(default_profile="default")
    cfg.profiles["default"] = Profile(
        workspace_id=_WS_ID,
        workspace_name=_WS_NAME,
    )
    return cfg


class TestDeployCreate:
    def test_deploy_create(self, mock_client):
        mock_client.post.return_value = {
            "id": "conv-1",
            "name": "my-site",
            "type": "workspace_channel",
        }
        result = operations.deploy_create(mock_client, "my-site")
        mock_client.post.assert_called_once_with(
            "/api/conversations/create",
            data={
                "name": "my-site",
                "conversation_type": "workspace_channel",
                "site_name": "my-site",
            },
        )
        assert result["name"] == "my-site"
        assert result["id"] == "conv-1"


class TestDeployPresign:
    def test_deploy_presign(self, mock_client):
        mock_client.post.return_value = {
            "upload_url": "https://s3.example.com/upload",
            "upload_fields": {"key": "abc"},
            "s3_key": "ws/sites/my-site/versions/123.tar.gz",
        }
        result = operations.deploy_presign(mock_client, "conv-1")
        mock_client.post.assert_called_once_with(
            "/api/conversations/presigned-url",
            data={"conversation_id": "conv-1", "method": "PUT"},
        )
        assert result["upload_url"] == "https://s3.example.com/upload"
        assert result["s3_key"].endswith(".tar.gz")


class TestDeployPublish:
    def test_deploy_publish(self, mock_client):
        mock_client.post.return_value = {
            "conversation_id": "conv-1",
            "site_name": "my-site",
            "version": 1,
            "commit_hash": "abc123",
        }
        result = operations.deploy_publish(mock_client, "conv-1", "s3-key-1")
        mock_client.post.assert_called_once_with(
            "/api/conversations/publish",
            data={"conversation_id": "conv-1", "s3_key": "s3-key-1"},
        )
        assert result["version"] == 1
        assert result["commit_hash"] == "abc123"

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
        operations.deploy_publish(mock_client, "conv-1", "s3-key-1")
        call_data = mock_client.post.call_args[1]["data"]
        assert "verify" not in call_data

    def test_deploy_publish_with_context(self, mock_client):
        mock_client.post.return_value = {
            "conversation_id": "conv-1",
            "site_name": "my-site",
            "version": 2,
            "commit_hash": "def456",
        }
        operations.deploy_publish(mock_client, "conv-1", "s3-key-1", context="fix login")
        mock_client.post.assert_called_once_with(
            "/api/conversations/publish",
            data={
                "conversation_id": "conv-1",
                "s3_key": "s3-key-1",
                "context": "fix login",
            },
        )


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
        result = operations.deploy_verify_status(mock_client, "conv-1", "task-uuid", "my-site")
        mock_client.get.assert_called_once_with(
            "/api/conversations/verify-status",
            {"task_id": "task-uuid", "site_name": "my-site", "conversation": "conv-1"},
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
        result = operations.deploy_verify_status(mock_client, "conv-1", "task-uuid", "my-site")
        assert result["status"] == "fixing"
        assert result["healthy"] is None


class TestDeployUpload:
    def test_deploy_upload(self, tmp_path):
        tarball = tmp_path / "test.tar.gz"
        tarball.write_bytes(b"fake-tarball-data")

        mock_resp = MagicMock()
        mock_resp.status_code = 204

        with patch("popcorn_core.operations.httpx.post", return_value=mock_resp) as mock_post:
            operations.deploy_upload(
                "https://s3.example.com/upload",
                {"key": "abc", "policy": "xyz"},
                str(tarball),
            )
            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args
            assert call_kwargs[0][0] == "https://s3.example.com/upload"
            assert call_kwargs[1]["data"] == {"key": "abc", "policy": "xyz"}
            assert call_kwargs[1]["timeout"] == 120.0
            files = call_kwargs[1]["files"]
            assert files["file"][0] == "push.tar.gz"
            assert files["file"][2] == "application/gzip"

    def test_deploy_upload_missing_file(self, tmp_path):
        with pytest.raises(PopcornError, match="Tarball not found"):
            operations.deploy_upload(
                "https://s3.example.com/upload",
                {"key": "abc"},
                str(tmp_path / "nonexistent.tar.gz"),
            )

    def test_deploy_upload_http_error(self, tmp_path):
        tarball = tmp_path / "test.tar.gz"
        tarball.write_bytes(b"fake-tarball-data")

        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "Forbidden"

        with (
            patch("popcorn_core.operations.httpx.post", return_value=mock_resp),
            pytest.raises(APIError, match="Deploy upload failed: HTTP 403"),
        ):
            operations.deploy_upload(
                "https://s3.example.com/upload",
                {"key": "abc"},
                str(tarball),
            )

    def test_deploy_upload_timeout(self, tmp_path):
        import httpx

        tarball = tmp_path / "test.tar.gz"
        tarball.write_bytes(b"fake-tarball-data")

        with (
            patch(
                "popcorn_core.operations.httpx.post",
                side_effect=httpx.TimeoutException("timed out"),
            ),
            pytest.raises(APIError, match="Deploy upload timed out"),
        ):
            operations.deploy_upload(
                "https://s3.example.com/upload",
                {"key": "abc"},
                str(tarball),
            )


class TestCreateTarball:
    def test_create_tarball_git_repo(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "index.html").write_text("<html>hello</html>")
        (tmp_path / "app.js").write_text("console.log('hi')")
        (tmp_path / ".popcorn.local.json").write_text("{}")

        with (
            patch("popcorn_core.archive._is_git_repo", return_value=True),
            patch(
                "popcorn_core.archive.subprocess.check_output",
                return_value="index.html\napp.js\n.popcorn.local.json\n",
            ),
        ):
            tarball = create_tarball()
            try:
                assert tarball.endswith(".tar.gz")
                with tarfile.open(tarball, "r:gz") as tar:
                    names = tar.getnames()
                    assert "index.html" in names
                    assert "app.js" in names
                    assert ".popcorn.local.json" not in names
            finally:
                Path(tarball).unlink(missing_ok=True)

    def test_create_tarball_not_git(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "index.html").write_text("<html>hello</html>")
        (tmp_path / ".popcorn.local.json").write_text("{}")
        (tmp_path / ".git").mkdir()
        (tmp_path / "node_modules").mkdir()

        with patch("popcorn_core.archive._is_git_repo", return_value=False):
            tarball = create_tarball()
            try:
                with tarfile.open(tarball, "r:gz") as tar:
                    names = tar.getnames()
                    assert "index.html" in names
                    assert ".git" not in names
                    assert "node_modules" not in names
                    assert ".popcorn.local.json" not in names
            finally:
                Path(tarball).unlink(missing_ok=True)

    def test_create_tarball_popcornignore_git(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "index.html").write_text("<html>hello</html>")
        (tmp_path / "app.js").write_text("console.log('hi')")
        (tmp_path / "debug.log").write_text("log data")
        (tmp_path / ".popcornignore").write_text("*.log\n")

        with (
            patch("popcorn_core.archive._is_git_repo", return_value=True),
            patch(
                "popcorn_core.archive.subprocess.check_output",
                return_value="index.html\napp.js\ndebug.log\n.popcornignore\n",
            ),
        ):
            tarball = create_tarball()
            try:
                with tarfile.open(tarball, "r:gz") as tar:
                    names = tar.getnames()
                    assert "index.html" in names
                    assert "app.js" in names
                    assert "debug.log" not in names
                    assert ".popcornignore" not in names
            finally:
                Path(tarball).unlink(missing_ok=True)

    def test_create_tarball_popcornignore_non_git(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "index.html").write_text("<html>hello</html>")
        (tmp_path / "style.css").write_text("body {}")
        (tmp_path / "debug.log").write_text("log data")
        (tmp_path / ".popcornignore").write_text("*.log\n")

        with patch("popcorn_core.archive._is_git_repo", return_value=False):
            tarball = create_tarball()
            try:
                with tarfile.open(tarball, "r:gz") as tar:
                    names = tar.getnames()
                    assert "index.html" in names
                    assert "style.css" in names
                    assert "debug.log" not in names
                    assert ".popcornignore" not in names
            finally:
                Path(tarball).unlink(missing_ok=True)

    def test_create_tarball_no_popcornignore(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "index.html").write_text("<html>hello</html>")
        (tmp_path / "data.log").write_text("log data")

        with patch("popcorn_core.archive._is_git_repo", return_value=False):
            tarball = create_tarball()
            try:
                with tarfile.open(tarball, "r:gz") as tar:
                    names = tar.getnames()
                    assert "index.html" in names
                    assert "data.log" in names
            finally:
                Path(tarball).unlink(missing_ok=True)

    def test_popcornignore_excludes_itself(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "index.html").write_text("<html>hello</html>")
        (tmp_path / ".popcornignore").write_text("# empty ignore\n")

        # Test in git mode
        with (
            patch("popcorn_core.archive._is_git_repo", return_value=True),
            patch(
                "popcorn_core.archive.subprocess.check_output",
                return_value="index.html\n.popcornignore\n",
            ),
        ):
            tarball = create_tarball()
            try:
                with tarfile.open(tarball, "r:gz") as tar:
                    names = tar.getnames()
                    assert "index.html" in names
                    assert ".popcornignore" not in names
            finally:
                Path(tarball).unlink(missing_ok=True)

        # Test in non-git mode
        with patch("popcorn_core.archive._is_git_repo", return_value=False):
            tarball = create_tarball()
            try:
                with tarfile.open(tarball, "r:gz") as tar:
                    names = tar.getnames()
                    assert "index.html" in names
                    assert ".popcornignore" not in names
            finally:
                Path(tarball).unlink(missing_ok=True)

    def test_create_tarball_non_git_recursive(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "index.html").write_text("<html>hello</html>")
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "app.js").write_text("console.log('hi')")
        deep = sub / "utils"
        deep.mkdir()
        (deep / "helper.js").write_text("export default {}")
        (deep / "debug.log").write_text("log data")
        (tmp_path / ".popcornignore").write_text("**/*.log\n")

        with patch("popcorn_core.archive._is_git_repo", return_value=False):
            tarball = create_tarball()
            try:
                with tarfile.open(tarball, "r:gz") as tar:
                    names = tar.getnames()
                    assert "index.html" in names
                    assert os.path.join("src", "app.js") in names
                    assert os.path.join("src", "utils", "helper.js") in names
                    assert os.path.join("src", "utils", "debug.log") not in names
                    assert ".popcornignore" not in names
            finally:
                Path(tarball).unlink(missing_ok=True)

    def test_create_tarball_git_error(self, tmp_path, monkeypatch):
        import subprocess

        monkeypatch.chdir(tmp_path)

        with (
            patch("popcorn_core.archive._is_git_repo", return_value=True),
            patch(
                "popcorn_core.archive.subprocess.check_output",
                side_effect=subprocess.CalledProcessError(1, "git"),
            ),
            pytest.raises(PopcornError, match="Failed to list git-tracked files"),
        ):
            create_tarball()


class TestPop:
    @pytest.fixture()
    def pop_args(self):
        args = MagicMock()
        args.name = None
        args.context = ""
        args.json = False
        args.env = None
        args.workspace = None
        args.force = False
        args.target = None
        return args

    def test_pop_full_flow(self, mock_client, tmp_path, monkeypatch, pop_args):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "index.html").write_text("<html>hello</html>")
        (tmp_path / ".gitignore").write_text("node_modules\n")

        mock_client.post.side_effect = [
            {
                "ok": True,
                "conversation": {"id": "conv-1", "name": "pop-test", "type": "workspace_channel"},
            },
            {
                "upload_url": "https://s3.example.com/upload",
                "upload_fields": {"key": "abc"},
                "s3_key": "ws/sites/pop-test/versions/123.tar.gz",
            },
            {
                "conversation_id": "conv-1",
                "site_name": "pop-test",
                "version": 1,
                "commit_hash": "abc123",
            },
            _SITE_STATUS,
        ]

        with (
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_core.operations.deploy_upload"),
            patch("os.unlink"),
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
            patch("popcorn_cli.cli.load_config", return_value=_mock_load_config()),
        ):
            from popcorn_cli.cli import cmd_pop

            cmd_pop(pop_args)

        local = json.loads((tmp_path / ".popcorn.local.json").read_text())
        assert local["version"] == 2
        assert "pop-test" in local["targets"]
        assert local["targets"]["pop-test"]["conversation_id"] == "conv-1"

        gitignore = (tmp_path / ".gitignore").read_text()
        assert ".popcorn.local.json" in gitignore

    def test_pop_json_includes_site_url(self, mock_client, tmp_path, monkeypatch, pop_args, capsys):
        """JSON output includes site_url derived from publish response subdomain."""
        monkeypatch.chdir(tmp_path)
        pop_args.json = True
        (tmp_path / ".gitignore").write_text("")

        mock_client.post.side_effect = [
            {
                "ok": True,
                "conversation": {"id": "conv-1", "name": "pop-test", "type": "workspace_channel"},
            },
            {
                "upload_url": "https://s3.example.com/upload",
                "upload_fields": {"key": "abc"},
                "s3_key": "key.tar.gz",
            },
            {
                "conversation_id": "conv-1",
                "site_name": "pop-test",
                "version": 1,
                "commit_hash": "abc123",
                "subdomain": "pop-test--my-ws",
            },
        ]

        with (
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_core.operations.deploy_upload"),
            patch("os.unlink"),
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
            patch("popcorn_cli.cli.load_config", return_value=_mock_load_config()),
        ):
            from popcorn_cli.cli import cmd_pop

            cmd_pop(pop_args)

        output = json.loads(capsys.readouterr().out)
        assert output["ok"] is True
        assert output["data"]["site_url"] == "https://pop-test--my-ws.popcorn.ing"

    def test_pop_existing_site(self, mock_client, tmp_path, monkeypatch, pop_args):
        monkeypatch.chdir(tmp_path)
        _write_v2_local(tmp_path, "conv-existing", "pop-test")

        mock_client.get.side_effect = [
            {
                "conversation": {"id": "conv-existing", "site": {"name": "pop-test"}},
            },
            _SITE_STATUS,
        ]
        mock_client.post.side_effect = [
            {
                "upload_url": "https://s3.example.com/upload",
                "upload_fields": {"key": "abc"},
                "s3_key": "ws/sites/pop-test/versions/456.tar.gz",
            },
            {
                "conversation_id": "conv-existing",
                "site_name": "pop-test",
                "version": 2,
                "commit_hash": "def456",
            },
        ]

        with (
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_core.operations.deploy_upload"),
            patch("os.unlink"),
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
            patch("popcorn_cli.cli.load_config", return_value=_mock_load_config()),
        ):
            from popcorn_cli.cli import cmd_pop

            cmd_pop(pop_args)

        assert mock_client.post.call_count == 2  # presign + publish

    def test_pop_409_conflict_retries_with_suffix(
        self, mock_client, tmp_path, monkeypatch, pop_args
    ):
        monkeypatch.chdir(tmp_path)

        # All creates return 409 — should try original + 5 suffixed names then fail
        mock_client.post.side_effect = APIError("Site already exists", status_code=409)

        with (
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("os.unlink"),
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
        ):
            from popcorn_cli.cli import cmd_pop

            with pytest.raises(PopcornError, match="Could not find available name"):
                cmd_pop(pop_args)

        # 1 original + 5 retries = 6 create calls
        assert mock_client.post.call_count == 6

    def test_pop_409_conflict_succeeds_on_retry(self, mock_client, tmp_path, monkeypatch, pop_args):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".gitignore").write_text("")

        # First create 409s, second succeeds, then presign + publish + site-status
        mock_client.post.side_effect = [
            APIError("Site already exists", status_code=409),
            {
                "ok": True,
                "conversation": {
                    "id": "conv-1",
                    "name": "pop-test-abcd",
                    "type": "workspace_channel",
                },
            },
            {
                "upload_url": "https://s3.example.com/upload",
                "upload_fields": {"key": "abc"},
                "s3_key": "ws/sites/pop-test-abcd/versions/1.tar.gz",
            },
            {
                "conversation_id": "conv-1",
                "site_name": "pop-test-abcd",
                "version": 1,
                "commit_hash": "abc123",
            },
            _SITE_STATUS,
        ]

        with (
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_core.operations.deploy_upload"),
            patch("os.unlink"),
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
            patch("popcorn_cli.cli.load_config", return_value=_mock_load_config()),
        ):
            from popcorn_cli.cli import cmd_pop

            cmd_pop(pop_args)

        local = json.loads((tmp_path / ".popcorn.local.json").read_text())
        assert local["targets"]["pop-test-abcd"]["conversation_id"] == "conv-1"

    def test_pop_name_flag(self, mock_client, tmp_path, monkeypatch, pop_args):
        monkeypatch.chdir(tmp_path)
        pop_args.name = "custom-site"

        mock_client.post.side_effect = [
            {
                "ok": True,
                "conversation": {
                    "id": "conv-1",
                    "name": "custom-site",
                    "type": "workspace_channel",
                },
            },
            {
                "upload_url": "https://s3.example.com/upload",
                "upload_fields": {"key": "abc"},
                "s3_key": "ws/sites/custom-site/versions/1.tar.gz",
            },
            {
                "conversation_id": "conv-1",
                "site_name": "custom-site",
                "version": 1,
                "commit_hash": "abc",
            },
            _SITE_STATUS,
        ]

        with (
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_core.operations.deploy_upload"),
            patch("os.unlink"),
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
            patch("popcorn_cli.cli.load_config", return_value=_mock_load_config()),
        ):
            from popcorn_cli.cli import cmd_pop

            cmd_pop(pop_args)

        mock_client.post.assert_any_call(
            "/api/conversations/create",
            data={
                "name": "custom-site",
                "conversation_type": "workspace_channel",
                "site_name": "custom-site",
            },
        )

    def test_pop_stale_config_force(self, mock_client, tmp_path, monkeypatch, pop_args):
        """--force auto-deletes stale config and creates new channel."""
        monkeypatch.chdir(tmp_path)
        pop_args.force = True
        _write_v2_local(tmp_path, "dead-conv", "old-site")
        (tmp_path / ".gitignore").write_text("")

        # _validate_channel returns False (stale), then fresh create flow
        mock_client.get.side_effect = APIError("Not found", status_code=404)
        mock_client.post.side_effect = [
            {
                "ok": True,
                "conversation": {"id": "conv-new", "name": "pop-test", "type": "workspace_channel"},
            },
            {
                "upload_url": "https://s3.example.com/upload",
                "upload_fields": {"key": "abc"},
                "s3_key": "key.tar.gz",
            },
            {
                "conversation_id": "conv-new",
                "site_name": "pop-test",
                "version": 1,
                "commit_hash": "abc123",
            },
            _SITE_STATUS,
        ]

        with (
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_core.operations.deploy_upload"),
            patch("os.unlink"),
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
            patch("popcorn_cli.cli.load_config", return_value=_mock_load_config()),
        ):
            from popcorn_cli.cli import cmd_pop

            cmd_pop(pop_args)

        local = json.loads((tmp_path / ".popcorn.local.json").read_text())
        assert local["targets"]["pop-test"]["conversation_id"] == "conv-new"

    def test_pop_stale_config_no_force_auto_recreates(
        self, mock_client, tmp_path, monkeypatch, pop_args
    ):
        """Non-interactive + stale config auto-recreates like --force."""
        monkeypatch.chdir(tmp_path)
        _write_v2_local(tmp_path, "dead-conv", "old-site")
        (tmp_path / ".gitignore").write_text("")

        mock_client.get.side_effect = APIError("Not found", status_code=404)
        mock_client.post.side_effect = [
            {
                "ok": True,
                "conversation": {"id": "conv-new", "name": "pop-test", "type": "workspace_channel"},
            },
            {
                "upload_url": "https://s3.example.com/upload",
                "upload_fields": {"key": "abc"},
                "s3_key": "key.tar.gz",
            },
            {
                "conversation_id": "conv-new",
                "site_name": "pop-test",
                "version": 1,
                "commit_hash": "abc123",
            },
            _SITE_STATUS,
        ]

        with (
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_core.operations.deploy_upload"),
            patch("os.unlink"),
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
            patch("popcorn_cli.cli.load_config", return_value=_mock_load_config()),
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = False
            from popcorn_cli.cli import cmd_pop

            cmd_pop(pop_args)

        local = json.loads((tmp_path / ".popcorn.local.json").read_text())
        assert local["targets"]["pop-test"]["conversation_id"] == "conv-new"

    def test_pop_publish_vm_error_surfaced(self, mock_client, tmp_path, monkeypatch, pop_args):
        """VM error from publish body is surfaced to user."""
        monkeypatch.chdir(tmp_path)
        _write_v2_local(tmp_path, "conv-1", "my-site")

        mock_client.get.return_value = {
            "conversation": {"id": "conv-1", "site": {"name": "my-site"}},
        }
        mock_client.post.side_effect = [
            {
                "upload_url": "https://s3.example.com/upload",
                "upload_fields": {"key": "abc"},
                "s3_key": "key.tar.gz",
            },
            APIError(
                "Publish failed on VM",
                status_code=500,
                body='{"error": "No changes to commit"}',
            ),
        ]

        with (
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_core.operations.deploy_upload"),
            patch("os.unlink"),
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
        ):
            from popcorn_cli.cli import cmd_pop

            with pytest.raises(PopcornError, match="Publish failed: No changes to commit"):
                cmd_pop(pop_args)

    def test_pop_502_retry(self, mock_client, tmp_path, monkeypatch, pop_args):
        """502 on publish triggers retry."""
        monkeypatch.chdir(tmp_path)
        _write_v2_local(tmp_path, "conv-1", "my-site")
        (tmp_path / ".gitignore").write_text("")

        mock_client.get.side_effect = [
            {
                "conversation": {"id": "conv-1", "site": {"name": "my-site"}},
            },
            _SITE_STATUS,
        ]
        mock_client.post.side_effect = [
            {
                "upload_url": "https://s3.example.com/upload",
                "upload_fields": {"key": "abc"},
                "s3_key": "key.tar.gz",
            },
            APIError("Bad Gateway", status_code=502),
            {
                "conversation_id": "conv-1",
                "site_name": "my-site",
                "version": 2,
                "commit_hash": "abc",
            },
        ]

        with (
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_core.operations.deploy_upload"),
            patch("os.unlink"),
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
            patch("popcorn_cli.cli.load_config", return_value=_mock_load_config()),
            patch("time.sleep"),
        ):
            from popcorn_cli.cli import cmd_pop

            cmd_pop(pop_args)

        # presign + publish(502) + publish(success) = 3 post calls
        assert mock_client.post.call_count == 3

    def test_pop_force_flag_passed_to_publish(self, mock_client, tmp_path, monkeypatch, pop_args):
        """--force passes force=True to deploy_publish."""
        monkeypatch.chdir(tmp_path)
        pop_args.force = True
        _write_v2_local(tmp_path, "conv-1", "my-site")
        (tmp_path / ".gitignore").write_text("")

        mock_client.get.side_effect = [
            {
                "conversation": {"id": "conv-1", "site": {"name": "my-site"}},
            },
            _SITE_STATUS,
        ]
        mock_client.post.side_effect = [
            {
                "upload_url": "https://s3.example.com/upload",
                "upload_fields": {"key": "abc"},
                "s3_key": "key.tar.gz",
            },
            {
                "conversation_id": "conv-1",
                "site_name": "my-site",
                "version": 2,
                "commit_hash": "abc",
            },
        ]

        with (
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_core.operations.deploy_upload"),
            patch("os.unlink"),
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
            patch("popcorn_cli.cli.load_config", return_value=_mock_load_config()),
        ):
            from popcorn_cli.cli import cmd_pop

            cmd_pop(pop_args)

        # Check the publish call includes force=True (last post call)
        publish_call = mock_client.post.call_args_list[-1]
        assert publish_call[1]["data"]["force"] is True

    def test_pop_presign_no_site_auto_provisions(
        self, mock_client, tmp_path, monkeypatch, pop_args
    ):
        """Deploy with --target to a siteless channel auto-provisions via update_conversation."""
        monkeypatch.chdir(tmp_path)
        pop_args.name = "pop-test"
        conv_uuid = "00000000-0000-0000-0000-000000000001"
        _write_v2_local(tmp_path, conv_uuid, "pop-test")
        (tmp_path / ".gitignore").write_text("")

        no_site_body = json.dumps({"detail": {"error": "no_site"}})

        mock_client.get.side_effect = [
            # _validate_channel
            {"conversation": {"id": conv_uuid}},
            # site-status after publish
            _SITE_STATUS,
        ]
        mock_client.post.side_effect = [
            # First presign → no_site
            APIError(
                "Conversation does not have a provisioned site", status_code=400, body=no_site_body
            ),
            # update_conversation (provision site) — conv_uuid passes through resolve_conversation
            {"ok": True},
            # Second presign (after provision) → success
            {
                "upload_url": "https://s3.example.com/upload",
                "upload_fields": {"key": "abc"},
                "s3_key": "ws/sites/pop-test/versions/456.tar.gz",
            },
            # publish
            {
                "conversation_id": conv_uuid,
                "site_name": "pop-test",
                "version": 1,
                "commit_hash": "abc123",
            },
        ]

        with (
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_core.operations.deploy_upload"),
            patch("os.unlink"),
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
            patch("popcorn_cli.cli.load_config", return_value=_mock_load_config()),
        ):
            from popcorn_cli.cli import cmd_pop

            cmd_pop(pop_args)

        # update_conversation should have been called to provision the site
        update_call = mock_client.post.call_args_list[1]
        assert update_call[0][0] == "/api/conversations/update"
        assert update_call[1]["data"]["conversation_type"] == "workspace_channel"
        assert update_call[1]["data"]["site_name"] == "pop-test"

    def test_pop_create_already_exists_auto_provisions(
        self, mock_client, tmp_path, monkeypatch, pop_args
    ):
        """First deploy to a channel that already exists (no site) auto-provisions."""
        monkeypatch.chdir(tmp_path)
        pop_args.name = "pop-test"
        (tmp_path / ".gitignore").write_text("")

        conv_uuid = "00000000-0000-0000-0000-000000000002"
        already_exists_body = json.dumps({"detail": {"error": "already_exists"}})

        mock_client.get.side_effect = [
            # resolve_conversation for get_conversation_info: conversations/list
            {"conversations": [{"id": conv_uuid, "name": "pop-test"}]},
            # get_conversation_info: conversations/info
            {"conversation": {"id": conv_uuid, "name": "pop-test", "metadata": {}}},
            # get_conversation_info: conversations/members
            {"members": []},
            # site-status after publish
            _SITE_STATUS,
        ]
        mock_client.post.side_effect = [
            # deploy_create → already_exists
            APIError("Channel already exists", status_code=400, body=already_exists_body),
            # update_conversation (provision site) — conv_uuid passes through resolve_conversation
            {"ok": True},
            # presign
            {
                "upload_url": "https://s3.example.com/upload",
                "upload_fields": {"key": "abc"},
                "s3_key": "ws/sites/pop-test/versions/123.tar.gz",
            },
            # publish
            {
                "conversation_id": conv_uuid,
                "site_name": "pop-test",
                "version": 1,
                "commit_hash": "abc123",
            },
        ]

        with (
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_core.operations.deploy_upload"),
            patch("os.unlink"),
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
            patch("popcorn_cli.cli.load_config", return_value=_mock_load_config()),
        ):
            from popcorn_cli.cli import cmd_pop

            cmd_pop(pop_args)

        # update_conversation should have been called to provision the site
        update_calls = [
            c for c in mock_client.post.call_args_list if c[0][0] == "/api/conversations/update"
        ]
        assert len(update_calls) == 1
        assert update_calls[0][1]["data"]["conversation_type"] == "workspace_channel"
        assert update_calls[0][1]["data"]["site_name"] == "pop-test"

    def test_pop_create_already_exists_ghost_channel_raises_conflict(
        self, mock_client, tmp_path, monkeypatch, pop_args
    ):
        """Server says channel exists but it's not in the user's channel list.

        The previous behavior leaked 'Channel not found: #<name>' from
        resolve_conversation — confusing because the server just said it exists.
        Expect a clear 'conflict' error instead.
        """
        monkeypatch.chdir(tmp_path)
        pop_args.name = "pop-test"
        (tmp_path / ".gitignore").write_text("")

        # Clear resolve cache — prior tests may have cached 'pop-test'
        from popcorn_core.resolve import _channel_cache

        _channel_cache.clear()

        already_exists_body = json.dumps({"detail": {"error": "already_exists"}})

        mock_client.get.side_effect = [
            # resolve_conversation for get_conversation_info:
            # pop-test is NOT in the list → ghost channel
            {"conversations": [{"id": "other-id", "name": "other-channel"}]},
        ]
        mock_client.post.side_effect = [
            # deploy_create → already_exists
            APIError("Channel already exists", status_code=400, body=already_exists_body),
        ]

        with (
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_core.operations.deploy_upload"),
            patch("os.unlink"),
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
            patch("popcorn_cli.cli.load_config", return_value=_mock_load_config()),
        ):
            from popcorn_cli.cli import cmd_pop

            with pytest.raises(PopcornError) as exc_info:
                cmd_pop(pop_args)

        assert exc_info.value.error_code == "conflict"
        msg = str(exc_info.value)
        assert "pop-test" in msg
        # Message should clearly convey the access conflict, not leak
        # "Channel not found" from resolve_conversation.
        assert "not found" not in msg.lower()
        assert "accessible" in msg.lower()

    def test_pop_existing_site_still_works(self, mock_client, tmp_path, monkeypatch, pop_args):
        """Deploy to channel that already has a site works without provisioning."""
        monkeypatch.chdir(tmp_path)
        _write_v2_local(tmp_path, "conv-existing", "pop-test")
        (tmp_path / ".gitignore").write_text("")

        mock_client.get.side_effect = [
            {"conversation": {"id": "conv-existing", "site": {"name": "pop-test"}}},
            _SITE_STATUS,
        ]
        mock_client.post.side_effect = [
            {
                "upload_url": "https://s3.example.com/upload",
                "upload_fields": {"key": "abc"},
                "s3_key": "ws/sites/pop-test/versions/456.tar.gz",
            },
            {
                "conversation_id": "conv-existing",
                "site_name": "pop-test",
                "version": 2,
                "commit_hash": "def456",
            },
        ]

        with (
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_core.operations.deploy_upload"),
            patch("os.unlink"),
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
            patch("popcorn_cli.cli.load_config", return_value=_mock_load_config()),
        ):
            from popcorn_cli.cli import cmd_pop

            cmd_pop(pop_args)

        # No update_conversation calls — site already provisioned
        update_calls = [
            c for c in mock_client.post.call_args_list if c[0][0] == "/api/conversations/update"
        ]
        assert len(update_calls) == 0
        assert mock_client.post.call_count == 2  # presign + publish


class TestStatus:
    def test_status_from_local_json(self, mock_client, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_v2_local(tmp_path, "conv-1", "my-site")

        mock_client.get.return_value = {
            "site_name": "my-site",
            "url": "https://my-site.popcorn.ai",
            "version": 3,
            "commit_hash": "abc1234",
            "deployed_at": "2026-03-12T14:30:00Z",
            "deployed_by": "user@example.com",
        }

        args = MagicMock()
        args.channel = None
        args.target = None
        args.json = False
        args.env = None
        args.workspace = None

        with patch("popcorn_cli.cli._get_client", return_value=mock_client):
            from popcorn_cli.cli import cmd_status

            cmd_status(args)

    def test_status_fallback(self, mock_client, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _write_v2_local(tmp_path, "conv-1", "my-site")

        mock_client.get.side_effect = [
            APIError("Not found", status_code=404),
            {"conversation": {"name": "my-site"}},
        ]

        args = MagicMock()
        args.channel = None
        args.target = None
        args.json = False
        args.env = None
        args.workspace = None

        with patch("popcorn_cli.cli._get_client", return_value=mock_client):
            from popcorn_cli.cli import cmd_status

            cmd_status(args)

        output = capsys.readouterr().out
        assert "my-site" in output
        assert "Detailed status not available" in output

    def test_status_no_local_json(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        args = MagicMock()
        args.channel = None
        args.target = None
        args.json = False
        args.env = None
        args.workspace = None

        with pytest.raises(PopcornError, match="No channel specified"):
            from popcorn_cli.cli import cmd_status

            cmd_status(args)


class TestLog:
    def test_log_versions(self, mock_client, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _write_v2_local(tmp_path, "conv-1", "my-site")

        mock_client.get.return_value = {
            "versions": [
                {
                    "version": 3,
                    "commit_hash": "abc1234567",
                    "message": "Update landing page",
                    "author": "user@example.com",
                    "created_at": "2026-03-12T14:30:00Z",
                },
                {
                    "version": 2,
                    "commit_hash": "def5678901",
                    "message": "Fix typo",
                    "author": "user@example.com",
                    "created_at": "2026-03-11T10:15:00Z",
                },
            ],
        }

        args = MagicMock()
        args.channel = None
        args.target = None
        args.json = False
        args.env = None
        args.workspace = None
        args.limit = 10

        with patch("popcorn_cli.cli._get_client", return_value=mock_client):
            from popcorn_cli.cli import cmd_log

            cmd_log(args)

        output = capsys.readouterr().out
        assert "v3" in output
        assert "abc1234" in output
        assert "Update landing page" in output

    def test_log_fallback(self, mock_client, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        _write_v2_local(tmp_path, "conv-1", "my-site")

        mock_client.get.side_effect = APIError("Not found", status_code=404)

        args = MagicMock()
        args.channel = None
        args.target = None
        args.json = False
        args.env = None
        args.workspace = None
        args.limit = 10

        with patch("popcorn_cli.cli._get_client", return_value=mock_client):
            from popcorn_cli.cli import cmd_log

            cmd_log(args)

        output = capsys.readouterr().out
        assert "not available yet" in output


class TestPopParser:
    @pytest.fixture()
    def parser(self):
        return build_parser()

    def test_deploy_defaults(self, parser):
        args = parser.parse_args(["site", "deploy"])
        assert args.command == "site"
        assert args.site_command == "deploy"
        assert args.name is None
        assert args.context == ""
        assert args.force is False

    def test_deploy_with_options(self, parser):
        args = parser.parse_args(["site", "deploy", "my-app", "--context", "initial release"])
        assert args.name == "my-app"
        assert args.context == "initial release"

    def test_deploy_force_flag(self, parser):
        args = parser.parse_args(["site", "deploy", "--force"])
        assert args.force is True

    def test_status_parser(self, parser):
        args = parser.parse_args(["site", "status"])
        assert args.command == "site"
        assert args.site_command == "status"
        assert args.channel is None

    def test_status_with_channel(self, parser):
        args = parser.parse_args(["site", "status", "my-channel"])
        assert args.channel == "my-channel"

    def test_log_parser(self, parser):
        args = parser.parse_args(["site", "log"])
        assert args.command == "site"
        assert args.site_command == "log"
        assert args.limit == 10

    def test_log_with_limit(self, parser):
        args = parser.parse_args(["site", "log", "--limit", "5"])
        assert args.limit == 5
