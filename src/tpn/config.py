"""Settings, read from TPN_* environment variables.

List settings take JSON, e.g. TPN_TOR_BRIDGES='["obfs4 1.2.3.4:443 ..."]'.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from tpn import host
from tpn.probe import DEFAULT_TARGETS


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TPN_")

    state_dir: Path = Field(default_factory=host.default_state_dir)

    # tailscaled: "managed" runs a private tailscaled; "systemd" steers tailscaled.service;
    # "windows" steers the Tailscale Windows service (the default, and only choice, there).
    daemon_mode: Literal["managed", "systemd", "windows"] = Field(
        default_factory=host.default_daemon_mode
    )
    tailscale_bin: str = Field(default_factory=host.tailscale_binary)
    tailscaled_bin: str = "tailscaled"
    tailscale_socket: Path | None = None
    tun_name: str = "tpn0"
    systemd_dropin: Path = Path("/etc/systemd/system/tailscaled.service.d/50-tpn-proxy.conf")
    windows_service: str = "Tailscale"
    auth_key: SecretStr | None = None
    client_tags: list[str] = ["tag:vpn-client"]

    # tor
    tor_bin: str = Field(default_factory=host.tor_binary)
    tor_http_port: int = 9080
    tor_bootstrap_timeout: float = 180.0
    tor_bridges: list[str] = []
    obfs4proxy_bin: str = Field(default_factory=host.pt_binary)

    # servers
    server_tag: str = "tag:vpn-server"
    api_port: int = 8443
    health_timeout: float = 10.0

    # transitions and watchdog
    probe_targets: list[str] = list(DEFAULT_TARGETS)
    probe_timeout: float = 10.0
    control_timeout: float = 90.0
    direct_verify_timeout: float = 60.0
    watchdog_interval: float = 30.0
    watchdog_failures: int = 3

    @property
    def socket_path(self) -> Path | None:
        if self.tailscale_socket is not None:
            return self.tailscale_socket
        if self.daemon_mode == "managed":
            return self.state_dir / "tailscaled" / "tailscaled.sock"
        return None

    @property
    def stop_file(self) -> Path:
        """`tpn disconnect` creates it; the running agent sees it and tears down cleanly."""
        return self.state_dir / "stop"

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"
