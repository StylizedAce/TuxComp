import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tuxcomp import doctor as doctor_mod
from tuxcomp.cli import main


def test_parse_cpuset_group():
    text = "10:pids:/\n4:cpuset:/background\n0::/\n"
    assert doctor_mod.parse_cpuset_group(text) == "background"
    assert doctor_mod.parse_cpuset_group(None) is None


def test_parse_cpus_allowed_and_ranges():
    status = "Name:\tbash\nCpus_allowed_list:\t0-3\n"
    assert doctor_mod.parse_cpus_allowed(status) == [0, 1, 2, 3]
    assert doctor_mod.parse_cpus_allowed(None) == []
    assert doctor_mod.format_ranges([0, 1, 2, 3, 6, 7]) == "0-3,6-7"
    assert doctor_mod.format_ranges([2]) == "2"
    assert doctor_mod.format_ranges([]) == "-"


def test_clusterize_groups_and_labels():
    clusters = doctor_mod.clusterize({0: 1800000, 1: 1800000, 4: 2600000, 5: 2600000})
    assert [c["label"] for c in clusters] == ["little", "prime"]
    assert clusters[-1]["cpus_text"] == "4-5"
    assert doctor_mod.clusterize({0: 1000})[0]["label"] == ""


def test_parse_meminfo():
    text = (
        "MemTotal:        5743076 kB\n"
        "MemAvailable:    2646676 kB\n"
        "SwapTotal:       2867196 kB\n"
        "SwapFree:        1689652 kB\n"
    )
    mem = doctor_mod.parse_meminfo(text)
    assert mem["mem_total_kb"] == 5743076
    assert mem["mem_available_kb"] == 2646676
    assert mem["swap_free_kb"] == 1689652
    assert doctor_mod.parse_meminfo(None)["mem_total_kb"] is None


def test_parse_user_process_count():
    ps = "USER PID PPID\nroot 1 0\nu0_a212 10 1\nu0_a212 11 1\nu0_a99 12 1\n"
    assert doctor_mod.parse_user_process_count(ps, "u0_a212") == 2
    assert doctor_mod.parse_user_process_count(None, "u0_a212") is None


def test_parse_phantom_value():
    assert doctor_mod.parse_phantom_value("false") == "false"
    assert doctor_mod.parse_phantom_value("true\n") == "true"
    assert doctor_mod.parse_phantom_value("null") is None
    assert doctor_mod.parse_phantom_value(None) is None


def test_parse_proot_containers():
    cmd = (
        b"proot --kill-on-exit --rootfs=/data/data/com.termux/usr/var/lib/"
        b"proot-distro/containers/lt-api /bin/sh\x00-c\x00x"
    )
    assert doctor_mod.parse_proot_containers({123: cmd}) == {123: "lt-api"}
    assert doctor_mod.parse_proot_containers({1: b"bash\x00"}) == {}


def _fixture(**overrides):
    data = {
        "tuxcomp_version": "0.8.18",
        "android": "10",
        "model": "VOG-L29",
        "platform": "aarch64",
        "cpuset_group": "background",
        "allowed_cpus": "0-3",
        "allowed_count": 4,
        "total_count": 8,
        "clusters": [
            {"cpus_text": "0-3", "max_khz": 1844000, "label": "little"},
            {"cpus_text": "4-5", "max_khz": 1920000, "label": "mid"},
            {"cpus_text": "6-7", "max_khz": 2600000, "label": "prime"},
        ],
        "missing_clusters": [
            {"cpus_text": "6-7", "max_khz": 2600000, "label": "prime"},
        ],
        "governor": "schedutil",
        "mem_total_kb": 5743076,
        "mem_available_kb": 2646676,
        "swap_total_kb": 2867196,
        "swap_free_kb": 1689652,
        "process_count": 19,
        "phantom": None,
        "wake_lock": False,
        "wake_lock_available": True,
        "containers_installed": ["lt-api", "lt-redis"],
        "containers_running": ["lt-api"],
        "orphans": [],
    }
    data.update(overrides)
    return data


def test_warnings_background_cores_and_wake_lock():
    issues = doctor_mod.warnings(_fixture())
    assert any("Only 4 of 8 CPUs" in issue for issue in issues)
    assert any("Wake lock" in issue for issue in issues)
    assert not any("phantom" in issue.lower() for issue in issues)


def test_warnings_android12_phantom_and_budget():
    issues = doctor_mod.warnings(_fixture(android="13", phantom="true", process_count=26))
    joined = " ".join(issues)
    assert "phantom-process killing is active" in joined
    assert "26 processes" in joined


def test_warnings_phantom_disabled_skips_budget():
    issues = doctor_mod.warnings(_fixture(android="13", phantom="false", process_count=26))
    joined = " ".join(issues)
    assert "phantom-process killing is active" not in joined
    assert "26 processes" not in joined


def test_warnings_orphans_reported():
    issues = doctor_mod.warnings(_fixture(orphans=["ghost"]))
    assert any("Orphan proot" in issue for issue in issues)


def test_format_report_contains_sections():
    report = doctor_mod.format_report(_fixture(orphans=["ghost"]))
    assert "tuxcomp 0.8.18" in report
    assert "cpuset group   background" in report
    assert "orphan proot   1" in report
    assert "Warnings" in report


def test_doctor_cli_json(monkeypatch, capsys):
    monkeypatch.setattr(
        doctor_mod, "collect_device", lambda installed=None, running=None: _fixture()
    )
    monkeypatch.setattr("tuxcomp.cli._installed_containers", list)
    monkeypatch.setattr("tuxcomp.cli._running_containers", set)
    assert main(["doctor", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["android"] == "10"


def test_doctor_cli_human(monkeypatch, capsys):
    monkeypatch.setattr(
        doctor_mod, "collect_device", lambda installed=None, running=None: _fixture()
    )
    monkeypatch.setattr("tuxcomp.cli._installed_containers", list)
    monkeypatch.setattr("tuxcomp.cli._running_containers", set)
    assert main(["doctor"]) == 0
    assert "tuxcomp 0.8.18" in capsys.readouterr().out
