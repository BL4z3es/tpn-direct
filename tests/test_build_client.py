"""Test packaging decisions without running PyInstaller or Inno Setup."""

from __future__ import annotations

import importlib.util
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_build_client():
    spec = importlib.util.spec_from_file_location(
        "build_client", ROOT / "packaging" / "build_client.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def option_values(args, option):
    return [args[i + 1] for i, value in enumerate(args[:-1]) if value == option]


def test_windows_console_pyinstaller_arguments(tmp_path):
    build = load_build_client()
    args = build.pyinstaller_args(tmp_path, windows=True)
    assert "--onefile" in args
    assert "--console" in args
    assert "--windowed" not in args
    assert option_values(args, "--name") == ["tpn"]
    hidden = option_values(args, "--hidden-import")
    assert {"win32timezone", "servicemanager", "win32serviceutil", "win32service"} <= set(hidden)
    assert option_values(args, "--exclude-module") == list(build.SERVER_ONLY)
    assert args[-1].endswith("tpn_entry.py")


def test_windows_app_pyinstaller_arguments_and_optional_icon(tmp_path):
    build = load_build_client()
    icon = tmp_path / "tpn.ico"
    args = build.pyinstaller_args(tmp_path, name="tpn-app", app=True, windows=True, icon=icon)
    assert "--onefile" in args
    assert "--windowed" in args
    assert "--console" not in args
    assert option_values(args, "--name") == ["tpn-app"]
    assert option_values(args, "--icon") == [str(icon)]
    hidden = set(option_values(args, "--hidden-import"))
    assert {"pystray", "PIL.Image", "tkinter"} <= hidden
    assert args[-1].endswith("tpn_app_entry.py")
    without_icon = build.pyinstaller_args(tmp_path, name="tpn-app", app=True, windows=True)
    assert "--icon" not in without_icon


def test_linux_console_has_no_windows_service_imports(tmp_path):
    build = load_build_client()
    args = build.pyinstaller_args(tmp_path, windows=False)
    assert not option_values(args, "--hidden-import")
    assert option_values(args, "--exclude-module") == list(build.SERVER_ONLY)


def make_windows_inputs(tmp_path, build):
    executables = []
    for name in ("tpn.exe", "tpn-app.exe"):
        exe = tmp_path / "bin" / name
        exe.parent.mkdir(exist_ok=True)
        exe.write_text(name)
        executables.append(exe)
    tor = tmp_path / "tor-bundle"
    for source in build.TOR_FILES:
        path = tor / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return executables, tor


def test_windows_stage_layout_and_zip(tmp_path):
    build = load_build_client()
    executables, tor = make_windows_inputs(tmp_path, build)
    work = tmp_path / "work"
    stage = build.stage_package(
        work, "tpn-0.3.0-windows-x86_64", executables, windows=True, tor_bundle=tor
    )
    expected = {"tpn.exe", "tpn-app.exe", "README.txt", *(build.TOR_FILES.values())}
    actual = {path.relative_to(stage).as_posix() for path in stage.rglob("*") if path.is_file()}
    assert actual == expected
    archive = build.archive_package(stage, work, tmp_path / "dist", windows=True)
    assert archive.name == "tpn-0.3.0-windows-x86_64.zip"
    with zipfile.ZipFile(archive) as zf:
        assert set(zf.namelist()) == {f"{stage.name}/{name}" for name in expected}


def test_linux_stage_includes_optional_systemd_unit(tmp_path, monkeypatch):
    build = load_build_client()
    unit = tmp_path / "root" / "packaging" / "linux" / "tpn.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n")
    # Keep the real README, but test the daemon worker's optional file without requiring it here.
    readme_dir = tmp_path / "root" / "packaging"
    (readme_dir / "README-linux.txt").write_text(
        (ROOT / "packaging" / "README-linux.txt").read_text()
    )
    monkeypatch.setattr(build, "ROOT", tmp_path / "root")
    exe = tmp_path / "tpn"
    exe.write_text("binary")
    work = tmp_path / "work"
    stage = build.stage_package(work, "tpn-0.3.0-linux-x86_64", [exe], windows=False)
    assert (stage / "tpn.service").read_text() == "[Unit]\n"
    archive = build.archive_package(stage, work, tmp_path / "dist", windows=False)
    with tarfile.open(archive) as tf:
        names = tf.getnames()
    assert f"{stage.name}/tpn" in names
    assert f"{stage.name}/README.txt" in names
    assert f"{stage.name}/tpn.service" in names


def test_windows_stage_requires_a_complete_tor_bundle(tmp_path):
    build = load_build_client()
    exe = tmp_path / "tpn.exe"
    exe.write_text("")
    with pytest.raises(ValueError, match="tor bundle"):
        build.stage_package(tmp_path / "work", "tpn-test", [exe], windows=True)
    empty_bundle = tmp_path / "tor"
    with pytest.raises(SystemExit, match="tor bundle is missing"):
        build.stage_package(
            tmp_path / "work", "tpn-test", [exe], windows=True, tor_bundle=empty_bundle
        )


def test_installer_arguments_match_platform(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "build_installer", ROOT / "packaging" / "windows" / "build_installer.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    args = module.iscc_args("ISCC.exe", tmp_path, platform="windows-arm64", version="1.2.3")
    assert args[0] == "ISCC.exe"
    assert "/DMyAppVersion=1.2.3" in args
    assert "/DArch=arm64" in args
    assert args[-1].endswith("tpn.iss")
    with pytest.raises(ValueError, match="Unsupported"):
        module.iscc_args("ISCC.exe", tmp_path, platform="linux-x86_64")
