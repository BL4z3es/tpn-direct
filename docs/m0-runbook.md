# M0: prove the premise by hand

Run this **before** trusting any tpn code on a real network. It answers the questions the whole
design rests on:

1. Can tailscaled log in and reach DERP **only** through tor's HTTP CONNECT proxy?
2. With Tailscale blocked, does the exit node still carry traffic (direct UDP or DERP-over-tor)?
3. When tor is stopped, what breaks, and how soon?
4. What exact health strings does `tailscale status --json` show when the coordination
   server is unreachable? (`CONTROL_HEALTH_MARKERS` in `src/tpn/tailscale.py` depends on this.)

Record results in [`m0-findings.md`](m0-findings.md).

## Setup
- **server**: any Linux VPS. Follow "VPN server" in the README (Docker or bare metal). Note its
  public IP as `$SERVER_IP`.
- **client**: a Linux VM you can firewall (root). Install `tor`, `tailscale`, `curl`, `jq`.
  Stop the system daemon: `systemctl disable --now tailscaled`.
- Tailnet policy from `policy/tailnet.hujson` applied, plus a `tag:vpn-client` auth key.

## 1. Simulate the block (client)
```sh
# Drop everything to Tailscale's control plane and DERP fleet.
for h in controlplane.tailscale.com login.tailscale.com log.tailscale.io \
         $(seq -f 'derp%g.tailscale.com' 1 28); do
  for ip in $(getent ahostsv4 "$h" | awk '{print $1}' | sort -u); do
    iptables -A OUTPUT -d "$ip" -j REJECT
  done
done
curl -m 10 https://controlplane.tailscale.com/key?v=71 ; echo "exit=$?  (expect failure)"
```
To also make **direct WireGuard UDP** fail (the worst case), block UDP except DNS:
`iptables -A OUTPUT -p udp ! --dport 53 -j REJECT`.

## 2. Tor with an HTTP tunnel
```sh
mkdir -p /tmp/m0/tor && cat > /tmp/m0/torrc <<'EOF'
DataDirectory /tmp/m0/tor
SocksPort 0
HTTPTunnelPort 127.0.0.1:9080
Log notice stdout
EOF
tor -f /tmp/m0/torrc | tee /tmp/m0/tor.log &
# wait for "Bootstrapped 100%"
```

## 3. tailscaled through tor
```sh
mkdir -p /tmp/m0/ts
HTTPS_PROXY=http://127.0.0.1:9080 HTTP_PROXY=http://127.0.0.1:9080 \
  tailscaled --statedir=/tmp/m0/ts --socket=/tmp/m0/ts/sock --tun=tpn0 --port=0 \
  > /tmp/m0/tailscaled.log 2>&1 &
TS="tailscale --socket=/tmp/m0/ts/sock"
time $TS up --auth-key=file:/root/client.key --advertise-tags=tag:vpn-client --timeout=180s
$TS status --json | jq '{BackendState, Health}'
$TS netcheck   # which DERP regions are reachable, UDP yes/no
```
Record: did `up` succeed? How long? Any captcha, 403, or rate-limit errors in
`/tmp/m0/tailscaled.log`?

## 4. Exit node
```sh
$TS set --exit-node=<server tailnet IP> --exit-node-allow-lan-access=true
curl -m 20 https://api.ipify.org; echo " (expect $SERVER_IP)"
$TS status --json | jq '.ExitNodeStatus'
$TS ping <server tailnet IP>   # "via DERP(...)" or "via <ip:port>" (direct)
curl -m 60 -o /dev/null -w '%{speed_download}\n' https://speed.cloudflare.com/__down?bytes=25000000
```
Record: direct or DERP? Throughput?

## 5. Drop tor, watch what dies
```sh
pkill -f 'tor -f /tmp/m0/torrc'
for i in $(seq 1 40); do
  date +%T
  $TS status --json | jq -c '{BackendState, Health}'
  curl -s -m 10 https://api.ipify.org || echo "egress FAILED"
  sleep 30
done | tee /tmp/m0/after-tor.log
```
Record: how long did egress keep working? When did a health warning appear, and what was its
**exact text**?

## 6. Repeat with the block removed
`iptables -F OUTPUT`. Restart tailscaled **without** the proxy variables (same state dir). Check it
reconnects without logging in again. This is the `going_direct` transition tpn automates.

## Decision rules for M3 (fill in after running)
- If step 5 shows egress dies within minutes on a blocked network → keeping tor
  (`TOR_KEPT_FOR_CONTROL`) is mandatory there, as already designed.
- If step 3 fails (Tailscale refuses tor exits) → the Tor bootstrap needs a different carrier
  (obfs4 bridge with a fixed exit, or our own DERP/HTTPS relay on the VPN server). Redesign
  before writing more code.
