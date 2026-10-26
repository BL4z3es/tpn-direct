import io
import subprocess

import pytest

from tpn.tor import TorConfig, TorError, TorManager


class FakeTorProcess:
    def __init__(self, output: str, *, exits: bool = True) -> None:
        self.stdout = io.StringIO(output)
        self._exits = exits
        self.returncode: int | None = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None and self._exits:
            self.returncode = 1
        return self.returncode


def manager(tmp_path, proc, **kwargs):
    calls = []

    def popen(args, **kw):
        calls.append(args)
        return proc

    tor = TorManager(TorConfig(data_dir=tmp_path / "tor" / "data"), popen=popen, **kwargs)
    return tor, calls


def test_torrc_exposes_only_an_http_tunnel():
    rendered = TorConfig(data_dir="/x").render()
    assert "SocksPort 0" in rendered
    assert "HTTPTunnelPort 127.0.0.1:9080" in rendered
    assert "UseBridges" not in rendered


def test_torrc_with_bridges():
    cfg = TorConfig(data_dir="/x", bridges=["obfs4 1.2.3.4:443 FP cert=abc iat-mode=0"])
    rendered = cfg.render()
    assert "UseBridges 1" in rendered
    assert "ClientTransportPlugin obfs4 exec obfs4proxy" in rendered
    assert "Bridge obfs4 1.2.3.4:443 FP cert=abc iat-mode=0" in rendered


def test_torrc_rejects_injected_directives():
    with pytest.raises(TorError):
        TorConfig(data_dir="/x", bridges=["1.2.3.4:443\nExitNodes {us}"]).render()


def test_start_waits_for_full_bootstrap(tmp_path):
    proc = FakeTorProcess(
        "[notice] Bootstrapped 5% (conn): Connecting\n[notice] Bootstrapped 100% (done): Done\n",
        exits=False,
    )
    tor, calls = manager(tmp_path, proc)
    tor.start(timeout=5)
    assert tor.is_running()
    assert calls[0][1:] == ["-f", str(tmp_path / "tor" / "torrc")]
    assert "HTTPTunnelPort" in (tmp_path / "tor" / "torrc").read_text()
    tor.stop()
    assert proc.terminated
    assert not tor.is_running()


def test_start_fails_when_tor_exits_early(tmp_path):
    proc = FakeTorProcess("[err] Could not bind to 127.0.0.1:9080\n")
    tor, _ = manager(tmp_path, proc)
    with pytest.raises(TorError, match="Could not bind"):
        tor.start(timeout=5)


def test_start_times_out_and_stops_tor(tmp_path):
    class Blocking(FakeTorProcess):
        def __init__(self):
            super().__init__("", exits=False)
            # A stdout that never yields a line and never ends.
            r, _w = __import__("os").pipe()
            self.stdout = open(r)  # noqa: SIM115

    proc = Blocking()
    ticks = iter([0.0, 0.0, 10.0, 10.0])
    tor, _ = manager(tmp_path, proc, clock=lambda: next(ticks, 10.0))
    with pytest.raises(TorError, match="did not bootstrap"):
        tor.start(timeout=5)
    assert proc.terminated


def test_missing_tor_binary(tmp_path):
    def popen(*a, **k):
        raise FileNotFoundError

    tor = TorManager(TorConfig(data_dir=tmp_path / "d"), popen=popen)
    with pytest.raises(TorError, match="not found"):
        tor.start()


def test_stop_kills_when_terminate_is_ignored(tmp_path):
    class Stubborn(FakeTorProcess):
        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("tor", timeout)
            return self.returncode

    proc = Stubborn("[notice] Bootstrapped 100% (done)\n", exits=False)
    tor, _ = manager(tmp_path, proc)
    tor.start(timeout=5)
    tor.stop()
    assert proc.returncode == -9
