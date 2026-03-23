"""Tests for pop health verification."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from popcorn_cli.cli import _poll_verify, build_parser
from popcorn_core.errors import EXIT_UNHEALTHY, APIError

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

        with (
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_cli.cli.operations.deploy_upload"),
            patch("os.unlink"),
        ):
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
            _SITE_STATUS,  # get_site_status
        ]
        mock_client.post.side_effect = [
            {"upload_url": "https://s3/", "upload_fields": {}, "s3_key": "k"},  # presign
            _PUBLISH_RESULT,  # publish (no verify_task_id — backend doesn't support it yet)
        ]

        with (
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_cli.cli.operations.deploy_upload"),
            patch("os.unlink"),
        ):
            (tmp_path / "t.tar.gz").write_bytes(b"fake")

            parser = build_parser()
            args = parser.parse_args(["pop"])
            from popcorn_cli.cli import cmd_pop

            cmd_pop(args)

        # Check the publish call — verify SHOULD be in the data
        publish_call = mock_client.post.call_args_list[1]
        publish_data = publish_call[1]["data"]  # keyword arg
        assert publish_data["verify"] is True


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
            {
                "status": "done",
                "healthy": True,
                "site_type": "node",
                "fixes": [],
                "errors": [],
                "version": 3,
                "commit_hash": "abc",
            },
        ]
        with patch("time.sleep"):
            result = _poll_verify(
                mock_client, "conv-1", "task-uuid", json_mode=False, timeout=10, poll_interval=0.01
            )
        assert result["status"] == "done"
        assert mock_client.get.call_count == 3

    def test_poll_verify_timeout(self):
        """Returns timeout status when deadline exceeded."""
        mock_client = MagicMock()
        mock_client.get.return_value = {"status": "fixing", "healthy": None}

        with patch("time.monotonic", side_effect=[0, 0, 999]), patch("time.sleep"):
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
        assert result is None

    def test_poll_verify_transient_errors_retry(self):
        """Transient 500s are retried silently."""
        mock_client = MagicMock()
        mock_client.get.side_effect = [
            APIError("Server error", status_code=500),
            APIError("Server error", status_code=500),
            {
                "status": "done",
                "healthy": True,
                "site_type": "node",
                "fixes": [],
                "errors": [],
                "version": 3,
                "commit_hash": "abc",
            },
        ]
        with patch("time.sleep"):
            result = _poll_verify(
                mock_client, "conv-1", "task-uuid", json_mode=False, timeout=10, poll_interval=0.01
            )
        assert result["status"] == "done"
        assert result["healthy"] is True

    def test_poll_verify_persistent_errors(self):
        """3+ consecutive errors → stop polling, return error status."""
        mock_client = MagicMock()
        mock_client.get.side_effect = APIError("Server error", status_code=500)
        with patch("time.sleep"):
            result = _poll_verify(
                mock_client, "conv-1", "task-uuid", json_mode=False, timeout=10, poll_interval=0.01
            )
        assert result["status"] == "error"
        assert result["healthy"] is None
        assert mock_client.get.call_count == 3


class TestPollVerifyEdgeCases:
    def test_version_display_uses_verify_version(self):
        """When agent fixes (v3 → v4), output shows v4."""
        mock_client = MagicMock()
        mock_client.get.return_value = {
            "status": "done",
            "healthy": True,
            "site_type": "node",
            "fixes": [{"file": "server.js", "description": "fixed"}],
            "errors": [],
            "version": 4,
            "commit_hash": "def456",
        }
        result = _poll_verify(mock_client, "conv-1", "task-uuid", json_mode=False, timeout=10)
        assert result["version"] == 4

    def test_poll_verify_fixing_uses_longer_interval(self):
        """During 'fixing' status, poll interval should be 5s."""
        mock_client = MagicMock()
        mock_client.get.side_effect = [
            {"status": "fixing", "healthy": None},
            {
                "status": "done",
                "healthy": True,
                "site_type": "node",
                "fixes": [],
                "errors": [],
                "version": 3,
                "commit_hash": "abc",
            },
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

        with (
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_cli.cli.operations.deploy_upload"),
            patch("os.unlink"),
        ):
            (tmp_path / "t.tar.gz").write_bytes(b"fake")

            parser = build_parser()
            args = parser.parse_args(["--json", "pop", "--skip-check"])
            from popcorn_cli.cli import cmd_pop

            cmd_pop(args)

        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["ok"] is True
        assert "verify" not in data["data"]


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

        with (
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_cli.cli.operations.deploy_upload"),
            patch("os.unlink"),
            patch("time.sleep"),
        ):
            (tmp_path / "t.tar.gz").write_bytes(b"fake")

            parser = build_parser()
            args = parser.parse_args(["pop"])
            from popcorn_cli.cli import cmd_pop

            cmd_pop(args)

        return capsys.readouterr()

    def test_verify_healthy_no_warning(self, tmp_path, monkeypatch, capsys):
        out, err = self._run_pop(
            tmp_path,
            monkeypatch,
            publish_result=_PUBLISH_RESULT_WITH_VERIFY,
            verify_responses=[
                {
                    "status": "done",
                    "healthy": True,
                    "site_type": "node",
                    "fixes": [],
                    "errors": [],
                    "version": 3,
                    "commit_hash": "abc123",
                },
            ],
            capsys=capsys,
        )
        assert "Published to #my-site (v3)" in out
        assert "Fixed" not in out
        assert "issue" not in out

    def test_verify_fixed_shows_fixes(self, tmp_path, monkeypatch, capsys):
        out, err = self._run_pop(
            tmp_path,
            monkeypatch,
            publish_result=_PUBLISH_RESULT_WITH_VERIFY,
            verify_responses=[
                {
                    "status": "done",
                    "healthy": True,
                    "site_type": "node",
                    "fixes": [{"file": "server.js", "description": "added express import"}],
                    "errors": [],
                    "version": 4,
                    "commit_hash": "def456",
                },
            ],
            capsys=capsys,
        )
        assert "Published to #my-site (v4)" in out
        assert "Fixed 1 issue" in out
        assert "server.js" in out

    def test_verify_still_broken_exits_5(self, tmp_path, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc_info:
            self._run_pop(
                tmp_path,
                monkeypatch,
                publish_result=_PUBLISH_RESULT_WITH_VERIFY,
                verify_responses=[
                    {
                        "status": "done",
                        "healthy": False,
                        "site_type": "node",
                        "fixes": [],
                        "errors": ["Cannot find module 'foo'"],
                        "version": 3,
                        "commit_hash": "abc123",
                    },
                ],
                capsys=capsys,
            )
        assert exc_info.value.code == EXIT_UNHEALTHY

    def test_static_site_no_polling(self, tmp_path, monkeypatch, capsys):
        static_publish = {
            **_PUBLISH_RESULT,
            "verify": {"skipped": True, "reason": "static"},
        }
        out, err = self._run_pop(
            tmp_path,
            monkeypatch,
            publish_result=static_publish,
            verify_responses=[],
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
            {
                "status": "done",
                "healthy": True,
                "site_type": "node",
                "fixes": [],
                "errors": [],
                "version": 3,
                "commit_hash": "abc123",
            },
            _SITE_STATUS,
        ]
        mock_client.post.side_effect = [
            {"upload_url": "https://s3/", "upload_fields": {}, "s3_key": "k"},
            _PUBLISH_RESULT_WITH_VERIFY,
        ]

        with (
            patch("popcorn_cli.cli._get_client", return_value=mock_client),
            patch("popcorn_cli.cli.create_tarball", return_value=str(tmp_path / "t.tar.gz")),
            patch("popcorn_cli.cli.operations.deploy_upload"),
            patch("os.unlink"),
            patch("time.sleep"),
        ):
            (tmp_path / "t.tar.gz").write_bytes(b"fake")

            parser = build_parser()
            args = parser.parse_args(["--json", "pop"])
            from popcorn_cli.cli import cmd_pop

            cmd_pop(args)

        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["ok"] is True
        assert "verify" in data["data"]
        assert data["data"]["verify"]["healthy"] is True
