import os
import subprocess
import sys

from typer.testing import CliRunner

from tpn import cli, host
from tpn.config import Settings
from tpn.state import StateStore


def test_pid_alive_for_running_and_exited_processes():
    assert host.pid_alive(os.getpid())
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert not host.pid_alive(proc.pid)
    assert not host.pid_alive(0)


def test_secure_dir_creates_the_directory(tmp_path):
    target = tmp_path / "a" / "b"
    host.secure_dir(target)
    assert target.is_dir()


def test_find_binary_falls_back_to_program_files_on_windows(tmp_path, monkeypatch):
    exe = tmp_path / "Tailscale" / "tailscale.exe"
    exe.parent.mkdir()
    exe.touch()
    monkeypatch.setattr(host, "IS_WINDOWS", True)
    monkeypatch.setattr(host.shutil, "which", lambda name: None)
    monkeypatch.setenv("PROGRAMW6432", str(tmp_path))
    assert host.find_binary("tailscale") == str(exe)
    assert host.find_binary("tailscaled") == "tailscaled"


def test_windows_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(host, "IS_WINDOWS", True)
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    settings = Settings()
    assert settings.state_dir == tmp_path / "tpn"
    assert settings.daemon_mode == "service"
    assert settings.socket_path is None  # the service's default pipe

    monkeypatch.setenv("TPN_DAEMON_MODE", "managed")
    assert Settings().socket_path == host.MANAGED_PIPE


def test_linux_defaults(monkeypatch):
    monkeypatch.setattr(host, "IS_WINDOWS", False)
    settings = Settings()
    assert settings.daemon_mode == "managed"
    assert settings.socket_path == str(settings.state_dir / "tailscaled" / "tailscaled.sock")


def test_disconnect_on_windows_asks_instead_of_killing(tmp_path, monkeypatch):
    monkeypatch.setattr(host, "IS_WINDOWS", True)
    monkeypatch.setenv("TPN_STATE_DIR", str(tmp_path))
    StateStore(tmp_path / "state.json").write({"phase": "connected", "pid": 4242})
    alive = iter([True, True, False])
    monkeypatch.setattr(host, "pid_alive", lambda pid: next(alive))
    killed = []
    monkeypatch.setattr(cli.os, "kill", lambda *a: killed.append(a))

    result = CliRunner().invoke(cli.app, ["disconnect"])
    assert result.exit_code == 0, result.output
    assert "disconnected" in result.output
    assert (tmp_path / "stop").exists()
    assert killed == []
