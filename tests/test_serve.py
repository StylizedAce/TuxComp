import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tuxcomp import cli
from tuxcomp.cli import main


def _entry(name):
    return {
        "container": name,
        "project": "p",
        "compose_file": "/tmp/x.yml",
        "created_at": "2026-01-01T00:00:00",
        "ports": [],
        "start": ["proot-distro", "login", name, "-d", "--", "/bin/sh", "-c", "true"],
        "health": None,
        "volume_dirs": [],
    }


def test_backoff_delay():
    assert cli._backoff_delay(0) == 1
    assert cli._backoff_delay(1) == 2
    assert cli._backoff_delay(3) == 8
    assert cli._backoff_delay(99) == 300


def test_serve_once_no_containers(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TUXCOMP_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_running_containers", set)
    assert main(["serve", "--once"]) == 0
    assert "no registered containers" in capsys.readouterr().out


def test_serve_once_restarts_down_container(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TUXCOMP_HOME", str(tmp_path))
    cli._save_registry(_entry("app-one"))
    monkeypatch.setattr(cli, "_running_containers", set)
    calls = []
    monkeypatch.setattr(
        cli, "_start_from_entry", lambda entry, health: calls.append(entry["container"]) or 0
    )
    assert main(["serve", "--once"]) == 0
    assert calls == ["app-one"]
    assert "is down - restarting" in capsys.readouterr().out


def test_serve_once_skips_running(tmp_path, monkeypatch):
    monkeypatch.setenv("TUXCOMP_HOME", str(tmp_path))
    cli._save_registry(_entry("app-one"))
    monkeypatch.setattr(cli, "_running_containers", lambda: {"app-one"})
    called = []
    monkeypatch.setattr(cli, "_start_from_entry", lambda entry, health: called.append(1) or 0)
    assert main(["serve", "--once"]) == 0
    assert called == []


def test_serve_wake_lock_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("TUXCOMP_HOME", str(tmp_path))
    acquired = []
    monkeypatch.setattr(cli, "_acquire_wake_lock", lambda: acquired.append("on") or 0)
    monkeypatch.setattr(cli, "_release_wake_lock", lambda: acquired.append("off") or 0)
    monkeypatch.setattr(cli, "_running_containers", set)
    assert main(["serve", "--once", "--wake-lock"]) == 0
    assert acquired == ["on", "off"]


def test_serve_failure_logs_backoff(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TUXCOMP_HOME", str(tmp_path))
    cli._save_registry(_entry("bad"))
    monkeypatch.setattr(cli, "_running_containers", set)
    monkeypatch.setattr(cli, "_start_from_entry", lambda entry, health: 1)
    assert main(["serve", "--once"]) == 0
    err = capsys.readouterr().err
    assert "restart failed" in err
    assert "next attempt in" in err


def test_serve_status_not_running(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TUXCOMP_HOME", str(tmp_path))
    assert main(["serve", "--status"]) == 0
    assert "not running" in capsys.readouterr().out


def test_serve_detach_writes_pidfile(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TUXCOMP_HOME", str(tmp_path))
    popen_calls = []

    class FakePopen:
        pid = 4242

        def __init__(self, cmd, **kwargs):
            popen_calls.append((list(cmd), kwargs))

    monkeypatch.setattr(cli.subprocess, "Popen", FakePopen)
    assert main(["serve", "--detach", "--wake-lock"]) == 0
    pidfile = tmp_path / ".tuxcomp" / "serve.pid"
    assert pidfile.read_text(encoding="utf-8") == "4242"
    cmd, kwargs = popen_calls[0]
    assert "-m" in cmd and "tuxcomp" in cmd and "serve" in cmd
    assert "--wake-lock" in cmd
    assert kwargs["start_new_session"] is True
    assert "started in the background" in capsys.readouterr().out


def test_serve_detach_already_running(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TUXCOMP_HOME", str(tmp_path))
    root = tmp_path / ".tuxcomp"
    root.mkdir()
    (root / "serve.pid").write_text(str(os.getpid()), encoding="utf-8")

    def boom(*args, **kwargs):
        raise AssertionError("must not spawn a second supervisor")

    monkeypatch.setattr(cli.subprocess, "Popen", boom)
    assert main(["serve", "--detach"]) == 0
    assert "already running" in capsys.readouterr().out


def test_serve_stop_kills_pid(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TUXCOMP_HOME", str(tmp_path))
    root = tmp_path / ".tuxcomp"
    root.mkdir()
    (root / "serve.pid").write_text(str(os.getpid()), encoding="utf-8")
    killed = []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert main(["serve", "--stop"]) == 0
    assert killed[-1] == (os.getpid(), signal.SIGTERM)
    assert not (root / "serve.pid").exists()
