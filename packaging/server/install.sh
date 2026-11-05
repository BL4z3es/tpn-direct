#!/usr/bin/env bash
# Install or upgrade the tpn VPN server on this Linux host.
#
#   sudo ./install.sh              install the wheel next to this script, start tpn-server
#   sudo ./install.sh --uninstall  stop and remove it (leaves tailscale alone)
#
# Before running: tailscale up with a tag:vpn-server key, and advertise an exit node:
#   sudo tailscale up --auth-key=tskey-auth-... --advertise-tags=tag:vpn-server --advertise-exit-node
set -euo pipefail

PREFIX=/opt/tpn-server
UNIT=/etc/systemd/system/tpn-server.service
HERE=$(cd "$(dirname "$0")" && pwd)

die() { echo "install.sh: $*" >&2; exit 1; }
[[ $(uname -s) == Linux ]] || die "the server runs on Linux only"
[[ $EUID -eq 0 ]] || die "run as root (sudo)"

if [[ ${1:-} == --uninstall ]]; then
    systemctl disable --now tpn-server.service 2>/dev/null || true
    rm -f "$UNIT" /etc/sysctl.d/90-tpn-server.conf
    systemctl daemon-reload
    rm -rf "$PREFIX"
    echo "removed tpn-server"
    exit 0
fi

command -v tailscale >/dev/null || die "tailscale is not installed: https://tailscale.com/download/linux"
command -v python3 >/dev/null || die "python3 (3.11 or newer) is required"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "python3 must be 3.11 or newer"
python3 -c 'import venv, ensurepip' 2>/dev/null || die "python3 venv support is missing (Debian/Ubuntu: apt install python3-venv)"

shopt -s nullglob
wheels=("$HERE"/tpn_direct-*.whl)
[[ ${#wheels[@]} -eq 1 ]] || die "expected exactly one tpn_direct-*.whl next to install.sh"

# An exit node forwards packets; without this, clients connect and then reach nothing.
cat > /etc/sysctl.d/90-tpn-server.conf <<'SYSCTL'
net.ipv4.ip_forward = 1
net.ipv6.conf.all.forwarding = 1
SYSCTL
sysctl -q -p /etc/sysctl.d/90-tpn-server.conf

[[ -x $PREFIX/env/bin/python ]] || python3 -m venv "$PREFIX/env"
"$PREFIX/env/bin/python" -m pip install --disable-pip-version-check --quiet --upgrade \
    "${wheels[0]}[server]"
install -m 0644 "$HERE/tpn-server.service" "$UNIT"
install -d -m 0750 /etc/tpn-server
systemctl daemon-reload
systemctl enable tpn-server.service >/dev/null
systemctl restart tpn-server.service

if ! tailscale status --json 2>/dev/null | grep -q '"tag:vpn-server"'; then
    echo "warning: this node is not tagged tag:vpn-server; clients will not find it" >&2
fi
echo "tpn-server installed; check it with: systemctl status tpn-server"
