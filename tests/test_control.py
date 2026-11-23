import httpx
import pytest

from tpn import control
from tpn.control import (
    ControlClient,
    ControlError,
    DaemonNotRunning,
    Unauthorized,
    check_request,
    read_token,
    write_token,
)
from tpn.prefs import Prefs, PrefsStore

PORT = 9081


def headers(**extra):
    base = {"Host": f"127.0.0.1:{PORT}", "Authorization": "Bearer secret"}
    base.update(extra)
    return {k: v for k, v in base.items() if v is not None}


@pytest.mark.parametrize(
    ("hdrs", "expected"),
    [
        (headers(), None),
        (headers(Host=f"localhost:{PORT}"), None),
        (headers(Host="evil.example:9081"), 403),
        (headers(Host="127.0.0.1:1234"), 403),
        (headers(Host=None), 403),
        (headers(Origin="http://evil.example"), 403),
        (headers(Authorization="Bearer wrong"), 401),
        (headers(Authorization="Basic secret"), 401),
        (headers(Authorization=None), 401),
    ],
)
def test_check_request(hdrs, expected):
    result = check_request(hdrs, token="secret", port=PORT)
    assert (result[0] if result else None) == expected


def test_token_roundtrip(tmp_path):
    path = tmp_path / "d" / "control.token"
    assert read_token(path) is None
    write_token(path, "abc")
    assert read_token(path) == "abc"
    assert len(control.new_token()) >= 40


def make_client(tmp_path, handler, token="secret"):
    token_file = tmp_path / "control.token"
    if token is not None:
        write_token(token_file, token)
    return ControlClient(token_file=token_file, transport=httpx.MockTransport(handler))


def test_client_sends_token_and_parses(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(202, json={"phase": "tor_starting"})

    client = make_client(tmp_path, handler)
    assert client.connect(server="vpn-1", use_tor=False)["phase"] == "tor_starting"
    req = seen[0]
    assert req.url.path == "/v1/connect" and req.method == "POST"
    assert req.headers["authorization"] == "Bearer secret"
    assert req.url.host == "127.0.0.1" and req.url.port == PORT
    assert req.read() == b'{"server":"vpn-1","use_tor":false}'


def test_client_errors(tmp_path):
    client = make_client(tmp_path, lambda r: httpx.Response(409, json={"error": "busy"}))
    with pytest.raises(ControlError) as e:
        client.connect()
    assert e.value.status == 409 and e.value.message == "busy"

    client = make_client(tmp_path, lambda r: httpx.Response(401, json={"error": "no"}))
    with pytest.raises(Unauthorized):
        client.status()
    assert client.is_running()


def test_client_not_running(tmp_path):
    with pytest.raises(DaemonNotRunning):
        make_client(tmp_path, lambda r: httpx.Response(200, json={}), token=None).status()

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    client = make_client(tmp_path, refuse)
    with pytest.raises(DaemonNotRunning):
        client.status()
    assert not client.is_running()


def test_client_204(tmp_path):
    client = make_client(tmp_path, lambda r: httpx.Response(204))
    assert client.set_auth_key("tskey-auth-x") is None


def test_prefs_store(tmp_path):
    store = PrefsStore(tmp_path / "prefs.json")
    assert store.read() == Prefs()
    prefs = store.update({"server": "vpn-1", "auto_connect": True})
    assert store.read() == prefs and prefs.server == "vpn-1"
    with pytest.raises(ValueError):
        store.update({"nonsense": 1})
    with pytest.raises(ValueError):
        store.update({"use_tor": "maybe"})
    (tmp_path / "prefs.json").write_text("{not json")
    assert store.read() == Prefs()
