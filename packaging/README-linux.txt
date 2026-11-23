tpn client for Linux (x86_64 and arm64)
=======================================

A single binary. Needs, from your distro: tor, and tailscale (tailscaled +
tailscale CLI, https://tailscale.com/download/linux). For bridges, also
obfs4proxy or lyrebird.

As a daemon (recommended)
-------------------------
    sudo install -m 0755 tpn /usr/local/bin/tpn
    sudo install -m 0644 tpn.service /etc/systemd/system/tpn.service
    sudo systemctl daemon-reload
    sudo systemctl enable --now tpn

    sudo tpn set-key          # paste a tag:vpn-client auth key once (hidden prompt)
    sudo tpn up               # connects and waits; --server NAME, --no-tor, --keep-tor
    sudo tpn status
    sudo tpn servers
    sudo tpn down

The daemon keeps your choices (tpn prefs set keep_tor=true server=vpn-fra-1
auto_connect=true), retries failed connections with backoff, and disconnects
cleanly when the service stops. Its log: /var/lib/tpn/tpnd.log. It listens on
127.0.0.1:9081 only, with a token in /var/lib/tpn/control.token (root only).
Other settings (TPN_* environment variables, see src/tpn/config.py) go in a
drop-in: sudo systemctl edit tpn.

In the foreground (no daemon)
-----------------------------
    sudo TPN_AUTH_KEY=tskey-auth-... tpn connect     # Ctrl-C disconnects
    sudo tpn status
    sudo tpn disconnect

The two refuse to run at the same time.

By default tpn runs its own tailscaled (TUN tpn0, state in /var/lib/tpn) so it
does not fight with a system tailscaled. TPN_DAEMON_MODE=systemd steers
tailscaled.service instead.

The VPN server is a separate package (tpn-server-*-linux.tar.gz).
