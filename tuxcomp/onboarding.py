"""SSH key bring-up for ``tuxcomp remote bootstrap`` / ``remote test``.

The planting step is deliberately the classic ``ssh-copy-id`` approach: the
public key is piped in on stdin while OpenSSH itself prompts the user for the
password on the terminal. That works on Windows, macOS and Linux without
depending on ``ssh-copy-id`` or ``sshpass``, and Python never sees the
password.
"""

from __future__ import annotations

import os
import subprocess
import sys


def default_key_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".ssh", "id_ed25519")


def ensure_keypair(private: str, comment: str = "tuxcomp") -> int:
    """Create an ed25519 keypair at ``private`` when missing."""
    public = private + ".pub"
    if os.path.exists(private) and os.path.exists(public):
        print(f"  key      {private} (exists)")
        return 0
    parent = os.path.dirname(private)
    if parent:
        os.makedirs(parent, exist_ok=True)
    print(f"  → creating SSH key {private}")
    try:
        proc = subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", comment, "-f", private],
            check=False,
        )
    except OSError as exc:
        print(f"error: could not run ssh-keygen: {exc}", file=sys.stderr)
        return 1
    if proc.returncode != 0 or not os.path.exists(public):
        print("error: ssh-keygen failed", file=sys.stderr)
        return 1
    return 0


def plant_key(host: str, port: int, private: str) -> int:
    """First contact: pipe the public key in; ssh prompts for the password."""
    public = private + ".pub"
    remote = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        "cat >> ~/.ssh/authorized_keys && "
        "sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys && "
        "chmod 600 ~/.ssh/authorized_keys && echo tuxcomp-key-planted"
    )
    cmd = [
        "ssh", "-o", "StrictHostKeyChecking=accept-new",
        "-i", private, "-p", str(port), host, remote,
    ]
    print(
        f"  $ ssh -o StrictHostKeyChecking=accept-new -i {private} "
        f"-p {port} {host} '<plant key>'"
    )
    print("    (enter the phone's SSH password when prompted)")
    try:
        with open(public, "rb") as handle:
            proc = subprocess.run(cmd, stdin=handle, check=False)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        print(
            "error: could not plant the key (wrong password, or sshd not running)",
            file=sys.stderr,
        )
        return 1
    return 0


def verify_key(host: str, port: int, private: str) -> bool:
    """True when the key logs in without any password prompt."""
    cmd = [
        "ssh", "-q",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        "-i", private,
        "-p", str(port), host, "echo tuxcomp-key-ok",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.stdout.strip() == "tuxcomp-key-ok"


def hardening_notes(host: str, port: int) -> str:
    return (
        "Key login works. Optional hardening (disables password auth):\n"
        f"  ssh -p {port} {host}\n"
        "  sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' "
        "$PREFIX/etc/ssh/sshd_config\n"
        "  pkill sshd; sshd\n"
        "Keep the current session open until a new one logs in without a password."
    )
