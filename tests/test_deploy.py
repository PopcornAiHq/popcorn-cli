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
from popcorn_core.errors import APIError, PopcornError

# Shared mock response for site-status (appended to post side_effect lists)
_SITE_STATUS = {"url": "https://pop-test.popcorn.ai", "site_name": "pop-test", "version": 1}


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
        ):
            from popcorn_cli.cli import cmd_pop

            cmd_pop(pop_args)

        local = json.loads((tmp_path / ".popcorn.local.json").read_text())
        assert local["conversation_id"] == "conv-1"
        assert local["site_name"] == "pop-test"

        gitignore = (tmp_path / ".gitignore").read_text()
        assert ".popcorn.local.json" in gitignore

    def test_pop_json_includes_site_url(self, mock_client, tmp_path, monkeypatch, pop_args, capsys):
        """JSON output includes site_url from site-status."""
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
            },
            {"url": "https://pop-test.popcorn.ai", "site_name": "pop-test", "version": 1},
        ]

        with (
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_core.operations.deploy_upload"),
            patch("os.unlink"),
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
        ):
            from popcorn_cli.cli import cmd_pop

            cmd_pop(pop_args)

        output = json.loads(capsys.readouterr().out)
        assert output["site_url"] == "https://pop-test.popcorn.ai"

    def test_pop_existing_site(self, mock_client, tmp_path, monkeypatch, pop_args):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".popcorn.local.json").write_text(
            json.dumps({"conversation_id": "conv-existing", "site_name": "pop-test"})
        )

        mock_client.get.return_value = {
            "conversation": {"id": "conv-existing", "site": {"name": "pop-test"}},
        }
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
            _SITE_STATUS,
        ]

        with (
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_core.operations.deploy_upload"),
            patch("os.unlink"),
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
        ):
            from popcorn_cli.cli import cmd_pop

            cmd_pop(pop_args)

        assert mock_client.post.call_count == 3  # presign + publish + site-status

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
        ):
            from popcorn_cli.cli import cmd_pop

            cmd_pop(pop_args)

        local = json.loads((tmp_path / ".popcorn.local.json").read_text())
        assert local["conversation_id"] == "conv-1"

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
        (tmp_path / ".popcorn.local.json").write_text(
            json.dumps({"conversation_id": "dead-conv", "site_name": "old-site"})
        )
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
        ):
            from popcorn_cli.cli import cmd_pop

            cmd_pop(pop_args)

        local = json.loads((tmp_path / ".popcorn.local.json").read_text())
        assert local["conversation_id"] == "conv-new"

    def test_pop_stale_config_no_force_aborts(self, mock_client, tmp_path, monkeypatch, pop_args):
        """Non-interactive + stale config + no --force = abort."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".popcorn.local.json").write_text(
            json.dumps({"conversation_id": "dead-conv", "site_name": "old-site"})
        )

        mock_client.get.side_effect = APIError("Not found", status_code=404)

        with (
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("os.unlink"),
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = False
            from popcorn_cli.cli import cmd_pop

            with pytest.raises(PopcornError, match="Stale channel configuration"):
                cmd_pop(pop_args)

    def test_pop_publish_vm_error_surfaced(self, mock_client, tmp_path, monkeypatch, pop_args):
        """VM error from publish body is surfaced to user."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".popcorn.local.json").write_text(
            json.dumps({"conversation_id": "conv-1", "site_name": "my-site"})
        )

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
        (tmp_path / ".popcorn.local.json").write_text(
            json.dumps({"conversation_id": "conv-1", "site_name": "my-site"})
        )
        (tmp_path / ".gitignore").write_text("")

        mock_client.get.return_value = {
            "conversation": {"id": "conv-1", "site": {"name": "my-site"}},
        }
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
            _SITE_STATUS,
        ]

        with (
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_core.operations.deploy_upload"),
            patch("os.unlink"),
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
            patch("time.sleep"),
        ):
            from popcorn_cli.cli import cmd_pop

            cmd_pop(pop_args)

        # presign + publish(502) + publish(success) + site-status = 4 post calls
        assert mock_client.post.call_count == 4

    def test_pop_force_flag_passed_to_publish(self, mock_client, tmp_path, monkeypatch, pop_args):
        """--force passes force=True to deploy_publish."""
        monkeypatch.chdir(tmp_path)
        pop_args.force = True
        (tmp_path / ".popcorn.local.json").write_text(
            json.dumps({"conversation_id": "conv-1", "site_name": "my-site"})
        )
        (tmp_path / ".gitignore").write_text("")

        mock_client.get.return_value = {
            "conversation": {"id": "conv-1", "site": {"name": "my-site"}},
        }
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
            _SITE_STATUS,
        ]

        with (
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_core.operations.deploy_upload"),
            patch("os.unlink"),
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
        ):
            from popcorn_cli.cli import cmd_pop

            cmd_pop(pop_args)

        # Check the publish call includes force=True (second-to-last post, before site-status)
        publish_call = mock_client.post.call_args_list[-2]
        assert publish_call[1]["data"]["force"] is True


