"""Windows service tests use pywin32 stubs, whether or not it is installed."""

from __future__ import annotations

import importlib
import sys
import types
from unittest.mock import MagicMock

import pytest

from tpn import winservice
from tpn.config import Settings
from tpn.proc import CmdResult

from .conftest import FakeRunner


@pytest.fixture
def service_env(monkeypatch):
    """Rebind the service class to a local ServiceFramework and record event-log writes."""
    manager = MagicMock()

    class Framework:
        def __init__(self, args):
            self.args = args
            self.statuses = []

        def ReportServiceStatus(self, status, **kwargs):
            self.statuses.append((status, kwargs))

    util = types.ModuleType("win32serviceutil")
    util.ServiceFramework = Framework
    win32service = types.ModuleType("win32service")
    win32service.SERVICE_STOP_PENDING = 3
    win32service.SERVICE_RUNNING = 4
    win32service.SERVICE_STOPPED = 1
    monkeypatch.setitem(sys.modules, "win32serviceutil", util)
    monkeypatch.setitem(sys.modules, "win32service", win32service)
    monkeypatch.setitem(sys.modules, "servicemanager", manager)
    original_modules = {
        name: sys.modules.get(name)
        for name in ("win32serviceutil", "win32service", "servicemanager")
    }
    module = importlib.reload(winservice)
    yield module, manager, win32service
    # Restore real pywin32 before reloading, including when this test runs on Windows.
    for name, original in original_modules.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original
    importlib.reload(winservice)


def test_module_imports_without_pywin32(monkeypatch):
    monkeypatch.setitem(sys.modules, "win32serviceutil", None)
    module = importlib.reload(winservice)
    assert module.TpnService._svc_name_ == "tpn"
    assert module.TpnService._svc_display_name_ == "tpn-direct VPN"
    assert "Tor" in module.TpnService._svc_description_
    monkeypatch.undo()
    importlib.reload(winservice)


def test_svc_stop_sets_event_and_reports_pending(service_env):
    module, manager, win32service = service_env
    service = module.TpnService(["tpn"])
    service.SvcStop()
    assert service.stop_event.is_set()
    assert service.statuses == [
        (win32service.SERVICE_STOP_PENDING, {"waitHint": module.STOP_WAIT_HINT_MS})
    ]
    assert module.STOP_WAIT_HINT_MS >= 60_000
    assert manager.LogInfoMsg.called


def test_svc_do_run_passes_settings_and_event_and_reports_running(service_env, monkeypatch):
    module, manager, win32service = service_env
    service = module.TpnService(["tpn"])
    calls = []

    def run(settings, stop):
        calls.append((settings, stop))

    monkeypatch.setattr("tpn.service.run", run)
    service.SvcDoRun()
    assert len(calls) == 1
    settings, stop = calls[0]
    assert isinstance(settings, Settings)
    assert stop is service.stop_event
    assert service.statuses[0][0] == win32service.SERVICE_RUNNING
    assert manager.LogInfoMsg.call_count == 3  # starting, running, stopped


def test_daemon_crash_is_logged_and_stops_without_hanging(service_env, monkeypatch):
    module, manager, win32service = service_env
    service = module.TpnService(["tpn"])

    def crash(settings, stop):
        raise RuntimeError("daemon exploded")

    monkeypatch.setattr("tpn.service.run", crash)
    service.SvcDoRun()
    assert service.stop_event.is_set()
    assert service.statuses[-1] == (
        win32service.SERVICE_STOPPED,
        {"win32ExitCode": 1, "svcExitCode": 1},
    )
    error = manager.LogErrorMsg.call_args.args[0]
    assert "daemon exploded" in error
    assert "RuntimeError" in error


def test_svc_run_propagates_failure_to_pywin32_dispatcher(service_env, monkeypatch):
    module, _, _ = service_env
    service = module.TpnService(["tpn"])
    monkeypatch.setattr("tpn.service.run", MagicMock(side_effect=RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        service.SvcRun()
    assert service.stop_event.is_set()


def test_dispatch_prepares_a_single_service(service_env):
    module, manager, _ = service_env
    module.dispatch()
    assert manager.Initialize.call_count == 1
    manager.PrepareToHostSingle.assert_called_once_with(module.TpnService)
    assert manager.StartServiceCtrlDispatcher.call_count == 1


def test_install_configures_service_dependencies_recovery_and_quoted_path():
    runner = FakeRunner()
    winservice.install(r"C:\Program Files\tpn\tpn.exe", runner=runner)
    assert runner.calls[0] == [
        "sc.exe",
        "create",
        "tpn",
        "binPath=",
        r'"C:\Program Files\tpn\tpn.exe" service-host',
        "start=",
        "delayed-auto",
        "depend=",
        "Tailscale",
        "DisplayName=",
        "tpn-direct VPN",
        "obj=",
        "LocalSystem",
    ]
    assert runner.calls[1][:3] == ["sc.exe", "config", "tpn"]
    assert runner.calls[2] == ["sc.exe", "description", "tpn", winservice.SERVICE_DESCRIPTION]
    assert runner.calls[3] == [
        "sc.exe",
        "failure",
        "tpn",
        "reset=",
        "86400",
        "actions=",
        "restart/10000/restart/30000/restart/60000",
    ]
    assert runner.calls[4] == ["sc.exe", "failureflag", "tpn", "1"]


def test_install_repairs_an_existing_service():
    def existing(args):
        return CmdResult(1073 if args[1] == "create" else 0, "", "already exists")

    runner = FakeRunner(existing)
    winservice.install(r"C:\tpn\tpn.exe", runner=runner)
    assert [call[1] for call in runner.calls] == [
        "create",
        "config",
        "description",
        "failure",
        "failureflag",
    ]


def test_install_reports_sc_errors():
    runner = FakeRunner(lambda args: CmdResult(5, "", "Access is denied"))
    with pytest.raises(RuntimeError, match="Access is denied"):
        winservice.install(r"C:\tpn\tpn.exe", runner=runner)


def test_uninstall_stops_and_deletes_service():
    runner = FakeRunner()
    winservice.uninstall(runner=runner)
    assert runner.calls == [["sc.exe", "stop", "tpn"], ["sc.exe", "delete", "tpn"]]


def test_uninstall_tolerates_missing_or_stopped_service():
    runner = FakeRunner(
        lambda args: CmdResult(1062 if args[1] == "stop" else 1060, "", "not started")
    )
    winservice.uninstall(runner=runner)
    assert len(runner.calls) == 2
