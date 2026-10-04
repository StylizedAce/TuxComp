import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from tuxcomp import adb as adb_mod
from tuxcomp.cli import main


class FakeCompleted:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def _rest(cmd):
    """Drop the adb path and optional '-s serial' prefix."""
    return cmd[3:] if len(cmd) > 1 and cmd[1] == "-s" else cmd[1:]


@pytest.fixture
def fake_adb(monkeypatch):
    """adb on PATH plus one connected device; records every adb invocation."""
    monkeypatch.delenv("ANDROID_SERIAL", raising=False)
    monkeypatch.setattr(adb_mod.shutil, "which", lambda name: "/fake/adb" if name == "adb" else None)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        rest = _rest(cmd)
        if rest == ["devices"]:
            return FakeCompleted("List of devices attached\nRFCN20\tdevice\n")
        if rest == ["shell", "getprop", "ro.product.model"]:
            return FakeCompleted("VOG-L29\n")
        if rest == ["shell", "getprop", "ro.build.version.release"]:
            return FakeCompleted("12\n")
        return FakeCompleted("")

    monkeypatch.setattr(adb_mod.subprocess, "run", fake_run)
    return calls


def _shell_calls(calls):
    return [_rest(call) for call in calls]


def test_find_adb_prefers_path(fake_adb):
    assert adb_mod.find_adb() == "/fake/adb"


@pytest.mark.skipif(os.name != "nt", reason="Windows SDK path layout")
def test_find_adb_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.setattr(adb_mod.shutil, "which", lambda name: None)
    fake_dir = tmp_path / "Android" / "Sdk" / "platform-tools"
    fake_dir.mkdir(parents=True)
    fake_adb_path = fake_dir / "adb.exe"
    fake_adb_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "profile"))
    assert adb_mod.find_adb() == str(fake_adb_path)


def test_setup_adb_dry_run_plans_without_applying(fake_adb, capsys):
    assert adb_mod.setup_adb(apply_changes=False) == 0
    out = capsys.readouterr().out
    assert "planned changes (dry-run)" in out
    assert "max_phantom_processes" in out
    assert "max_cached_processes" in out
    assert "VOG-L29" in out
    shells = _shell_calls(fake_adb)
    assert ["devices"] in shells
    assert all("device_config" not in call and "settings" not in call for call in shells)


def test_setup_adb_apply_runs_all_commands(fake_adb, capsys):
    assert adb_mod.setup_adb(apply_changes=True) == 0
    shells = _shell_calls(fake_adb)
    assert ["shell", "settings", "put", "global", "settings_enable_monitor_phantom_procs", "false"] in shells
    assert ["shell", "device_config", "put", "activity_manager", "max_phantom_processes", "2147483647"] in shells
    assert ["shell", "device_config", "put", "activity_manager", "max_cached_processes", "256"] in shells
    assert ["shell", "device_config", "put", "activity_manager", "max_empty_time_millis", "43200000"] in shells
    assert ["shell", "device_config", "set_sync_disabled_for_tests", "persistent"] in shells
    out = capsys.readouterr().out
    assert "applied 5/5" in out
    assert "Reopen Termux" not in out


def test_setup_adb_apply_with_restart(fake_adb, capsys):
    assert adb_mod.setup_adb(apply_changes=True, restart_termux=True) == 0
    shells = _shell_calls(fake_adb)
    assert ["shell", "am", "force-stop", "com.termux"] in shells
    assert "tuxcomp up --all" in capsys.readouterr().out


def test_setup_adb_skips_version_specific_keys(monkeypatch, capsys):
    monkeypatch.setattr(adb_mod.shutil, "which", lambda name: "/fake/adb" if name == "adb" else None)
    monkeypatch.delenv("ANDROID_SERIAL", raising=False)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        rest = _rest(cmd)
        if rest == ["devices"]:
            return FakeCompleted("List of devices attached\nRFCN20\tdevice\n")
        if rest == ["shell", "getprop", "ro.product.model"]:
            return FakeCompleted("VOG-L29\n")
        if rest == ["shell", "getprop", "ro.build.version.release"]:
            return FakeCompleted("10\n")
        return FakeCompleted("")

    monkeypatch.setattr(adb_mod.subprocess, "run", fake_run)
    assert adb_mod.setup_adb(apply_changes=True) == 0
    out = capsys.readouterr().out
    assert "[skip] stop Play Services from reverting these values (Android 12+)" in out
    assert "applied 4/4" in out
    assert "1 skipped" in out
    shells = _shell_calls(calls)
    assert ["shell", "device_config", "set_sync_disabled_for_tests", "persistent"] not in shells


