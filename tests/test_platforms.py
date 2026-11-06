"""Windows service control, the cross-platform disconnect path, and host helpers."""

import os

import pytest

from tpn import host
from tpn.bootstrap import ConnectOptions
from tpn.daemon import DaemonError, WindowsServiceTailscaled
from tpn.proc import CmdResult
from tpn.tailscale import TailscaleClient

from .conftest import FakeRunner, ok_json, status

PROXY = "http://127.0.0.1:9080"


class FakeServiceEnv:
    def __init__(self, values=None):
        self.values = list(values or [])
        self.writes = 0

    def get(self, service):
        return list(self.values)

    def set(self, service, values):
        self.values = list(values)
        self.writes += 1


def windows(clock, env, handler=None):
    runner = FakeRunner(handler or (lambda a: ok_json(status())))
    d = WindowsServiceTailscaled(
        TailscaleClient(runner), runner, env=env, clock=clock, sleep=clock.sleep
    )
    return d, runner


def restarts(runner):
    return [c for c in runner.calls if c[0] == "powershell.exe"]


def test_windows_service_gets_proxy_and_keeps_other_variables(clock):
    env = FakeServiceEnv(["TS_DEBUG_X=1", "HTTPS_PROXY=http://stale:1"])
    d, runner = windows(clock, env)

    d.apply(PROXY)
    assert env.values == ["TS_DEBUG_X=1", f"HTTPS_PROXY={PROXY}", f"HTTP_PROXY={PROXY}"]
    assert len(restarts(runner)) == 1
    assert "Restart-Service -Name 'Tailscale'" in restarts(runner)[0][-1]

    d.apply(PROXY)  # nothing changed and the service answers: no restart
    assert len(restarts(runner)) == 1

    d.apply(None)
    assert env.values == ["TS_DEBUG_X=1"]
    assert len(restarts(runner)) == 2


def test_windows_shutdown_removes_proxy_only_if_set(clock):
    env = FakeServiceEnv()
    d, runner = windows(clock, env)
    d.shutdown()
    assert env.writes == 0 and not restarts(runner)

    d.apply(PROXY)
    d.shutdown()
    assert env.values == []


def test_windows_restart_failure_is_reported(clock):
    def handler(args):
        if args[0] == "powershell.exe":
            return CmdResult(1, "", "Access is denied")
        return ok_json(status())

    d, _ = windows(clock, FakeServiceEnv(), handler)
    with pytest.raises(DaemonError, match="Access is denied"):
        d.apply(PROXY)


def test_windows_restarts_a_service_that_does_not_answer(clock):
    answers = iter([CmdResult(1, "", "pipe not found")])

    def handler(args):
        if args[0] == "powershell.exe":
            return CmdResult(0, "", "")
        return next(answers, ok_json(status()))

    d, runner = windows(clock, FakeServiceEnv(), handler)
    d.apply(None)
    assert len(restarts(runner)) == 1


def test_pid_alive():
    assert host.pid_alive(os.getpid())
    assert not host.pid_alive(0)
    assert not host.pid_alive(2**22 + 12345)


def test_find_binary_prefers_bundled_copy(tmp_path):
    name = "tor.exe" if host.IS_WINDOWS else "tor"
    (tmp_path / name).write_text("")
    assert host.find_binary("tor", tmp_path) == str(tmp_path / name)


def test_default_options_unchanged():
    assert ConnectOptions().unattended is False
