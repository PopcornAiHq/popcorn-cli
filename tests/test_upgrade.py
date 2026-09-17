"""Tests for popcorn upgrade command."""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock, patch

import pytest

from popcorn_cli.cli import (
    _check_and_update,
    _detect_installer,
    _fetch_latest_version,
    _is_outdated,
    _read_version_cache,
    _write_version_cache,
    cmd_upgrade,
    cmd_version,
)


class TestDetectInstaller:
    def test_detect_uv_tool(self):
        with patch("sys.executable", "/home/user/.local/share/uv/tools/popcorn-cli/bin/python"):
            assert _detect_installer() == "uv_tool"

    def test_detect_pipx(self):
        with patch("sys.executable", "/home/user/.local/share/pipx/venvs/popcorn-cli/bin/python"):
            assert _detect_installer() == "pipx"

    def test_detect_uv_pip_fallback(self):
        with (
            patch("sys.executable", "/usr/local/bin/python"),
            patch("shutil.which", side_effect=lambda x: "/usr/bin/uv" if x == "uv" else None),
        ):
            assert _detect_installer() == "uv_pip"

    def test_detect_pip_fallback(self):
        with (
            patch("sys.executable", "/usr/local/bin/python"),
            patch("shutil.which", side_effect=lambda x: "/usr/bin/pip" if x == "pip" else None),
        ):
            assert _detect_installer() == "pip"

    def test_detect_unknown(self):
        with (
            patch("sys.executable", "/usr/local/bin/python"),
            patch("shutil.which", return_value=None),
        ):
            assert _detect_installer() is None

    def test_detect_dev_environment(self):
        with (
            patch("sys.executable", "/Users/dev/popcorn-cli/.venv/bin/python"),
            patch("shutil.which", return_value=None),
        ):
            assert _detect_installer() is None