def test_setup_adb_partial_failures_still_succeed(monkeypatch, capsys):
    monkeypatch.setattr(adb_mod.shutil, "which", lambda name: "/fake/adb" if name == "adb" else None)
    monkeypatch.delenv("ANDROID_SERIAL", raising=False)

    def fake_run(cmd, **kwargs):
        rest = _rest(cmd)
        if rest == ["devices"]:
            return FakeCompleted("List of devices attached\nRFCN20\tdevice\n")
        if rest == ["shell", "getprop", "ro.product.model"]:
            return FakeCompleted("VOG-L29\n")
        if "device_config" in rest:
            return FakeCompleted("", returncode=1)  # old Android: key missing
        return FakeCompleted("")

    monkeypatch.setattr(adb_mod.subprocess, "run", fake_run)
    assert adb_mod.setup_adb(apply_changes=True) == 0
    assert "applied 1/5" in capsys.readouterr().out


def test_setup_adb_all_failures_fail(monkeypatch, capsys):
    monkeypatch.setattr(adb_mod.shutil, "which", lambda name: "/fake/adb" if name == "adb" else None)
    monkeypatch.delenv("ANDROID_SERIAL", raising=False)

    def fake_run(cmd, **kwargs):
        rest = _rest(cmd)
        if rest == ["devices"]:
            return FakeCompleted("List of devices attached\nRFCN20\tdevice\n")
        if rest == ["shell", "getprop", "ro.product.model"]:
            return FakeCompleted("VOG-L29\n")
        return FakeCompleted("", returncode=1)

    monkeypatch.setattr(adb_mod.subprocess, "run", fake_run)
    assert adb_mod.setup_adb(apply_changes=True) == 1
    assert "none of the settings applied" in capsys.readouterr().err


def test_setup_adb_no_device(monkeypatch, capsys):
    monkeypatch.setattr(adb_mod.shutil, "which", lambda name: "/fake/adb" if name == "adb" else None)
    monkeypatch.setattr(
        adb_mod.subprocess, "run", lambda cmd, **kwargs: FakeCompleted("List of devices attached\n")
    )
    assert adb_mod.setup_adb(apply_changes=True) == 1
    assert "no device detected" in capsys.readouterr().err


def test_setup_adb_unauthorized(monkeypatch, capsys):
    monkeypatch.setattr(adb_mod.shutil, "which", lambda name: "/fake/adb" if name == "adb" else None)
    monkeypatch.setattr(
        adb_mod.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompleted("List of devices attached\nRFCN20\tunauthorized\n"),
    )
    assert adb_mod.setup_adb(apply_changes=True) == 1
    assert "unauthorized" in capsys.readouterr().err


def test_setup_adb_multiple_devices(monkeypatch, capsys):
    monkeypatch.setattr(adb_mod.shutil, "which", lambda name: "/fake/adb" if name == "adb" else None)
    monkeypatch.setattr(
        adb_mod.subprocess,
        "run",
        lambda cmd, **kwargs: FakeCompleted("List of devices attached\nA\tdevice\nB\tdevice\n"),
    )
    assert adb_mod.setup_adb(apply_changes=True) == 1
    assert "ANDROID_SERIAL" in capsys.readouterr().err


def test_setup_adb_respects_android_serial(monkeypatch, capsys):
    monkeypatch.setattr(adb_mod.shutil, "which", lambda name: "/fake/adb" if name == "adb" else None)
    monkeypatch.setenv("ANDROID_SERIAL", "B")

    def fake_run(cmd, **kwargs):
        rest = _rest(cmd)
        if rest == ["devices"]:
            return FakeCompleted("List of devices attached\nA\tdevice\nB\tdevice\n")
        if rest == ["shell", "getprop", "ro.product.model"]:
            return FakeCompleted("Redmi\n")
        return FakeCompleted("")

    monkeypatch.setattr(adb_mod.subprocess, "run", fake_run)
    assert adb_mod.setup_adb(apply_changes=False) == 0
    assert "Redmi" in capsys.readouterr().out


def test_setup_adb_no_adb(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(adb_mod.shutil, "which", lambda name: None)
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty"))
    monkeypatch.setattr(adb_mod.os.path, "exists", lambda path: False)
    assert adb_mod.setup_adb(apply_changes=False) == 1
    err = capsys.readouterr().err
    assert "adb not found" in err
    assert "scoop install adb" in err


def test_setup_cli_rejects_apply_without_adb(monkeypatch, capsys):
    assert main(["setup", "--apply"]) == 1
    assert "--adb" in capsys.readouterr().err


def test_setup_cli_adb_dry_run(fake_adb, capsys):
    assert main(["setup", "--adb"]) == 0
    assert "planned changes (dry-run)" in capsys.readouterr().out
