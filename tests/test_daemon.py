import pytest

from tpn.daemon import DaemonError, ManagedTailscaled, SystemdTailscaled
from tpn.proc import CmdResult
from tpn.tailscale import TailscaleClient

from .conftest import FakeRunner, ok_json, status


class FakeProc:
    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode


def test_managed_restarts_only_when_proxy_changes(tmp_path, clock, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://corp:3128")
    spawned = []

    def popen(args, env, **kw):
        spawned.append((args, env))
        return FakeProc()

    ts = TailscaleClient(FakeRunner(lambda a: ok_json(status())))
    d = ManagedTailscaled(
        ts,
        state_dir=tmp_path,
        socket=tmp_path / "s.sock",
        popen=popen,
        clock=clock,
        sleep=clock.sleep,
    )
    d.apply("http://127.0.0.1:9080")
    d.apply("http://127.0.0.1:9080")
    d.apply(None)
    assert len(spawned) == 2
    args, tor_env = spawned[0]
    assert f"--socket={tmp_path / 's.sock'}" in args and "--tun=tpn0" in args
    assert tor_env["HTTPS_PROXY"] == "http://127.0.0.1:9080"
    # Going direct must not inherit the shell's proxy either.
    assert "HTTPS_PROXY" not in spawned[1][1]


def test_managed_reports_unresponsive_daemon(tmp_path, clock):
    ts = TailscaleClient(FakeRunner(lambda a: CmdResult(1, "", "no socket")))
    d = ManagedTailscaled(
        ts,
        state_dir=tmp_path,
        socket=tmp_path / "s",
        popen=lambda *a, **k: FakeProc(),
        ready_timeout=5,
        clock=clock,
        sleep=clock.sleep,
    )
    with pytest.raises(DaemonError, match="did not answer"):
        d.apply(None)


def test_systemd_dropin_written_and_removed(tmp_path, clock):
    runner = FakeRunner(lambda a: ok_json(status()))
    ts = TailscaleClient(runner)
    dropin = tmp_path / "tailscaled.service.d" / "50-tpn-proxy.conf"
    d = SystemdTailscaled(ts, runner, dropin=dropin, clock=clock, sleep=clock.sleep)

    d.apply("http://127.0.0.1:9080")
    assert "HTTPS_PROXY=http://127.0.0.1:9080" in dropin.read_text()
    assert ["systemctl", "restart", "tailscaled.service"] in runner.calls

    d.shutdown()
    assert not dropin.exists()
    restarts = [c for c in runner.calls if c[:2] == ["systemctl", "restart"]]
    assert len(restarts) == 2
