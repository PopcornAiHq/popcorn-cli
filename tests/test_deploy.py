"""Tests for site operations and CLI commands."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from popcorn_cli.cli import build_parser
from popcorn_core import operations
from popcorn_core.archive import create_tarball
from popcorn_core.errors import APIError, PopcornError


class TestDeployCreate:
    def test_deploy_create(self, mock_client):
        mock_client.post.return_value = {
            "conversation_id": "conv-1",
            "site_name": "my-site",
            "name": "my-site",
        }
        result = operations.deploy_create(mock_client, "my-site")
        mock_client.post.assert_called_once_with(
            "/appchannels/sites", data={"site_name": "my-site"}
        )
        assert result["site_name"] == "my-site"
        assert result["conversation_id"] == "conv-1"


class TestDeployPresign:
    def test_deploy_presign(self, mock_client):
        mock_client.post.return_value = {
            "upload_url": "https://s3.example.com/upload",
            "upload_fields": {"key": "abc"},
            "s3_key": "ws/sites/my-site/versions/123.tar.gz",
        }
        result = operations.deploy_presign(mock_client, "my-site")
        mock_client.post.assert_called_once_with("/appchannels/sites/my-site/s3-presign")
        assert result["upload_url"] == "https://s3.example.com/upload"
        assert result["s3_key"].endswith(".tar.gz")


class TestDeployPull:
    def test_deploy_pull(self, mock_client):
        mock_client.post.return_value = {
            "conversation_id": "conv-1",
            "site_name": "my-site",
            "version": 1,
            "commit_hash": "abc123",
        }
        result = operations.deploy_pull(mock_client, "my-site", "s3-key-1", "conv-1")
        mock_client.post.assert_called_once_with(
            "/appchannels/sites/my-site/s3-pull",
            data={"s3_key": "s3-key-1", "conversation_id": "conv-1"},
        )
        assert result["version"] == 1
        assert result["commit_hash"] == "abc123"

    def test_deploy_pull_with_context(self, mock_client):
        mock_client.post.return_value = {
            "conversation_id": "conv-1",
            "site_name": "my-site",
            "version": 2,
            "commit_hash": "def456",
        }
        operations.deploy_pull(mock_client, "my-site", "s3-key-1", "conv-1", context="fix login")
        mock_client.post.assert_called_once_with(
            "/appchannels/sites/my-site/s3-pull",
            data={
                "s3_key": "s3-key-1",
                "conversation_id": "conv-1",
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


class TestSitePush:
    @pytest.fixture()
    def push_args(self):
        args = MagicMock()
        args.name = None
        args.context = ""
        args.json = False
        args.env = None
        args.workspace = None
        return args

    def test_site_push_full_flow(self, mock_client, tmp_path, monkeypatch, push_args):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "index.html").write_text("<html>hello</html>")
        (tmp_path / ".gitignore").write_text("node_modules\n")

        mock_client.post.side_effect = [
            # deploy_create
            {"conversation_id": "conv-1", "site_name": "pop-test", "name": "pop-test"},
            # deploy_presign
            {
                "upload_url": "https://s3.example.com/upload",
                "upload_fields": {"key": "abc"},
                "s3_key": "ws/sites/pop-test/versions/123.tar.gz",
            },
            # deploy_pull
            {
                "conversation_id": "conv-1",
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
        ):
            from popcorn_cli.cli import cmd_site_push

            cmd_site_push(push_args)

        # Verify .popcorn.local.json was written
        local = json.loads((tmp_path / ".popcorn.local.json").read_text())
        assert local["conversation_id"] == "conv-1"
        assert local["site_name"] == "pop-test"

        # Verify .gitignore was updated
        gitignore = (tmp_path / ".gitignore").read_text()
        assert ".popcorn.local.json" in gitignore

    def test_site_push_existing_site(self, mock_client, tmp_path, monkeypatch, push_args):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "index.html").write_text("<html>hello</html>")

        # Pre-existing .popcorn.local.json
        (tmp_path / ".popcorn.local.json").write_text(
            json.dumps({"conversation_id": "conv-existing", "site_name": "pop-test"})
        )

        mock_client.post.side_effect = [
            # deploy_presign (no create call since conversation_id exists)
            {
                "upload_url": "https://s3.example.com/upload",
                "upload_fields": {"key": "abc"},
                "s3_key": "ws/sites/pop-test/versions/456.tar.gz",
            },
            # deploy_pull
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
        ):
            from popcorn_cli.cli import cmd_site_push

            cmd_site_push(push_args)

        # deploy_create should NOT have been called — only presign + pull
        assert mock_client.post.call_count == 2

    def test_site_push_409_conflict(self, mock_client, tmp_path, monkeypatch, push_args):
        monkeypatch.chdir(tmp_path)

        mock_client.post.side_effect = APIError("Site already exists", status_code=409)

        with (
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("os.unlink"),
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
        ):
            from popcorn_cli.cli import cmd_site_push

            with pytest.raises(PopcornError, match="already exists"):
                cmd_site_push(push_args)

    def test_site_push_name_flag(self, mock_client, tmp_path, monkeypatch, push_args):
        monkeypatch.chdir(tmp_path)
        push_args.name = "custom-site"

        mock_client.post.side_effect = [
            {"conversation_id": "conv-1", "site_name": "custom-site", "name": "custom-site"},
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
        ]

        with (
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_core.operations.deploy_upload"),
            patch("os.unlink"),
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
        ):
            from popcorn_cli.cli import cmd_site_push

            cmd_site_push(push_args)

        mock_client.post.assert_any_call("/appchannels/sites", data={"site_name": "custom-site"})


class TestSiteParser:
    @pytest.fixture()
    def parser(self):
        return build_parser()

    def test_site_push_defaults(self, parser):
        args = parser.parse_args(["site", "push"])
        assert args.site_command == "push"
        assert args.name is None
        assert args.context == ""

    def test_site_push_with_options(self, parser):
        args = parser.parse_args(
            [
                "site",
                "push",
                "--name",
                "my-app",
                "--context",
                "initial release",
            ]
        )
        assert args.name == "my-app"
        assert args.context == "initial release"
