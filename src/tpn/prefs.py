"""Connection preferences the daemon keeps between runs (state_dir/prefs.json).

Settings (tpn/config.py) are deployment knobs read from TPN_* environment variables: paths,
ports, timeouts. Prefs are the user's choices, changed at runtime through the control API by
`tpn prefs` or the Windows app, and persisted so a reboot keeps them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError


class Prefs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    use_tor: bool = True
    """Bootstrap through tor. False is `tpn connect --no-tor`."""
    keep_tor: bool = False
    """Never drop tor, even when the direct path works."""
    server: str | None = None
    """Preferred VPN server hostname; None picks the fastest healthy one."""
    hostname: str | None = None
    """This device's tailnet hostname; None lets tailscale choose."""
    tor_bridges: list[str] = []
    """obfs4 bridge lines. Empty falls back to TPN_TOR_BRIDGES."""
    auto_connect: bool = False
    """Connect as soon as the daemon starts (at boot, for the service)."""


class PrefsStore:
    """Reads and atomically writes Prefs. A missing or corrupt file reads as the defaults."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> Prefs:
        try:
            return Prefs.model_validate(json.loads(self.path.read_text()))
        except (FileNotFoundError, json.JSONDecodeError, ValidationError):
            return Prefs()

    def write(self, prefs: Prefs) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(prefs.model_dump_json(indent=2))
        os.replace(tmp, self.path)

    def update(self, patch: dict[str, Any]) -> Prefs:
        """Apply a partial update, validate it, persist it and return the result.

        Raises ValueError (pydantic's ValidationError is one) on an unknown key or bad type.
        """
        merged = self.read().model_dump() | patch
        prefs = Prefs.model_validate(merged)
        self.write(prefs)
        return prefs