class TestStatus:
    def test_status_from_local_json(self, mock_client, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".popcorn.local.json").write_text(
            json.dumps({"conversation_id": "conv-1", "site_name": "my-site"})
        )

        mock_client.post.return_value = {
            "site_name": "my-site",
            "url": "https://my-site.popcorn.ai",
            "version": 3,
            "commit_hash": "abc1234",
            "deployed_at": "2026-03-12T14:30:00Z",
            "deployed_by": "user@example.com",
        }

        args = MagicMock()
        args.channel = None
        args.json = False
        args.env = None
        args.workspace = None

        with patch("popcorn_cli.cli._get_client", return_value=mock_client):
            from popcorn_cli.cli import cmd_status

            cmd_status(args)

    def test_status_fallback(self, mock_client, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".popcorn.local.json").write_text(
            json.dumps({"conversation_id": "conv-1", "site_name": "my-site"})
        )

        mock_client.post.side_effect = APIError("Not found", status_code=404)
        mock_client.get.return_value = {
            "conversation": {"name": "my-site"},
        }

        args = MagicMock()
        args.channel = None
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
        args.json = False
        args.env = None
        args.workspace = None

        with pytest.raises(PopcornError, match="No channel specified"):
            from popcorn_cli.cli import cmd_status

            cmd_status(args)


class TestLog:
    def test_log_versions(self, mock_client, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".popcorn.local.json").write_text(
            json.dumps({"conversation_id": "conv-1", "site_name": "my-site"})
        )

        mock_client.post.return_value = {
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
        (tmp_path / ".popcorn.local.json").write_text(
            json.dumps({"conversation_id": "conv-1", "site_name": "my-site"})
        )

        mock_client.post.side_effect = APIError("Not found", status_code=404)

        args = MagicMock()
        args.channel = None
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

    def test_pop_defaults(self, parser):
        args = parser.parse_args(["pop"])
        assert args.command == "pop"
        assert args.name is None
        assert args.context == ""
        assert args.force is False

    def test_pop_with_options(self, parser):
        args = parser.parse_args(["pop", "my-app", "--context", "initial release"])
        assert args.name == "my-app"
        assert args.context == "initial release"

    def test_pop_force_flag(self, parser):
        args = parser.parse_args(["pop", "--force"])
        assert args.force is True

    def test_status_parser(self, parser):
        args = parser.parse_args(["status"])
        assert args.command == "status"
        assert args.channel is None

    def test_status_with_channel(self, parser):
        args = parser.parse_args(["status", "my-channel"])
        assert args.channel == "my-channel"

    def test_log_parser(self, parser):
        args = parser.parse_args(["log"])
        assert args.command == "log"
        assert args.limit == 10

    def test_log_with_limit(self, parser):
        args = parser.parse_args(["log", "--limit", "5"])
        assert args.limit == 5
