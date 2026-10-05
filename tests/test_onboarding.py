import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from tuxcomp import cli, onboarding


class FakeProc:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def test_ensure_keypair_existing(tmp_path, capsys):
    private = tmp_path / "id_ed25519"
    private.write_text("PRIVATE", encoding="utf-8")
    (tmp_path / "id_ed25519.pub").write_text("PUB", encoding="utf-8")
    assert onboarding.ensure_keypair(str(private)) == 0
    assert "exists" in capsys.readouterr().out


def test_ensure_keypair_creates(tmp_path, monkeypatch, capsys):
    private = tmp_path / "sub" / "id_ed25519"
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        Path(cmd[-1]).write_text("PRIVATE", encoding="utf-8")
        Path(cmd[-1] + ".pub").write_text("PUB", encoding="utf-8")
        return FakeProc()

    monkeypatch.setattr(onboarding.subprocess, "run", fake_run)
    assert onboarding.ensure_keypair(str(private)) == 0
    assert private.exists() and (tmp_path / "sub" / "id_ed25519.pub").exists()
    assert calls[0][:4] == ["ssh-keygen", "-t", "ed25519", "-N"]


def test_ensure_keypair_failure(tmp_path, monkeypatch, capsys):
    private = tmp_path / "id_ed25519"
    monkeypatch.setattr(onboarding.subprocess, "run", lambda cmd, **kw: FakeProc(returncode=1))
    assert onboarding.ensure_keypair(str(private)) == 1
    assert "ssh-keygen failed" in capsys.readouterr().err


def test_plant_key_pipes_pubkey(tmp_path, monkeypatch):
    private = tmp_path / "id_ed25519"
    private.write_text("PRIVATE", encoding="utf-8")
    (tmp_path / "id_ed25519.pub").write_text("ssh-ed25519 AAAA test\n", encoding="utf-8")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["stdin_content"] = kwargs["stdin"].read()
        return FakeProc()

    monkeypatch.setattr(onboarding.subprocess, "run", fake_run)
    assert onboarding.plant_key("root@1.2.3.4", 8022, str(private)) == 0
    assert "ssh" in captured["cmd"] and "root@1.2.3.4" in captured["cmd"]
    assert captured["stdin_content"].replace(b"\r\n", b"\n") == b"ssh-ed25519 AAAA test\n"


def test_verify_key(monkeypatch):
    monkeypatch.setattr(
        onboarding.subprocess,
        "run",
        lambda cmd, **kw: FakeProc(stdout="tuxcomp-key-ok\n"),
    )
    assert onboarding.verify_key("root@1.2.3.4", 8022, "/k") is True

    monkeypatch.setattr(
        onboarding.subprocess,
        "run",
        lambda cmd, **kw: FakeProc(stdout=""),
    )
    assert onboarding.verify_key("root@1.2.3.4", 8022, "/k") is False


@pytest.fixture
def remotes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TUXCOMP_HOME", str(tmp_path))
    assert cli.main(
        ["remote", "add", "phone", "--host", "root@10.0.0.9", "--port", "8022", "--default"]
    ) == 0
    return tmp_path


def test_remote_test_ok(remotes_home, monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "_ssh_batch_out",
        lambda host, port, cmd, timeout=20: "tuxcomp-ssh-ok\ntuxcomp 0.8.28\nVOG-L29",
    )
    assert cli.main(["remote", "test"]) == 0
    out = capsys.readouterr().out
    assert "ssh key auth" in out
    assert "tuxcomp 0.8.28" in out and "VOG-L29" in out


def test_remote_test_fails_without_key_auth(remotes_home, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_ssh_batch_out", lambda *a, **k: "")
    assert cli.main(["remote", "test", "phone"]) == 1
    assert "remote bootstrap" in capsys.readouterr().err


def test_remote_bootstrap_flow(remotes_home, monkeypatch, capsys):
    calls = []

    def fake_ensure(private, comment="tuxcomp"):
        calls.append(("ensure", private))
        return 0

    def fake_plant(host, port, private):
        calls.append(("plant", host, port))
        return 0

    def fake_verify(host, port, private):
        calls.append(("verify",))
        return True

    monkeypatch.setattr(onboarding, "ensure_keypair", fake_ensure)
    monkeypatch.setattr(onboarding, "plant_key", fake_plant)
    monkeypatch.setattr(onboarding, "verify_key", fake_verify)
    assert cli.main(["remote", "bootstrap", "phone", "--key", "~/.ssh/mykey"]) == 0
    out = capsys.readouterr().out
    assert "key login verified" in out
    assert "PasswordAuthentication no" in out
    assert calls[0][0] == "ensure" and calls[0][1].endswith("mykey")
    assert calls[1] == ("plant", "root@10.0.0.9", 8022)
    assert calls[2] == ("verify",)


def test_remote_bootstrap_unknown_remote(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TUXCOMP_HOME", str(tmp_path))
    assert cli.main(["remote", "bootstrap", "ghost"]) == 1
    assert "unknown remote" in capsys.readouterr().err
