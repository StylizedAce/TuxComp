import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from tuxcomp import cli
from tuxcomp.cli import main
from tuxcomp.parser import parse_compose_file


@pytest.fixture
def wake_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TUXCOMP_HOME", str(tmp_path))
    calls = []

    class FakeProc:
        returncode = 0

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return FakeProc()

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/fake/bin/" + name)
    return tmp_path, calls


def test_wake_on_creates_marker_and_is_idempotent(wake_env, capsys):
    home, calls = wake_env
    assert main(["wake", "on"]) == 0
    marker = home / ".tuxcomp" / "wake.state"
    assert marker.exists()
    assert calls == [["termux-wake-lock"]]

    assert main(["wake", "on"]) == 0
    assert calls == [["termux-wake-lock"]]  # no second lock
    assert "already held" in capsys.readouterr().out


def test_wake_off_releases_only_tuxcomp_lock(wake_env, capsys):
    home, calls = wake_env
    marker = home / ".tuxcomp" / "wake.state"

    # nothing held: no termux call, no error
    assert main(["wake", "off"]) == 0
    assert calls == []
    assert "not held" in capsys.readouterr().out

    assert main(["wake", "on"]) == 0
    calls.clear()
    assert main(["wake", "off"]) == 0
    assert calls == [["termux-wake-unlock"]]
    assert not marker.exists()


def test_wake_on_without_termux_binary_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TUXCOMP_HOME", str(tmp_path))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert main(["wake", "on"]) == 1
    assert not (tmp_path / ".tuxcomp" / "wake.state").exists()
    assert "termux-wake-lock not found" in capsys.readouterr().err


def test_wake_status_reports_state(wake_env, capsys):
    assert main(["wake", "status"]) == 0
    assert "not held" in capsys.readouterr().out
    assert main(["wake", "on"]) == 0
    capsys.readouterr()
    assert main(["wake", "status"]) == 0
    assert "held by tuxcomp" in capsys.readouterr().out


def test_release_wake_lock_if_idle(wake_env, monkeypatch):
    home, calls = wake_env
    assert main(["wake", "on"]) == 0
    calls.clear()

    monkeypatch.setattr(cli, "_running_containers", lambda: {"still-up"})
    cli._release_wake_lock_if_idle()
    assert calls == []
    assert (home / ".tuxcomp" / "wake.state").exists()

    monkeypatch.setattr(cli, "_running_containers", set)
    cli._release_wake_lock_if_idle()
    assert calls == [["termux-wake-unlock"]]
    assert not (home / ".tuxcomp" / "wake.state").exists()


def test_project_keep_awake_parsed(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "services:\n"
        "  app:\n"
        "    image: app:latest\n"
        "x-tuxcomp:\n"
        "  keep_awake: true\n",
        encoding="utf-8",
    )
    project = parse_compose_file(compose)
    assert project.tuxcomp is not None
    assert project.tuxcomp.keep_awake is True


def test_project_keep_awake_defaults_false(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "services:\n  app:\n    image: app:latest\nx-tuxcomp:\n  deploy:\n    sync: []\n",
        encoding="utf-8",
    )
    project = parse_compose_file(compose)
    assert project.tuxcomp is not None
    assert project.tuxcomp.keep_awake is False
