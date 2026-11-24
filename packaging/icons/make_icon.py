#!/usr/bin/env python3
"""Write packaging/icons/tpn.ico: the app icon (the "connected directly" state) at 16-256 px.

Run from the repository root: python packaging/icons/make_icon.py
The committed tpn.ico is used for the Windows executable and the installer.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tpn.gui.icons import ICON_SIZES, render_icon  # noqa: E402


def main() -> None:

    out = Path(__file__).resolve().parent / "tpn.ico"
    # Pillow writes one entry per size and never upscales, so the base must be the largest;
    # append_images supplies each smaller size drawn at its own resolution, not downscaled.
    images = {size: render_icon("direct", size) for size in ICON_SIZES}
    base = images[max(ICON_SIZES)]
    base.save(
        out,
        format="ICO",
        sizes=[(s, s) for s in ICON_SIZES],
        append_images=[images[s] for s in ICON_SIZES if s != max(ICON_SIZES)],
    )
    print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
