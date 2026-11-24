"""Build the client executables and pack them as dist/tpn-<version>-<platform>.{zip,tar.gz}.

    python packaging/build_client.py --platform windows-arm64 --tor-bundle path/to/tor-bundle

--tor-bundle is the extracted tor expert bundle (the directory holding tor/ and data/). On
Windows it is copied next to the executables, which find it there. Linux packages leave it out
and use the distro's tor, which gets security updates; a bundled one would not.

Windows also produces tpn-app.exe (the tray application). CI compiles packaging/windows/tpn.iss
from the staged directory into a separate setup executable. The server is never part of these:
it is Linux-only and ships as packaging/build_server.sh's tarball.
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
WINDOWS_SERVICE_MODULES = (
    "win32timezone",
    "servicemanager",
    "win32serviceutil",
    "win32service",
    "win32event",
    "win32api",
)
# tpn-entry imports tpn.winservice lazily, so PyInstaller needs these explicit imports.
WINDOWS_GUI_MODULES = ("pystray", "pystray._win32", "PIL.Image", "PIL.ImageDraw", "tkinter")
# Bundle path -> path next to tpn.exe. tpn looks for tor\tor.exe beside itself (tpn/host.py).
TOR_FILES = {
    "tor/tor.exe": "tor/tor.exe",
    "tor/pluggable_transports/lyrebird.exe": "tor/pluggable_transports/lyrebird.exe",
    "data/geoip": "tor/data/geoip",
    "data/geoip6": "tor/data/geoip6",
}


def pyinstaller_args(
    work: Path,
    *,
    name: str = "tpn",
    app: bool = False,
    windows: bool = True,
    icon: Path | None = None,
) -> list[str]:
    """Return a complete PyInstaller invocation without executing it."""
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed" if app else "--console",
        "--name",
        name,
        "--distpath",
        str(work / "bin"),
        "--workpath",
        str(work / "build" / name),
        "--specpath",
        str(work),
        "--paths",
        str(ROOT / "src"),
    ]
    hidden = WINDOWS_GUI_MODULES if app else (WINDOWS_SERVICE_MODULES if windows else ())
    for module in (*hidden, *SERVER_ONLY):
        args += ["--exclude-module" if module in SERVER_ONLY else "--hidden-import", module]
    if app and icon is not None:
        args += ["--icon", str(icon)]
    entry = "tpn_app_entry.py" if app else "tpn_entry.py"
    args.append(str(ROOT / "packaging" / entry))
    return args


def build_binary(work: Path, *, name: str = "tpn", app: bool = False) -> Path:
    windows = sys.platform == "win32"
    icon = ROOT / "packaging" / "icons" / "tpn.ico"
    args = pyinstaller_args(
        work,
        name=name,
        app=app,
        windows=windows,
        icon=icon if app and icon.is_file() else None,
    )
    subprocess.run(args, check=True)
    suffix = ".exe" if windows else ""
    exe = work / "bin" / f"{name}{suffix}"
    if not exe.is_file():
        raise SystemExit(f"PyInstaller produced no {exe}")
    return exe


def stage_package(
    work: Path,
    name: str,
    executables: list[Path],
    *,
    windows: bool,
    tor_bundle: Path | None = None,
) -> Path:
    """Stage the complete archive layout, returning its platform-named directory."""
    if windows and tor_bundle is None:
        raise ValueError("Windows packages need a tor bundle")
    stage = work / name
    stage.mkdir(parents=True, exist_ok=True)
    for exe in executables:
        shutil.copy2(exe, stage / exe.name)
    readme = ROOT / "packaging" / f"README-{'windows' if windows else 'linux'}.txt"
    shutil.copy2(readme, stage / "README.txt")
    if windows:
        assert tor_bundle is not None
        for rel, target in TOR_FILES.items():
            src = tor_bundle / rel
            if not src.is_file():
                raise SystemExit(f"tor bundle is missing {rel}")
            dst = stage / target
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    else:
        # Added by the daemon packaging work. Do not require it on the foundation branch.
        unit = ROOT / "packaging" / "linux" / "tpn.service"
        if unit.is_file():
            shutil.copy2(unit, stage / "tpn.service")
    return stage


def archive_package(stage: Path, work: Path, out: Path, *, windows: bool) -> Path:
    """Archive a staged directory and return the resulting package path."""
    out.mkdir(parents=True, exist_ok=True)
    archive = out / f"{stage.name}.{'zip' if windows else 'tar.gz'}"
    if windows:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(work))
    else:
        with tarfile.open(archive, "w:gz") as tf:
            tf.add(stage, arcname=stage.name)
    return archive


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, help="e.g. linux-x86_64, windows-arm64")
    parser.add_argument("--tor-bundle", type=Path, help="extracted tor expert bundle")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    opts = parser.parse_args()

    windows = opts.platform.startswith("windows")
    if windows and not sys.platform == "win32":
        raise SystemExit("Windows packages must be built on a Windows runner")
    if windows and opts.tor_bundle is None:
        raise SystemExit("Windows packages need --tor-bundle")

    work = ROOT / "build" / opts.platform
    shutil.rmtree(work, ignore_errors=True)
    executables = [build_binary(work)]
    if windows:
        executables.append(build_binary(work, name="tpn-app", app=True))

    name = f"tpn-{__version__}-{opts.platform}"
    stage = stage_package(
        work,
        name,
        executables,
        windows=windows,
        tor_bundle=opts.tor_bundle,
    )
    archive = archive_package(stage, work, opts.out, windows=windows)
    print(archive)
    if windows:
        # CI passes this path to Inno Setup. It is deliberately not put in the zip.
        print(f"Staged installer files: {stage}")


if __name__ == "__main__":
    main()
