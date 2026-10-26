import pytest
from fastapi.testclient import TestClient

from tpn.proc import CmdResult
from tpn.server.app import create_app
from tpn.tailscale import TailscaleClient

from .conftest import FakeRunner, ok_json, status

PEER_IP = "100.64.0.11"


def handler(args):
    if args[1] == "status":
        return ok_json(status())
    if args[1] == "whois":
        if args[-1] == PEER_IP:
            return ok_json({"Node": {"ComputedName": "laptop", "Tags": ["tag:vpn-client"]}})
        return CmdResult(1, "", "no match for IP:port")
    raise AssertionError(args)


@pytest.fixture
def runner():
    return FakeRunner(handler)


def client(runner, ip=PEER_IP):
    app = create_app(TailscaleClient(runner), egress_ip=lambda: "203.0.113.7")
    return TestClient(app, client=(ip, 41000))


def test_health_for_tailnet_peer(runner):
    resp = client(runner).get("/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_non_peer_is_rejected(runner):
    resp = client(runner, ip="198.51.100.9").get("/v1/health")
    assert resp.status_code == 403


def test_whoami_and_whois_is_cached(runner):
    c = client(runner)
    assert c.get("/v1/whoami").json()["node_name"] == "laptop"
    c.get("/v1/whoami")
    assert sum(1 for call in runner.calls if call[1] == "whois") == 1


def test_info(runner):
    body = client(runner).get("/v1/info").json()
    assert body["egress_ip"] == "203.0.113.7"
    assert body["tailscale_ips"] == ["100.64.0.2"]


def test_degraded_when_not_running():
    def h(args):
        if args[1] == "whois":
            return ok_json({"Node": {"ComputedName": "laptop"}})
        return ok_json(status(backend="Stopped"))

    resp = client(FakeRunner(h)).get("/v1/health")
    assert resp.json()["status"] == "degraded"
