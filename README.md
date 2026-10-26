# tpn-direct

Gets a device onto a Tailscale tailnet **through Tor**, routes all its traffic out through a
tailnet node tagged `tag:vpn-server`, and drops Tor once the direct path is proven to work.

The point: a network that blocks Tailscale can't stop the device joining, and the local
network/ISP sees only encrypted WireGuard (or Tor) traffic, never the destinations.

## What it is (and isn't)

- **The tunnel is Tailscale's exit-node feature.** tpn does not move packets. It adds:
  1. Tor bootstrap, so tailscaled can log in and reach DERP when Tailscale is blocked.
  2. Server discovery and health checks over a small FastAPI control API.
  3. A safe switch from Tor to direct, with rollback and a watchdog.
- **Who may connect is set by the tailnet policy** ([`policy/tailnet.hujson`](policy/tailnet.hujson)),
  not by tpn code. Only `tag:vpn-server` nodes are auto-approved as exit nodes and expose
  the API. Clients receive no inbound grants.
- **Linux only for now.** Phones can use a server through the official Tailscale app's
  exit-node menu, but without the Tor bootstrap (one-VPN-slot limit on iOS/Android).

## How a client connects

```
tor_starting → tor_ready → tailnet_up → probing → going_direct → connected
```

1. Start a private `tor` with an HTTP CONNECT proxy on `127.0.0.1:9080`.
2. Start tailscaled with `HTTPS_PROXY` pointing at it. Control and DERP traffic go
   through Tor; direct WireGuard UDP does not.
3. `tailscale up` with a tagged auth key.
4. **Probe** `controlplane.tailscale.com` and a DERP server *without* the proxy. This runs
   before an exit node is set, because afterwards every probe would succeed by going through
   the VPN.
5. If reachable: restart tailscaled without the proxy and confirm it reconnects to the
   coordination server, then stop Tor. If it doesn't reconnect, roll back to Tor. If the
   probe fails, keep Tor for control traffic only.
6. Pick the fastest healthy `tag:vpn-server` peer (`GET /v1/health`) and set it as the
   exit node.
7. Watchdog loop: restarts Tor if it dies, falls back to Tor if the coordination server stays
   unreachable on the direct path, and re-picks a server if the exit node goes offline.

## Quick start

### 1. Tailnet policy
Paste [`policy/tailnet.hujson`](policy/tailnet.hujson) into the admin console (Access controls),
then create two auth keys: one tagged `tag:vpn-server` and one tagged `tag:vpn-client`.

### 2. VPN server (systemd, deployed by easy-deploy)
The node needs `tag:vpn-server` (alongside any other tag it has) and must advertise itself as
an exit node:
```sh
sudo sysctl -w net.ipv4.ip_forward=1 net.ipv6.conf.all.forwarding=1
sudo tailscale set --advertise-exit-node
sudo deploy/install.sh --ci-key     # builds /srv/tpn-direct, starts tpn-direct.service
```
`tpn serve` binds the node's tailnet IP on port 8443 and answers tailnet peers only. After
that, every push to `main` deploys through [easy-deploy](https://github.com/BL4z3es/easy-deploy)
and rolls back if `/v1/health` stops answering. Settings: [`deploy/easy-deploy.conf`](deploy/easy-deploy.conf).
No Caddy: the API is tailnet-only and checks the caller's address itself.

### 3. Client (Linux, root, `tor` and `tailscaled` installed)
```sh
pip install .
TPN_AUTH_KEY=tskey-auth-... tpn connect      # foreground; Ctrl-C disconnects
tpn status                                   # from another shell
tpn disconnect
```
Flags: `--no-tor` (skip Tor), `--keep-tor` (never drop it), `--server NAME`.
If Tor itself is blocked, set obfs4 bridges:
`TPN_TOR_BRIDGES='["obfs4 1.2.3.4:443 FINGERPRINT cert=... iat-mode=0"]'`.

By default tpn runs its **own** tailscaled (`TPN_DAEMON_MODE=managed`, TUN `tpn0`, state in
`/var/lib/tpn`). To steer the distro's `tailscaled.service` instead, use
`TPN_DAEMON_MODE=systemd`. All settings are listed in [`src/tpn/config.py`](src/tpn/config.py).

## Development
```sh
uv sync
uv run ruff check . && uv run pytest
```
Everything that touches the outside world (tor, the tailscale CLI, HTTP) is injected, so the
state machine is tested with fakes. Real end-to-end testing needs two machines: see
[`docs/m0-runbook.md`](docs/m0-runbook.md) and [`scripts/e2e.sh`](scripts/e2e.sh).

## Known gaps (read before trusting this)
- **Unverified against a real block.** The M0 runbook has not been run yet. Whether
  tailscaled logs in cleanly over Tor, and which health strings mean "lost control", are
  assumptions until it is.
- **No kill switch.** If the agent or tailscaled dies, traffic goes out unencrypted over the
  normal route. If hiding traffic from the ISP is a hard requirement, that's the next thing
  to build.
- **Tailscale Inc. knows your identity.** This hides traffic from the local network, not from
  the coordination server.
