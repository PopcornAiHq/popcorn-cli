"""Tests for popcorn upgrade command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from popcorn_cli.cli import _detect_installer, cmd_upgrade


class TestDetectInstaller:
    def test_detect_uv(self):
        with patch("sys.executable", "/home/user/.local/share/uv/tools/popcorn-cli/bin/python"):
            assert _detect_installer() == "uv"

    def test_detect_pipx(self):
        with patch("sys.executable", "/home/user/.local/share/pipx/venvs/popcorn-cli/bin/python"):
            assert _detect_installer() == "pipx"

    def test_detect_unknown(self):
        with patch("sys.executable", "/usr/local/bin/python"):
            assert _detect_installer() is None

    def test_detect_dev_environment(self):
        with patch("sys.executable", "/Users/dev/popcorn-cli/.venv/bin/python"):
            assert _detect_installer() is None


class TestCmdUpgrade:
    def test_upgrade_success(self, capsys):
        with (
            patch("popcorn_cli.cli._detect_installer", return_value="uv"),
            patch("subprocess.run") as mock_run,
            patch("subprocess.check_output", return_value=b"popcorn 0.5.6\n"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            with patch("popcorn_cli.cli.__version__", "0.5.5"):
                cmd_upgrade(MagicMock())
        out = capsys.readouterr()
        assert "Upgrading via uv" in out.err
        assert "0.5.5" in out.err
        assert "0.5.6" in out.err

    def test_upgrade_already_current(self, capsys):
        with (
            patch("popcorn_cli.cli._detect_installer", return_value="uv"),
            patch("subprocess.run") as mock_run,
            patch("subprocess.check_output", return_value=b"popcorn 0.5.5\n"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            with patch("popcorn_cli.cli.__version__", "0.5.5"):
                cmd_upgrade(MagicMock())
        out = capsys.readouterr()
        assert "already up to date" in out.err

    def test_upgrade_unknown_installer(self, capsys):
        with (
            patch("popcorn_cli.cli._detect_installer", return_value=None),
            pytest.raises(SystemExit) as exc_info,
        ):
            cmd_upgrade(MagicMock())
        assert exc_info.value.code == 1
        out = capsys.readouterr()
        assert "Could not detect" in out.err
        assert "uv tool install" in out.err
        assert "pipx install" in out.err
        assert "pip install" in out.err

    def test_upgrade_subprocess_failure(self, capsys):
        with (
            patch("popcorn_cli.cli._detect_installer", return_value="pipx"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1)
            with pytest.raises(SystemExit) as exc_info:
                cmd_upgrade(MagicMock())
        assert exc_info.value.code == 1
        out = capsys.readouterr()
        assert "Upgrade failed" in out.err
        assert "pipx install" in out.err
