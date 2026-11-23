"""Compile the Inno Setup installer for an already staged Windows client.

    python packaging/windows/build_installer.py \
        --platform windows-x86_64 --stage build/windows-x86_64/tpn-0.3.0-windows-x86_64 \
        --iscc "C:\\Program Files (x86)\\Inno Setup 6\\ISCC.exe"
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from tpn import __version__  # noqa: E402


def iscc_args(
    iscc: str | Path,
    stage: Path,
    *,
    platform: str,
    version: str = __version__,
    output: Path = ROOT / "dist",
) -> list[str]:
    """Return ISCC arguments with absolute, native Windows-friendly paths."""
    architectures = {"windows-x86_64": "x86_64", "windows-arm64": "arm64"}
    try:
        arch = architectures[platform]
    except KeyError as exc:
        raise ValueError(f"Unsupported Windows platform: {platform}") from exc
    return [
        str(iscc),
        f"/DMyAppVersion={version}",
        f"/DArch={arch}",
        f"/DStagedDir={stage.resolve()}",
        f"/DOutputDir={output.resolve()}",
        f"/I{ROOT}",
        str(ROOT / "packaging" / "windows" / "tpn.iss"),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=("windows-x86_64", "windows-arm64"))
    parser.add_argument("--stage", required=True, type=Path)
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument("--iscc", required=True, help="Path to Inno Setup 6's ISCC.exe")
    opts = parser.parse_args()
    subprocess.run(
        iscc_args(opts.iscc, opts.stage, platform=opts.platform, output=opts.out),
        check=True,
        cwd=ROOT,
    )


if __name__ == "__main__":
    main()
