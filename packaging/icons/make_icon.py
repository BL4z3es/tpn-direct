#!/usr/bin/env python3
"""Write packaging/icons/tpn.ico with every tray state at 16/24/32/48/64/256 pixels.

Run from the repository root: python packaging/icons/make_icon.py
The committed tpn.ico is used for the Windows executable and the installer.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tpn.gui.icons import ICON_COLOURS, ICON_KINDS, ICON_SIZES, render_icon  # noqa: E402


def main() -> None:
    from PIL import Image

    out = Path(__file__).resolve().parent / "tpn.ico"
    images = [render_icon(kind, size) for kind in ICON_KINDS for size in ICON_SIZES]
    # The largest connected-direct image is the base; Pillow keeps every distinct size in the
    # file, and every size carries its own antialiasing rather than being scaled by the shell.
    base = images[-len(ICON_SIZES)]
    base.save(out, format="ICO", append_images=images, sizes=[(s, s) for s in ICON_SIZES])
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
