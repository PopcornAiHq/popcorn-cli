"""Tests for pop health verification."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from popcorn_cli.cli import build_parser

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
