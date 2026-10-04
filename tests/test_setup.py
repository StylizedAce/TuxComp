import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tuxcomp import doctor as doctor_mod
from tuxcomp import posture
from tuxcomp.cli import main


def _data(**overrides):
    data = {
        "android": "13",
        "model": "Redmi",
        "allowed_count": 4,
        "total_count": 8,
        "missing_clusters": [{"cpus_text": "4-7", "max_khz": 2600000, "label": "prime"}],
        "process_count": 12,
        "phantom": "false",
        "wake_lock": True,
        "wake_lock_available": True,
    }
    data.update(overrides)
    return data


def _by_name(checks):
    return {check.name: check for check in checks}


def test_android10_has_no_phantom_check_and_completes():
    checks = posture.run_checks(_data(android="10", phantom=None))
    by_name = _by_name(checks)
    assert by_name["phantom-process killing"].status == posture.STATUS_NA
    assert by_name["process budget"].status == posture.STATUS_NA
    assert by_name["wake lock"].status == posture.STATUS_PASS
    assert by_name["CPU posture"].status == posture.STATUS_WARN
    assert posture.is_complete(checks)


def test_phantom_active_fails_setup():
    checks = posture.run_checks(_data(phantom="true"))
    assert _by_name(checks)["phantom-process killing"].status == posture.STATUS_FAIL
    assert not posture.is_complete(checks)
    assert "Developer options" in _by_name(checks)["phantom-process killing"].fix


def test_phantom_unreadable_is_unverified_not_failed():
    checks = posture.run_checks(_data(phantom=None))
    assert _by_name(checks)["phantom-process killing"].status == posture.STATUS_UNVERIFIED
    assert posture.is_complete(checks)


def test_wake_lock_missing_fails():
    checks = posture.run_checks(_data(wake_lock=False))
    assert _by_name(checks)["wake lock"].status == posture.STATUS_FAIL
    assert not posture.is_complete(checks)


def test_wake_lock_unavailable_is_na():
    checks = posture.run_checks(_data(wake_lock=False, wake_lock_available=False))
    assert _by_name(checks)["wake lock"].status == posture.STATUS_NA


def test_process_budget_warns_when_killer_active_and_count_high():
    checks = posture.run_checks(_data(phantom="true", process_count=26))
    assert _by_name(checks)["process budget"].status == posture.STATUS_WARN


def test_all_cpus_pass_and_no_warn():
    checks = posture.run_checks(_data(allowed_count=8, missing_clusters=[]))
    assert _by_name(checks)["CPU posture"].status == posture.STATUS_PASS


def test_format_checks_reports_failure():
    text = posture.format_checks(posture.run_checks(_data(phantom="true")))
    assert "setup: NOT COMPLETED" in text
    assert "[FAIL]" in text


def test_setup_cli_exit_codes(monkeypatch, capsys):
    monkeypatch.setattr(
        doctor_mod, "collect_device", lambda installed=None, running=None: _data(phantom="true")
    )
    monkeypatch.setattr("tuxcomp.cli._installed_containers", list)
    monkeypatch.setattr("tuxcomp.cli._running_containers", set)
    assert main(["setup"]) == 1
    assert "NOT COMPLETED" in capsys.readouterr().out


def test_setup_cli_json_complete(monkeypatch, capsys):
    monkeypatch.setattr(
        doctor_mod, "collect_device", lambda installed=None, running=None: _data()
    )
    monkeypatch.setattr("tuxcomp.cli._installed_containers", list)
    monkeypatch.setattr("tuxcomp.cli._running_containers", set)
    assert main(["setup", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["complete"] is True
    assert any(check["name"] == "wake lock" for check in payload["checks"])
