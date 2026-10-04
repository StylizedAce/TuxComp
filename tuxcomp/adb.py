"""Host-side `tuxcomp setup --adb` helpers.

A phone cannot apply these Android settings to itself, so the one-time tweaks
that keep the Termux app (and its TuxComp containers) alive and unsandboxed
must be applied from a host with adb access. This module finds adb wherever it
is (no Android Studio required), resolves the target device, and applies the
community-standard settings:

- ``settings put global settings_enable_monitor_phantom_procs false`` disables
  the Android 12L+ phantom-process killer (and the excessive-CPU killer) where
  the ROM honors it.
- ``device_config put activity_manager max_phantom_processes 2147483647``
  disables trimming of forked app processes above 32 on Android 12.
- ``device_config put activity_manager max_cached_processes 256`` keeps more
  background apps cached so Termux is less likely to be evicted (Android 10+).
- ``device_config put activity_manager max_empty_time_millis 43200000``
  extends how long empty cached processes survive (12 hours).
- ``device_config set_sync_disabled_for_tests persistent`` stops Google Play
  Services from remotely reverting the device_config values.

Dry-run is the default; changes require ``--apply``. ``--restart-termux``
force-stops the Termux app afterwards (that kills every running container, so
it is never implicit). The tool reads the phone's Android release and skips
settings that do not exist on that version instead of reporting them as
failures.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def _exe() -> str:
    return "adb.exe" if os.name == "nt" else "adb"


#: (label, adb args, minimum Android API level). None of these is fatal on its
#: own: Android versions differ and unknown keys are harmless no-ops.
COMMANDS: list[tuple[str, list[str], int]] = [
    (
        "stop Play Services from reverting these values (Android 12+)",
        ["shell", "device_config", "set_sync_disabled_for_tests", "persistent"],
        12,
    ),
    (
        "disable the Android 12L+ phantom-process killer",
        ["shell", "settings", "put", "global", "settings_enable_monitor_phantom_procs", "false"],
        0,
    ),
    (
        "raise the phantom-process cap (Android 12)",
        ["shell", "device_config", "put", "activity_manager", "max_phantom_processes", "2147483647"],
        0,
    ),
    (
        "keep more background apps cached (Android 10+)",
        ["shell", "device_config", "put", "activity_manager", "max_cached_processes", "256"],
        0,
    ),
    (
        "extend empty-process lifetime to 12h",
        ["shell", "device_config", "put", "activity_manager", "max_empty_time_millis", "43200000"],
        0,
    ),
]

#: Force-stop the Termux app so the settings fully apply (kills containers).
RESTART_COMMAND: tuple[str, list[str]] = (
    "restart Termux (stops every running container)",
    ["shell", "am", "force-stop", "com.termux"],
)

_INSTALL_HINT = (
    "error: adb not found.\n"
    "  TuxComp looks in PATH, $ANDROID_HOME/$ANDROID_SDK_ROOT, Android Studio's SDK\n"
    "  and the common platform-tools folders. Android Studio is not required:\n"
    "    Windows:  scoop install adb      (or) choco install adb\n"
    "    macOS:    brew install android-platform-tools\n"
    "    Linux:    sudo apt install adb   (or) sudo dnf install android-tools\n"
    "  Then enable Developer options -> USB debugging and run `adb devices` once."
)


def _sdk_candidates() -> list[str]:
    candidates: list[str] = []
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(var)
        if root:
            candidates.append(os.path.join(root, "platform-tools", _exe()))
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(os.path.join(local, "Android", "Sdk", "platform-tools", "adb.exe"))
        profile = os.environ.get("USERPROFILE")
        if profile:
            candidates.extend(
                [
                    os.path.join(profile, "scoop", "shims", "adb.exe"),
                    os.path.join(profile, "AppData", "Local", "Android", "Sdk", "platform-tools", "adb.exe"),
                ]
            )
        candidates.extend(
            [
                "C:\\Android\\platform-tools\\adb.exe",
                "C:\\platform-tools\\adb.exe",
                "C:\\Program Files (x86)\\Android\\android-sdk\\platform-tools\\adb.exe",
                "C:\\ProgramData\\chocolatey\\bin\\adb.exe",
            ]
        )
    else:
        candidates.extend(
            [
                os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
                os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
                "/usr/lib/android-sdk/platform-tools/adb",
                "/opt/android-sdk/platform-tools/adb",
                "/opt/homebrew/bin/adb",
                "/usr/local/bin/adb",
                "/snap/bin/adb",
            ]
        )
    return candidates


def find_adb() -> str | None:
    """Locate adb: PATH first, then SDK env vars, then common install dirs."""
    found = shutil.which("adb")
    if found:
        return found
    for path in _sdk_candidates():
        if path and os.path.exists(path):
            return path
    return None


def _run(adb: str, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([adb, *args], capture_output=True, text=True, check=False)


def list_devices(adb: str) -> dict[str, str]:
    """serial -> state ('device', 'unauthorized', 'offline', ...)."""
    proc = _run(adb, ["devices"])
    devices: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] in ("*", "List"):
            continue
        devices[parts[0]] = parts[1]
    return devices


def resolve_device(adb: str) -> tuple[str | None, str | None]:
    """Return (serial, error). The ANDROID_SERIAL env var wins when set."""
    env_serial = (os.environ.get("ANDROID_SERIAL") or "").strip()
    devices = list_devices(adb)
    if env_serial:
        state = devices.get(env_serial)
        if state == "device":
            return env_serial, None
        return None, f"ANDROID_SERIAL={env_serial} is not in 'device' state ({state or 'not found'})"
    ready = [serial for serial, state in devices.items() if state == "device"]
    if len(ready) == 1:
        return ready[0], None
    if len(ready) > 1:
        return None, (
            "multiple devices connected - set ANDROID_SERIAL to choose one "
            f"({', '.join(ready)})"
        )
    unauthorized = [serial for serial, state in devices.items() if state == "unauthorized"]
    if unauthorized:
        return None, "device is 'unauthorized' - accept the USB debugging prompt on the phone"
    return None, "no device detected - connect USB / enable wireless debugging and check `adb devices`"


def _model(adb: str, serial: str) -> str:
    proc = _run(adb, ["-s", serial, "shell", "getprop", "ro.product.model"])
    return proc.stdout.strip() or "connected device"


def _android_version(adb: str, serial: str) -> int | None:
    proc = _run(adb, ["-s", serial, "shell", "getprop", "ro.build.version.release"])
    try:
        return int(proc.stdout.strip().split(".")[0])
    except (ValueError, IndexError):
        return None


def setup_adb(apply_changes: bool = False, restart_termux: bool = False) -> int:
    print("tuxcomp setup --adb")
    adb = find_adb()
    if not adb:
        print(_INSTALL_HINT, file=sys.stderr)
        return 1
    print(f"  adb     {adb}")

    serial, error = resolve_device(adb)
    version: int | None = None
    if serial:
        print(f"  device  {_model(adb, serial)} ({serial})")
        version = _android_version(adb, serial)
        if version is not None:
            print(f"  android {version}")
    else:
        print(f"  device  {error}")

    planned = [(label, args) for label, args, _ in COMMANDS]
    if restart_termux:
        planned.append(RESTART_COMMAND)
    print("  changes:" if apply_changes else "  planned changes (dry-run):")
    for label, args in planned:
        print(f"    adb {' '.join(args):<68} # {label}")

    if not apply_changes:
        print("nothing changed. Re-run with --apply to execute.")
        if not restart_termux:
            print("Tip: add --restart-termux to force-stop Termux afterwards (stops containers).")
        return 0

    if not serial:
        print(f"error: {error} - nothing applied", file=sys.stderr)
        return 1

    applied = 0
    skipped = 0
    executed = 0
    for label, args, min_android in COMMANDS:
        if min_android and version is not None and version < min_android:
            skipped += 1
            print(f"  [skip] {label}")
            continue
        executed += 1
        proc = _run(adb, ["-s", serial, *args])
        ok = proc.returncode == 0
        applied += 1 if ok else 0
        print(f"  [{'ok' if ok else 'FAIL'}] {label}")

    if restart_termux:
        proc = _run(adb, ["-s", serial, *RESTART_COMMAND[1]])
        if proc.returncode == 0:
            print("  [ok] Termux force-stopped")
            print("Reopen Termux on the phone, then run: tuxcomp up --all")
        else:
            print("  [FAIL] could not force-stop Termux", file=sys.stderr)
    else:
        print("restart Termux (or reboot the phone) so the cached-process changes fully apply.")

    suffix = f"; {skipped} skipped (Android-version specific)" if skipped else ""
    if executed and applied == 0:
        print("error: none of the settings applied - check `adb devices` and USB debugging", file=sys.stderr)
        return 1
    print(f"applied {applied}/{executed} settings{suffix}.")
    return 0
