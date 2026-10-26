from pathlib import Path

import pytest

from tpn.proc import CmdResult
from tpn.tailscale import TailscaleClient, TailscaleError, control_healthy

from .conftest import FakeRunner, ok_json, peer, status


def test_commands_use_custom_socket():
    runner = FakeRunner(lambda a: ok_json(status()))
    TailscaleClient(runner, socket=Path("/run/tpn.sock")).status()
    assert runner.calls == [["tailscale", "--socket=/run/tpn.sock", "status", "--json"]]


def test_up_passes_key_by_file_and_resets():
    runner = FakeRunner()
    TailscaleClient(runner).up(
        auth_key_file=Path("/var/lib/tpn/authkey"),
        hostname="laptop",
        tags=["tag:vpn-client"],
        timeout=60,
    )
    assert runner.calls[0] == [
        "tailscale",
        "up",
        "--reset",
        "--timeout=60s",
        "--auth-key=file:/var/lib/tpn/authkey",
        "--hostname=laptop",
        "--advertise-tags=tag:vpn-client",
    ]


def test_set_and_clear_exit_node():
    runner = FakeRunner()
    ts = TailscaleClient(runner)
    ts.set_exit_node("100.64.0.10")
    ts.set_exit_node(None)
    assert runner.calls[0][1:] == [
        "set",
        "--exit-node=100.64.0.10",
        "--exit-node-allow-lan-access=true",
    ]
    assert runner.calls[1][1:] == ["set", "--exit-node=", "--exit-node-allow-lan-access=false"]


def test_failure_raises_with_stderr():
    runner = FakeRunner(lambda a: CmdResult(1, "", "failed to connect to local tailscaled\n"))
    with pytest.raises(TailscaleError, match="failed to connect to local tailscaled"):
        TailscaleClient(runner).status()


def test_peers_with_tag():
    st = status(
        peers=[
            peer("vpn1", "100.64.0.10"),
            peer("laptop", "100.64.0.11", tags=["tag:vpn-client"]),
        ]
    )
    found = TailscaleClient.peers_with_tag(st, "tag:vpn-server")
    assert [p.hostname for p in found] == ["vpn1"]
    assert found[0].ipv4 == "100.64.0.10"
    assert found[0].dns_name == "vpn1.tail1234.ts.net"


def test_whois():
    runner = FakeRunner(
        lambda a: ok_json(
            {
                "Node": {"ComputedName": "laptop", "Tags": ["tag:vpn-client"]},
                "UserProfile": {"LoginName": "tagged-devices"},
            }
        )
    )
    ident = TailscaleClient(runner).whois("100.64.0.11")
    assert ident.node_name == "laptop"
    assert ident.tags == ("tag:vpn-client",)


@pytest.mark.parametrize(
    ("st", "expected"),
    [
        (status(), True),
        (status(backend="NeedsLogin"), False),
        (status(backend="Starting"), False),
        (
            status(
                health=[
                    "Unable to connect to the Tailscale coordination server to synchronize "
                    "the state of your tailnet."
                ]
            ),
            False,
        ),
        (status(health=["not in map poll"]), False),
        (status(health=["Some DNS servers are unreachable"]), True),
    ],
)
def test_control_healthy(st, expected):
    assert control_healthy(st) is expected
