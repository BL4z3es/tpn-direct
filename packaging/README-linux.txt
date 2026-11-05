tpn client for Linux (x86_64 and arm64)
=======================================

A single binary. Needs, from your distro: tor, and tailscale (tailscaled +
tailscale CLI, https://tailscale.com/download/linux). For bridges, also
obfs4proxy or lyrebird.

    sudo install -m 0755 tpn /usr/local/bin/tpn
    sudo TPN_AUTH_KEY=tskey-auth-... tpn connect     # foreground; Ctrl-C disconnects
    sudo tpn status
    sudo tpn disconnect

By default tpn runs its own tailscaled (TUN tpn0, state in /var/lib/tpn) so it
does not fight with a system tailscaled. TPN_DAEMON_MODE=systemd steers
tailscaled.service instead.

The VPN server is a separate package (tpn-server-*-linux.tar.gz).
