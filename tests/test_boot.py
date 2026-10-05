import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tuxcomp import cli


def test_boot_install_writes_script(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TUXCOMP_HOME", str(tmp_path))
    assert cli.main(["boot", "install"]) == 0
    script = tmp_path / ".termux" / "boot" / "00-tuxcomp"
    assert script.exists()
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!/data/data/com.termux/files/usr/bin/sh")
    assert "termux-wake-lock" in text
    assert "sshd" in text
    assert "tuxcomp up --all" in text
    assert "tuxcomp serve --wake-lock" in text
    assert "Termux:Boot" in capsys.readouterr().out


def test_boot_status_and_remove(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TUXCOMP_HOME", str(tmp_path))
    assert cli.main(["boot", "status"]) == 0
    assert "no boot script" in capsys.readouterr().out

    assert cli.main(["boot", "install"]) == 0
    capsys.readouterr()
    assert cli.main(["boot", "status"]) == 0
    assert "boot script" in capsys.readouterr().out

    assert cli.main(["boot", "remove"]) == 0
    assert not (tmp_path / ".termux" / "boot" / "00-tuxcomp").exists()
    assert cli.main(["boot", "remove"]) == 0


def test_boot_install_is_idempotent(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TUXCOMP_HOME", str(tmp_path))
    assert cli.main(["boot", "install"]) == 0
    assert cli.main(["boot", "install"]) == 0
    assert (tmp_path / ".termux" / "boot" / "00-tuxcomp").exists()
