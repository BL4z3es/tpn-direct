#!/usr/bin/env bash
# End-to-end check on a real Linux client (root). The VPN server must already be running.
#
#   SERVER_PUBLIC_IP=203.0.113.7 TPN_AUTH_KEY=tskey-auth-... scripts/e2e.sh
#   BLOCK=1 ... scripts/e2e.sh     # also firewall Tailscale and expect transport=tor
#
# Checks: egress goes through the server; transport matches what is reachable; tor is gone when
# direct; disconnect restores the original egress.
set -euo pipefail

: "${SERVER_PUBLIC_IP:?set SERVER_PUBLIC_IP}"
BLOCK="${BLOCK:-0}"
CONNECT_TIMEOUT="${CONNECT_TIMEOUT:-600}"
CHAIN=TPN_E2E

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }
egress() { curl -s -m 20 https://api.ipify.org || true; }

block() {
  iptables -N "$CHAIN" 2>/dev/null || iptables -F "$CHAIN"
  iptables -I OUTPUT -j "$CHAIN"
  for h in controlplane.tailscale.com login.tailscale.com log.tailscale.io \
           $(seq -f 'derp%g.tailscale.com' 1 28); do
    for ip in $(getent ahostsv4 "$h" | awk '{print $1}' | sort -u); do
      iptables -A "$CHAIN" -d "$ip" -j REJECT
    done
  done
}

cleanup() {
  tpn disconnect >/dev/null 2>&1 || true
  if [[ "$BLOCK" == 1 ]]; then
    iptables -D OUTPUT -j "$CHAIN" 2>/dev/null || true
    iptables -F "$CHAIN" 2>/dev/null || true
    iptables -X "$CHAIN" 2>/dev/null || true
  fi
}
trap cleanup EXIT

ORIGINAL="$(egress)"
echo "original egress: $ORIGINAL"
[[ "$ORIGINAL" != "$SERVER_PUBLIC_IP" ]] || fail "already egressing via the server"

if [[ "$BLOCK" == 1 ]]; then
  block
  curl -s -m 10 https://controlplane.tailscale.com/key?v=71 >/dev/null \
    && fail "block not effective" || pass "control plane blocked"
fi

tpn connect >/tmp/tpn-e2e.log 2>&1 &
deadline=$((SECONDS + CONNECT_TIMEOUT))
until [[ "$(tpn status --json 2>/dev/null | jq -r .phase)" == connected ]]; do
  phase="$(tpn status --json 2>/dev/null | jq -r .phase || true)"
  [[ "$phase" == failed ]] && { cat /tmp/tpn-e2e.log; fail "connect failed"; }
  ((SECONDS < deadline)) || { cat /tmp/tpn-e2e.log; fail "not connected after ${CONNECT_TIMEOUT}s"; }
  sleep 5
done
pass "connected: $(tpn status --json | jq -c '{server, transport, detail}')"

NOW="$(egress)"
[[ "$NOW" == "$SERVER_PUBLIC_IP" ]] || fail "egress is $NOW, expected $SERVER_PUBLIC_IP"
pass "egress goes through the VPN server"

transport="$(tpn status --json | jq -r .transport)"
if [[ "$BLOCK" == 1 ]]; then
  [[ "$transport" == tor ]] || fail "expected transport=tor under block, got $transport"
  pgrep -x tor >/dev/null || fail "tor should still be running"
  pass "tor kept for control traffic while blocked"
else
  [[ "$transport" == direct ]] || fail "expected transport=direct, got $transport"
  pgrep -x tor >/dev/null && fail "tor still running after going direct"
  pass "tor dropped after direct path verified"
fi

tpn disconnect
AFTER="$(egress)"
[[ "$AFTER" == "$ORIGINAL" ]] || fail "egress after disconnect is $AFTER, expected $ORIGINAL"
pass "disconnect restored original egress"

cat <<'EOF'

Manual checks still required:
  * From a non-tailnet host: curl http://<server public IP>:8443/v1/health must fail.
  * From a tag:vpn-client node: connecting to another client's tailnet IP must be refused.
EOF
