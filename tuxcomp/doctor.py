"""`tuxcomp doctor` — read-only device status for phone containers.

Everything here is best-effort and must never raise on a missing file or a
denied probe: phones vary wildly, and a diagnostic that crashes is useless.
Parsers are pure functions so the test suite can exercise every branch.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
from typing import Any

#: Android version at which the phantom process killer exists.
PHANTOM_ANDROID_VERSION = 12
#: Total forked-process budget before Android 12+ starts killing (default 32).
PHANTOM_PROCESS_BUDGET = 32
#: Warn when the app's own process count gets this close to the budget.
PHANTOM_WARN_AT = 24

_CGROUP_RE = re.compile(r"^\d+:(?P<controllers>[^:]*):(?P<path>.*)$")


def _run(cmd: list[str], timeout: int = 5) -> str | None:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip()


def _read(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


# ── parsers (pure) ─────────────────────────────────────────────────────────


def parse_cpuset_group(cgroup_text: str | None) -> str | None:
    """Group name from /proc/self/cgroup, e.g. 'background'."""
    if not cgroup_text:
        return None
    for line in cgroup_text.splitlines():
        match = _CGROUP_RE.match(line.strip())
        if not match:
            continue
        if "cpuset" in match.group("controllers").split(","):
            return match.group("path").strip("/") or "root"
    return None


def parse_cpus_allowed(status_text: str | None) -> list[int]:
    """CPU list from the `Cpus_allowed_list:` line of /proc/self/status."""
    if not status_text:
        return []
    for line in status_text.splitlines():
        if line.startswith("Cpus_allowed_list:"):
            return parse_cpu_list(line.split(":", 1)[1].strip())
    return []


def parse_cpu_list(text: str) -> list[int]:
    cpus: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, _, end = part.partition("-")
            if start.isdigit() and end.isdigit():
                cpus.extend(range(int(start), int(end) + 1))
        elif part.isdigit():
            cpus.append(int(part))
    return cpus


def format_ranges(cpus: list[int]) -> str:
    """[0,1,2,3,6,7] -> '0-3,6-7'."""
    if not cpus:
        return "-"
    ordered = sorted(set(cpus))
    ranges: list[str] = []
    start = prev = ordered[0]
    for cpu in ordered[1:]:
        if cpu == prev + 1:
            prev = cpu
            continue
        ranges.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = cpu
    ranges.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(ranges)


def clusterize(cpu_freqs: dict[int, int]) -> list[dict[str, Any]]:
    """Group CPUs by maximum frequency into clusters (little/mid/prime)."""
    by_freq: dict[int, list[int]] = {}
    for cpu, freq in cpu_freqs.items():
        by_freq.setdefault(freq, []).append(cpu)
    freqs = sorted(by_freq)
    clusters: list[dict[str, Any]] = []
    for freq in freqs:
        if len(freqs) == 1:
            label = ""
        elif freq == freqs[0]:
            label = "little"
        elif freq == freqs[-1]:
            label = "prime"
        else:
            label = "mid"
        clusters.append(
            {
                "cpus": sorted(by_freq[freq]),
                "cpus_text": format_ranges(by_freq[freq]),
                "max_khz": freq,
                "label": label,
            }
        )
    return clusters


def parse_meminfo(text: str | None) -> dict[str, int | None]:
    fields = {
        "MemTotal": "mem_total_kb",
        "MemAvailable": "mem_available_kb",
        "SwapTotal": "swap_total_kb",
        "SwapFree": "swap_free_kb",
    }
    result: dict[str, int | None] = {name: None for name in fields.values()}
    if not text:
        return result
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if key in fields:
            value = rest.strip().split(" ", 1)[0]
            if value.isdigit():
                result[fields[key]] = int(value)
    return result


def parse_user_process_count(ps_output: str | None, user: str) -> int | None:
    """Count processes owned by `user` from `ps -A -o USER` output."""
    if ps_output is None:
        return None
    count = 0
    for line in ps_output.splitlines()[1:]:
        stripped = line.strip()
        if stripped and stripped.split()[0] == user:
            count += 1
    return count


def parse_phantom_value(settings_output: str | None) -> str | None:
    """'true' / 'false' when the ROM exposes the setting, else None."""
    if settings_output is None:
        return None
    value = settings_output.strip().lower()
    if value in ("true", "false"):
        return value
    return None


def parse_proot_containers(cmdlines: dict[int, bytes]) -> dict[int, str]:
    """Map pid -> container name for running proot-distro sessions."""
    found: dict[int, str] = {}
    for pid, raw in cmdlines.items():
        text = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
        if "proot" not in text or "containers" not in text:
            continue
        match = re.search(r"containers[/\\]([\w.-]+)", text)
        if match:
            found[pid] = match.group(1)
    return found


# ── collectors ─────────────────────────────────────────────────────────────


def _collect_clusters(sysfs: str = "/sys/devices/system/cpu") -> list[dict[str, Any]]:
    freqs: dict[int, int] = {}
    if not os.path.isdir(sysfs):
        return []
    for entry in os.listdir(sysfs):
        if not entry.startswith("cpu") or not entry[3:].isdigit():
            continue
        text = _read(os.path.join(sysfs, entry, "cpufreq", "cpuinfo_max_freq"))
        if text and text.strip().isdigit():
            freqs[int(entry[3:])] = int(text.strip())
    return clusterize(freqs)


def _collect_process_count() -> int | None:
    user = _run(["id", "-un"])
    if user:
        count = parse_user_process_count(_run(["ps", "-A", "-o", "USER"]), user)
        if count is not None:
            return count
    # Fallback: count /proc entries (approximate, includes other UIDs if readable)
    try:
        return sum(1 for entry in os.listdir("/proc") if entry.isdigit())
    except OSError:
        return None


def _collect_orphans(installed: list[str]) -> list[str]:
    pids = {}
    try:
        entries = [e for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return []
    for entry in entries:
        raw = None
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                raw = handle.read()
        except OSError:
            continue
        if raw:
            pids[int(entry)] = raw
    containers = parse_proot_containers(pids)
    known = set(installed)
    return sorted({name for name in containers.values() if name not in known})


def _marker_path() -> str:
    home = os.environ.get("TUXCOMP_HOME") or os.environ.get("HOME")
    root = os.path.join(home, ".tuxcomp") if home else os.path.expanduser("~/.tuxcomp")
    return os.path.join(root, "wake.state")


def collect_device(
    installed: list[str] | None = None,
    running: list[str] | None = None,
) -> dict[str, Any]:
    installed = installed or []
    running = running or []

    cpus = parse_cpus_allowed(_read("/proc/self/status"))
    try:
        allowed = sorted(os.sched_getaffinity(0))
    except AttributeError:  # pragma: no cover - non-Linux dev machines
        allowed = cpus or list(range(os.cpu_count() or 1))
    clusters = _collect_clusters()
    missing = [c for c in clusters if not set(c["cpus"]) & set(allowed)]

    mem = parse_meminfo(_read("/proc/meminfo"))
    android = _run(["getprop", "ro.build.version.release"])
    phantom = parse_phantom_value(
        _run(["settings", "get", "global", "settings_enable_monitor_phantom_procs"])
    )

    return {
        "tuxcomp_version": _version(),
        "android": android,
        "model": _run(["getprop", "ro.product.model"]),
        "platform": platform.machine(),
        "cpuset_group": parse_cpuset_group(_read("/proc/self/cgroup")),
        "allowed_cpus": format_ranges(allowed),
        "allowed_count": len(allowed),
        "total_count": max(len({c for cl in clusters for c in cl["cpus"]}), len(allowed)),
        "clusters": [
            {k: v for k, v in cluster.items() if k != "cpus"} for cluster in clusters
        ],
        "missing_clusters": [
            {k: v for k, v in cluster.items() if k != "cpus"} for cluster in missing
        ],
        "governor": (_read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor") or "").strip()
        or None,
        **mem,
        "process_count": _collect_process_count(),
        "phantom": phantom,
        "wake_lock": os.path.exists(_marker_path()),
        "wake_lock_available": shutil.which("termux-wake-lock") is not None,
        "containers_installed": sorted(installed),
        "containers_running": sorted(running),
        "orphans": _collect_orphans(installed),
    }


def _version() -> str:
    try:
        from tuxcomp import __version__

        return __version__
    except ImportError:  # pragma: no cover
        return "unknown"


# ── report ─────────────────────────────────────────────────────────────────


def _android_at_least(data: dict[str, Any], version: int) -> bool:
    raw = data.get("android")
    try:
        return raw is not None and int(str(raw).split(".")[0]) >= version
    except (TypeError, ValueError):
        return False


def _fmt_khz(khz: int) -> str:
    return f"{khz / 1_000_000:.2f} GHz" if khz >= 1_000_000 else f"{khz} kHz"


def warnings(data: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    allowed = data.get("allowed_count") or 0
    total = data.get("total_count") or 0
    if total and allowed < total:
        missing = ", ".join(
            f"{c['cpus_text']} ({_fmt_khz(c['max_khz'])})"
            for c in data.get("missing_clusters", [])
        )
        detail = f" (locked: {missing})" if missing else ""
        issues.append(
            f"Only {allowed} of {total} CPUs are allowed while Termux is backgrounded{detail}. "
            "Keep the screen on with Termux visible for full speed."
        )
    if _android_at_least(data, PHANTOM_ANDROID_VERSION):
        if data.get("phantom") == "true":
            issues.append(
                "Android phantom-process killing is active; run `tuxcomp setup` for the check."
            )
        count = data.get("process_count")
        if count is not None and data.get("phantom") != "false" and count >= PHANTOM_WARN_AT:
            issues.append(
                f"{count} processes in this app UID — close to the ~{PHANTOM_PROCESS_BUDGET} "
                "forked-process budget Android 12+ enforces."
            )
    if data.get("orphans"):
        issues.append(
            "Orphan proot session(s): "
            + ", ".join(data["orphans"])
            + " — they are not registered; stop them with pkill or reboot."
        )
    if not data.get("wake_lock") and data.get("wake_lock_available"):
        issues.append(
            "Wake lock is not held by tuxcomp; run `termux-wake-lock` (or `tuxcomp wake on`)."
        )
    if data.get("android") is None:
        issues.append(
            "Android probes are unavailable (not running on Termux/Android?). "
            "Values above may be incomplete."
        )
    return issues


def format_report(data: dict[str, Any]) -> str:
    lines: list[str] = []
    name = data.get("model") or "unknown device"
    android = data.get("android") or "?"
    lines.append(f"tuxcomp {data.get('tuxcomp_version', '?')} — {name}, Android {android}")
    lines.append(f"  cpuset group   {data.get('cpuset_group') or '-'}")
    lines.append(
        f"  CPUs           {data.get('allowed_count')} of {data.get('total_count')} allowed "
        f"({data.get('allowed_cpus')})"
    )
    clusters = data.get("clusters") or []
    if clusters:
        described = ", ".join(
            f"{c['cpus_text']}" + (f" {c['label']}" if c["label"] else "") + f" @ {_fmt_khz(c['max_khz'])}"
            for c in clusters
        )
        lines.append(f"  clusters       {described}")
    if data.get("governor"):
        lines.append(f"  governor       {data['governor']}")
    mem_total = data.get("mem_total_kb")
    mem_avail = data.get("mem_available_kb")
    swap_free = data.get("swap_free_kb")
    if mem_total and mem_avail is not None:
        mem_line = f"  memory         {mem_avail / 1048576:.1f} GiB free of {mem_total / 1048576:.1f} GiB"
        if swap_free is not None:
            mem_line += f", swap {swap_free / 1048576:.1f} GiB free"
        lines.append(mem_line)
    if data.get("process_count") is not None:
        budget = (
            f" (budget ~{PHANTOM_PROCESS_BUDGET} on Android 12+)"
            if _android_at_least(data, PHANTOM_ANDROID_VERSION)
            else ""
        )
        lines.append(f"  processes      {data['process_count']} in this app UID{budget}")
    if _android_at_least(data, PHANTOM_ANDROID_VERSION):
        phantom = data.get("phantom")
        phantom_text = {"true": "active", "false": "disabled"}.get(phantom or "", "unavailable")
        lines.append(f"  phantom procs  {phantom_text}")
    wake = "held by tuxcomp" if data.get("wake_lock") else "not held"
    available = "termux-wake-lock available" if data.get("wake_lock_available") else "termux-wake-lock missing"
    lines.append(f"  wake lock      {wake} ({available})")
    lines.append(
        f"  containers     {len(data.get('containers_installed') or [])} installed, "
        f"{len(data.get('containers_running') or [])} running"
    )
    lines.append(f"  orphan proot   {len(data.get('orphans') or [])}")

    issues = warnings(data)
    if issues:
        lines.append("")
        lines.append("Warnings")
        for issue in issues:
            lines.append(f"  ! {issue}")
    return "\n".join(lines)


def doctor_json(
    installed: list[str] | None = None,
    running: list[str] | None = None,
) -> str:
    return json.dumps(collect_device(installed=installed, running=running), indent=2)
