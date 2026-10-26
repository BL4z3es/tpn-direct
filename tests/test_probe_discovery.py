import httpx
import pytest

from tpn.discovery import NoServerAvailable, ServerSelector
from tpn.probe import DirectProbe

from .conftest import peer, status


def test_probe_reports_blocked_targets():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "controlplane.tailscale.com":
            raise httpx.ConnectError("connection reset")
        return httpx.Response(200)

    results = DirectProbe(transport=httpx.MockTransport(handler)).run()
    assert [r.ok for r in results] == [False, True]
    assert "ConnectError" in results[0].detail


def test_probe_treats_server_errors_as_blocked():
    results = DirectProbe(
        ["https://a.example"], transport=httpx.MockTransport(lambda r: httpx.Response(503))
    ).run()
    assert not results[0].ok


def selector(handler, **kwargs) -> ServerSelector:
    return ServerSelector(transport=httpx.MockTransport(handler), **kwargs)


def test_selects_healthy_tagged_exit_node_and_skips_the_rest():
    st = status(
        peers=[
            peer("vpn-down", "100.64.0.10"),
            peer("vpn-ok", "100.64.0.11"),
            peer("vpn-offline", "100.64.0.12", online=False),
            peer("not-exit", "100.64.0.13", exit_node=False),
            peer("client", "100.64.0.14", tags=["tag:vpn-client"]),
        ]
    )
    seen = []

    def handler(request):
        seen.append(request.url.host)
        assert request.url.port == 8443 and request.url.path == "/v1/health"
        if request.url.host == "100.64.0.10":
            return httpx.Response(200, json={"status": "degraded"})
        return httpx.Response(200, json={"status": "ok"})

    chosen = selector(handler).select(st)
    assert chosen.peer.hostname == "vpn-ok"
    assert sorted(seen) == ["100.64.0.10", "100.64.0.11"]


def test_picks_lowest_latency():
    st = status(peers=[peer("slow", "100.64.0.10"), peer("fast", "100.64.0.11")])
    times = iter([0.0, 0.5, 1.0, 1.1])
    chosen = selector(
        lambda r: httpx.Response(200, json={"status": "ok"}), clock=lambda: next(times)
    ).select(st)
    assert chosen.peer.hostname == "fast"


def test_preferred_server_by_short_name():
    st = status(peers=[peer("vpn1", "100.64.0.10"), peer("vpn2", "100.64.0.11")])
    sel = selector(lambda r: httpx.Response(200, json={"status": "ok"}), preferred="vpn2")
    assert sel.select(st).peer.hostname == "vpn2"


def test_no_servers():
    with pytest.raises(NoServerAvailable, match="no online exit node"):
        selector(lambda r: httpx.Response(200)).select(status())


def test_all_unhealthy():
    st = status(peers=[peer("vpn1", "100.64.0.10")])

    def handler(request):
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(NoServerAvailable, match="vpn1"):
        selector(handler).select(st)
