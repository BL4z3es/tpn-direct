"""Build the one-file `tpn` client and pack it as dist/tpn-<version>-<platform>.{zip,tar.gz}.

    python packaging/build_client.py --platform windows-arm64 --tor-bundle path/to/tor-bundle

--tor-bundle is the extracted tor expert bundle (the directory holding tor/ and data/). On
Windows it is copied next to tpn.exe, which finds it there. Linux packages leave it out and use
the distro's tor, which gets security updates; a bundled one would not.

The server (`tpn serve`, FastAPI) is never part of these: it is Linux-only and ships as
packaging/build_server.sh's tarball.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tpn import __version__  # noqa: E402

SERVER_ONLY = ("fastapi", "uvicorn", "starlette")
# Bundle path -> path next to tpn.exe. tpn looks for tor\tor.exe beside itself (tpn/host.py).
TOR_FILES = {
    "tor/tor.exe": "tor/tor.exe",
    "tor/pluggable_transports/lyrebird.exe": "tor/pluggable_transports/lyrebird.exe",
    "data/geoip": "tor/data/geoip",
    "data/geoip6": "tor/data/geoip6",
}


def build_binary(work: Path) -> Path:
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--name",
        "tpn",
        "--distpath",
        str(work / "bin"),
        "--workpath",
        str(work / "build"),
        "--specpath",
        str(work),
        "--paths",
        str(ROOT / "src"),
    ]
    for module in SERVER_ONLY:
        args += ["--exclude-module", module]
    args.append(str(ROOT / "packaging" / "tpn_entry.py"))
    subprocess.run(args, check=True)
    exe = work / "bin" / ("tpn.exe" if sys.platform == "win32" else "tpn")
    if not exe.is_file():
        raise SystemExit(f"PyInstaller produced no {exe}")
    return exe


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, help="e.g. linux-x86_64, windows-arm64")
    parser.add_argument("--tor-bundle", type=Path, help="extracted tor expert bundle")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    opts = parser.parse_args()

    windows = opts.platform.startswith("windows")
    if windows and opts.tor_bundle is None:
        raise SystemExit("Windows packages need --tor-bundle")

    work = ROOT / "build" / opts.platform
    shutil.rmtree(work, ignore_errors=True)
    exe = build_binary(work)

    name = f"tpn-{__version__}-{opts.platform}"
    stage = work / name
    stage.mkdir(parents=True)
    shutil.copy2(exe, stage / exe.name)
    readme = ROOT / "packaging" / f"README-{'windows' if windows else 'linux'}.txt"
    shutil.copy2(readme, stage / "README.txt")
    if windows:
        for rel, target in TOR_FILES.items():
            src = opts.tor_bundle / rel
            if not src.is_file():
                raise SystemExit(f"tor bundle is missing {rel}")
            dst = stage / target
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    opts.out.mkdir(parents=True, exist_ok=True)
    if windows:
        archive = opts.out / f"{name}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(stage.rglob("*")):
                zf.write(path, path.relative_to(work))
    else:
        archive = opts.out / f"{name}.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(stage, arcname=name)
    print(archive)


if __name__ == "__main__":
    main()