class TestCmdUpgrade:
    """`cmd_upgrade` resolves the newest tag before installing, so each test
    has to state what that lookup returns — otherwise it reaches the network."""

    def test_upgrade_success(self, capsys):
        with (
            patch("popcorn_cli.cli._detect_installer", return_value="uv_tool"),
            patch("popcorn_cli.cli._fetch_latest_version", return_value="0.5.6"),
            patch("popcorn_cli.cli._write_version_cache"),
            patch("subprocess.run") as mock_run,
            patch("subprocess.check_output", return_value="popcorn 0.5.6\n"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            with patch("popcorn_cli.cli.__version__", "0.5.5"):
                cmd_upgrade(MagicMock())
        out = capsys.readouterr()
        assert "Upgrading via uv_tool" in out.err
        assert "0.5.5" in out.err
        assert "0.5.6" in out.err

    def test_upgrade_already_current(self, capsys):
        with (
            patch("popcorn_cli.cli._detect_installer", return_value="uv_tool"),
            patch("popcorn_cli.cli._fetch_latest_version", return_value="0.5.5"),
            patch("popcorn_cli.cli._write_version_cache"),
            patch("subprocess.run") as mock_run,
            patch("subprocess.check_output", return_value="popcorn 0.5.5\n"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            with patch("popcorn_cli.cli.__version__", "0.5.5"):
                cmd_upgrade(MagicMock())
        out = capsys.readouterr()
        assert "already up to date" in out.err

    def test_upgrade_unknown_installer(self, capsys):
        with (
            patch("popcorn_cli.cli._detect_installer", return_value=None),
            patch("popcorn_cli.cli._fetch_latest_version", return_value="0.5.6"),
            patch("popcorn_cli.cli._write_version_cache"),
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
            patch("popcorn_cli.cli._fetch_latest_version", return_value="0.5.6"),
            patch("popcorn_cli.cli._write_version_cache"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=1)
            with pytest.raises(SystemExit) as exc_info:
                cmd_upgrade(MagicMock())
        assert exc_info.value.code == 1
        out = capsys.readouterr()
        assert "Upgrade failed" in out.err
        assert "pipx install" in out.err


class TestFetchLatestVersion:
    def test_success(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = [{"name": "v0.5.7"}]
        mock_resp.raise_for_status = MagicMock()
        with patch("httpx.get", return_value=mock_resp):
            assert _fetch_latest_version() == "0.5.7"

    def test_strips_v_prefix(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = [{"name": "v1.2.3"}]
        mock_resp.raise_for_status = MagicMock()
        with patch("httpx.get", return_value=mock_resp):
            assert _fetch_latest_version() == "1.2.3"

    def test_timeout(self):
        import httpx

        with patch("httpx.get", side_effect=httpx.TimeoutException("timeout")):
            assert _fetch_latest_version() is None

    def test_empty_tags(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        with patch("httpx.get", return_value=mock_resp):
            assert _fetch_latest_version() is None

    def test_parse_error(self):
        mock_resp = MagicMock()
        mock_resp.json.side_effect = ValueError("bad json")
        mock_resp.raise_for_status = MagicMock()
        with patch("httpx.get", return_value=mock_resp):
            assert _fetch_latest_version() is None


class TestVersionCache:
    def test_read_write(self, tmp_path, monkeypatch):
        import popcorn_core.config

        monkeypatch.setattr(popcorn_core.config, "CONFIG_DIR", tmp_path)
        _write_version_cache("0.5.7")
        version, checked_at = _read_version_cache()
        assert version == "0.5.7"
        assert checked_at > 0

    def test_read_missing(self, tmp_path, monkeypatch):
        import popcorn_core.config

        monkeypatch.setattr(popcorn_core.config, "CONFIG_DIR", tmp_path / "nonexistent")
        version, checked_at = _read_version_cache()
        assert version is None
        assert checked_at == 0

    def test_read_corrupt(self, tmp_path, monkeypatch):
        import popcorn_core.config

        monkeypatch.setattr(popcorn_core.config, "CONFIG_DIR", tmp_path)
        (tmp_path / "version-check.json").write_text("not json")
        version, checked_at = _read_version_cache()
        assert version is None
        assert checked_at == 0


class TestIsOutdated:
    def test_older(self):
        assert _is_outdated("0.5.5", "0.5.7") is True

    def test_same(self):
        assert _is_outdated("0.5.7", "0.5.7") is False

    def test_newer(self):
        assert _is_outdated("0.5.8", "0.5.7") is False


class TestCheckAndUpdate:
    def test_up_to_date(self, monkeypatch):
        monkeypatch.setattr("popcorn_cli.cli._quiet", False)
        monkeypatch.delenv("POPCORN_NO_UPDATE_CHECK", raising=False)
        monkeypatch.setattr("sys.argv", ["popcorn", "whoami"])
        with (
            patch("popcorn_cli.cli._read_version_cache", return_value=("0.5.6", time.time())),
            patch("popcorn_cli.cli.__version__", "0.5.6"),
            patch("os.execvp") as mock_exec,
        ):
            _check_and_update()
        mock_exec.assert_not_called()

    def test_outdated_upgrades(self, monkeypatch):
        monkeypatch.setattr("popcorn_cli.cli._quiet", False)
        monkeypatch.delenv("POPCORN_NO_UPDATE_CHECK", raising=False)
        monkeypatch.setattr("sys.argv", ["popcorn", "whoami"])
        with (
            patch("popcorn_cli.cli._read_version_cache", return_value=(None, 0)),
            patch("popcorn_cli.cli._fetch_latest_version", return_value="0.5.7"),
            patch("popcorn_cli.cli._write_version_cache"),
            patch("popcorn_cli.cli.__version__", "0.5.5"),
            patch("popcorn_cli.cli._detect_installer", return_value="uv_tool"),
            patch("subprocess.run", return_value=MagicMock(returncode=0)),
            patch("shutil.which", return_value="/usr/local/bin/popcorn"),
            patch("os.execvp") as mock_exec,
        ):
            _check_and_update()
        mock_exec.assert_called_once()
        assert mock_exec.call_args[0][1] == ["popcorn", "whoami"]
        # Verify env var set to prevent infinite re-exec loop
        assert os.environ.get("POPCORN_NO_UPDATE_CHECK") == "1"
        monkeypatch.delenv("POPCORN_NO_UPDATE_CHECK", raising=False)

    def test_outdated_unknown_installer(self, monkeypatch):
        monkeypatch.setattr("popcorn_cli.cli._quiet", False)
        monkeypatch.delenv("POPCORN_NO_UPDATE_CHECK", raising=False)
        monkeypatch.setattr("sys.argv", ["popcorn", "whoami"])
        with (
            patch("popcorn_cli.cli._read_version_cache", return_value=(None, 0)),
            patch("popcorn_cli.cli._fetch_latest_version", return_value="0.5.7"),
            patch("popcorn_cli.cli._write_version_cache"),
            patch("popcorn_cli.cli.__version__", "0.5.5"),
            patch("popcorn_cli.cli._detect_installer", return_value=None),
            patch("os.execvp") as mock_exec,
        ):
            _check_and_update()
        mock_exec.assert_not_called()

    def test_fetch_fails(self, monkeypatch):
        monkeypatch.setattr("popcorn_cli.cli._quiet", False)
        monkeypatch.delenv("POPCORN_NO_UPDATE_CHECK", raising=False)
        monkeypatch.setattr("sys.argv", ["popcorn", "whoami"])
        with (
            patch("popcorn_cli.cli._read_version_cache", return_value=(None, 0)),
            patch("popcorn_cli.cli._fetch_latest_version", return_value=None),
            patch("os.execvp") as mock_exec,
        ):
            _check_and_update()
        mock_exec.assert_not_called()

    def test_skips_upgrade_command(self, monkeypatch):
        monkeypatch.setattr("popcorn_cli.cli._quiet", False)
        monkeypatch.delenv("POPCORN_NO_UPDATE_CHECK", raising=False)
        monkeypatch.setattr("sys.argv", ["popcorn", "upgrade"])
        with patch("popcorn_cli.cli._read_version_cache") as mock_cache:
            _check_and_update()
        mock_cache.assert_not_called()

    def test_env_var_opt_out(self, monkeypatch):
        monkeypatch.setattr("popcorn_cli.cli._quiet", False)
        monkeypatch.setenv("POPCORN_NO_UPDATE_CHECK", "1")
        monkeypatch.setattr("sys.argv", ["popcorn", "whoami"])
        with patch("popcorn_cli.cli._read_version_cache") as mock_cache:
            _check_and_update()
        mock_cache.assert_not_called()

    def test_skips_quiet_mode(self, monkeypatch):
        monkeypatch.setattr("popcorn_cli.cli._quiet", True)
        monkeypatch.delenv("POPCORN_NO_UPDATE_CHECK", raising=False)
        monkeypatch.setattr("sys.argv", ["popcorn", "whoami"])
        with patch("popcorn_cli.cli._read_version_cache") as mock_cache:
            _check_and_update()
        mock_cache.assert_not_called()


class TestCmdVersion:
    def test_version_plain(self, capsys):
        with patch("popcorn_cli.cli.__version__", "0.5.6"):
            cmd_version(MagicMock(check=False))
        assert "popcorn 0.5.6" in capsys.readouterr().out

    def test_version_check_outdated(self, capsys):
        with (
            patch("popcorn_cli.cli.__version__", "0.5.5"),
            patch("popcorn_cli.cli._fetch_latest_version", return_value="0.5.7"),
            patch("popcorn_cli.cli._write_version_cache"),
        ):
            cmd_version(MagicMock(check=True))
        out = capsys.readouterr().out
        assert "0.5.7 available" in out
        assert "popcorn upgrade" in out

    def test_version_check_up_to_date(self, capsys):
        with (
            patch("popcorn_cli.cli.__version__", "0.5.7"),
            patch("popcorn_cli.cli._fetch_latest_version", return_value="0.5.7"),
            patch("popcorn_cli.cli._write_version_cache"),
        ):
            cmd_version(MagicMock(check=True))
        assert "up to date" in capsys.readouterr().out

    def test_version_check_fetch_fails(self, capsys):
        with (
            patch("popcorn_cli.cli.__version__", "0.5.6"),
            patch("popcorn_cli.cli._fetch_latest_version", return_value=None),
        ):
            cmd_version(MagicMock(check=True))
        assert "could not check" in capsys.readouterr().out


class TestPinnedUpgradeTarget:
    """The thing installed must be the thing that was compared against.

    Unpinned, `git+…` resolves to the default branch's HEAD, while the decision
    to upgrade is made against the newest TAG. They agree only while tagging
    keeps pace with merges — otherwise an upgrade triggered by tag N quietly
    delivers every untagged commit after it.
    """

    def test_url_is_pinned_to_the_tag(self):
        from popcorn_cli.cli import _github_url

        assert _github_url("0.35.1").endswith("popcorn-cli.git@v0.35.1")

    def test_url_falls_back_to_head_when_no_version_is_known(self):
        from popcorn_cli.cli import _github_url

        assert "@" not in _github_url(None).split("github.com")[1]

    @pytest.mark.parametrize("installer", ["uv_tool", "uv_pip", "pipx", "pip"])
    def test_every_installer_gets_the_pin(self, installer):
        from popcorn_cli.cli import _upgrade_command

        assert any(a.endswith("@v0.35.1") for a in _upgrade_command(installer, "0.35.1"))

    def test_auto_upgrade_installs_the_version_it_compared(self, monkeypatch):
        """The auto path already knows the tag; it must not re-resolve to HEAD."""
        from popcorn_cli.cli import _check_and_update

        # Same defensive clear every _check_and_update test in this file does:
        # the re-exec test above sets POPCORN_NO_UPDATE_CHECK from production
        # code, and monkeypatch then restores that "1" at teardown, so it leaks
        # into whatever runs next.
        monkeypatch.setattr("popcorn_cli.cli._quiet", False)
        monkeypatch.delenv("POPCORN_NO_UPDATE_CHECK", raising=False)

        with (
            patch("popcorn_cli.cli._read_version_cache", return_value=("99.0.0", 2**31)),
            patch("popcorn_cli.cli._detect_installer", return_value="uv_tool"),
            patch("popcorn_cli.cli.__version__", "0.1.0"),
            patch("popcorn_cli.cli.sys.argv", ["popcorn", "whoami"]),
            patch("shutil.which", return_value=None),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            _check_and_update()
            installed = mock_run.call_args[0][0]
        assert any(a.endswith("@v99.0.0") for a in installed)

    def test_manual_instructions_name_the_pinned_target(self, capsys):
        """A user copying the printed command should get the same build."""
        from popcorn_cli.cli import cmd_upgrade

        with (
            patch("popcorn_cli.cli._detect_installer", return_value=None),
            patch("popcorn_cli.cli._fetch_latest_version", return_value="0.35.1"),
            patch("popcorn_cli.cli._write_version_cache"),
            pytest.raises(SystemExit),
        ):
            cmd_upgrade(MagicMock())
        assert capsys.readouterr().err.count("@v0.35.1") == 4

    def test_an_unreachable_tag_list_still_upgrades(self, capsys):
        """Falling back to HEAD beats refusing to upgrade at all."""
        from popcorn_cli.cli import cmd_upgrade

        with (
            patch("popcorn_cli.cli._detect_installer", return_value="uv_tool"),
            patch("popcorn_cli.cli._fetch_latest_version", return_value=None),
            patch("subprocess.run") as mock_run,
            patch("subprocess.check_output", return_value="popcorn 0.5.5\n"),
        ):
            mock_run.return_value = MagicMock(returncode=0)
            cmd_upgrade(MagicMock())
        assert not any("@v" in a for a in mock_run.call_args[0][0])
        assert "installing HEAD" in capsys.readouterr().err
