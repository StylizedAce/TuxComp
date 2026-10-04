"""`tuxcomp setup` — pass/fail serving posture checks.

Only checks that can actually be verified from the CLI are reported; anything
the ROM hides is marked unverified instead of being turned into a manual
checklist. A failed check means the phone is not serving-ready yet.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from tuxcomp.doctor import PHANTOM_ANDROID_VERSION, PHANTOM_WARN_AT

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_WARN = "warn"
STATUS_UNVERIFIED = "unverified"
STATUS_NA = "n/a"

_MARKS = {
    STATUS_PASS: "[ok]  ",
    STATUS_FAIL: "[FAIL]",
    STATUS_WARN: "[warn]",
    STATUS_UNVERIFIED: "[?]   ",
    STATUS_NA: "[-]   ",
}


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str | None = None


def _android_at_least(data: dict[str, Any], version: int) -> bool:
    raw = data.get("android")
    try:
        return raw is not None and int(str(raw).split(".")[0]) >= version
    except (TypeError, ValueError):
        return False


def run_checks(data: dict[str, Any]) -> list[Check]:
    checks: list[Check] = []
    phantom_present = _android_at_least(data, PHANTOM_ANDROID_VERSION)

    # 1. Phantom-process killing (Android 12+)
    if not phantom_present:
        checks.append(
            Check(
                "phantom-process killing",
                STATUS_NA,
                "not present before Android 12",
            )
        )
    else:
        phantom = data.get("phantom")
        if phantom == "false":
            checks.append(
                Check("phantom-process killing", STATUS_PASS, "disabled on this device")
            )
        elif phantom == "true":
            checks.append(
                Check(
                    "phantom-process killing",
                    STATUS_FAIL,
                    "active - Android may kill forked container processes",
                    fix="Developer options -> Disable child process restrictions (Android 12L+)",
                )
            )
        else:
            checks.append(
                Check(
                    "phantom-process killing",
                    STATUS_UNVERIFIED,
                    "setting not readable on this ROM",
                    fix="Developer options -> Disable child process restrictions (Android 12L+)",
                )
            )

    # 2. Wake lock (keeps Android from freezing the app and its containers)
    if data.get("wake_lock"):
        checks.append(Check("wake lock", STATUS_PASS, "held by tuxcomp"))
    elif data.get("wake_lock_available"):
        checks.append(
            Check(
                "wake lock",
                STATUS_FAIL,
                "not held",
                fix="tuxcomp wake on",
            )
        )
    else:
        checks.append(
            Check("wake lock", STATUS_NA, "termux-wake-lock not available on this host")
        )

    # 3. Process budget (informational; only meaningful while the killer runs)
    count = data.get("process_count")
    if not phantom_present:
        checks.append(Check("process budget", STATUS_NA, "no phantom limit before Android 12"))
    elif count is None:
        checks.append(Check("process budget", STATUS_UNVERIFIED, "process count unavailable"))
    elif data.get("phantom") == "false":
        checks.append(Check("process budget", STATUS_NA, "killer disabled"))
    elif count >= PHANTOM_WARN_AT:
        checks.append(
            Check(
                "process budget",
                STATUS_WARN,
                f"{count} processes in this app UID (budget ~32)",
                fix="stop unused containers or disable the phantom killer",
            )
        )
    else:
        checks.append(Check("process budget", STATUS_PASS, f"{count} processes"))

    # 4. CPU posture (informational - background mode is valid, just slower)
    allowed = data.get("allowed_count") or 0
    total = data.get("total_count") or 0
    if not total or not allowed:
        checks.append(Check("CPU posture", STATUS_UNVERIFIED, "CPU topology unavailable"))
    elif allowed >= total:
        checks.append(Check("CPU posture", STATUS_PASS, f"all {total} CPUs available"))
    else:
        missing = ", ".join(
            c.get("cpus_text", "?") for c in data.get("missing_clusters", [])
        )
        detail = f"{allowed} of {total} CPUs available"
        if missing:
            detail += f" (locked: {missing})"
        checks.append(
            Check(
                "CPU posture",
                STATUS_WARN,
                detail,
                fix="keep the screen on with Termux visible for full speed",
            )
        )
    return checks


def is_complete(checks: list[Check]) -> bool:
    return not any(check.status == STATUS_FAIL for check in checks)


def format_checks(checks: list[Check]) -> str:
    lines = ["tuxcomp setup"]
    for check in checks:
        lines.append(f"  {_MARKS[check.status]} {check.name}: {check.detail}")
        if check.fix:
            lines.append(f"          -> {check.fix}")
    failures = [c for c in checks if c.status == STATUS_FAIL]
    if failures:
        lines.append("")
        lines.append(f"setup: NOT COMPLETED ({len(failures)} issue(s))")
    else:
        lines.append("")
        lines.append("setup: COMPLETE")
    return "\n".join(lines)


def checks_json(checks: list[Check]) -> str:
    import json

    return json.dumps(
        {"complete": is_complete(checks), "checks": [asdict(c) for c in checks]},
        indent=2,
    )
