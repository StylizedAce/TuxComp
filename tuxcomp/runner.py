from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from tuxcomp.planner import Plan

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


@dataclass
class RunResult:
    step: str
    service: str
    ok: bool
    output: str = ""
    skipped: bool = False


class Runner:
    def __init__(self, dry_run: bool = False, verbose: bool = False, proot: str = "proot-distro"):
        self.dry_run = dry_run
        self.verbose = verbose
        self.proot = proot
        self.results: list[RunResult] = []

    def _cmdline(self, command: list[str]) -> str:
        cmd = [self.proot if c == "proot-distro" else c for c in command]
        return " ".join(shlex.quote(c) for c in cmd)

    def _prefix(self, service: str, ok: bool) -> str:
        state = "DRY" if self.dry_run else ("OK " if ok else "FAIL")
        return f"[{state}] [{service}]"

    def _installed_container_names(self) -> set[str]:
        try:
            proc = subprocess.run(
                [self.proot, "list"], capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.TimeoutExpired):
            return set()
        ansi = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
        names: set[str] = set()
        for line in (proc.stdout + "\n" + proc.stderr).splitlines():
            line = ansi.sub("", line).strip()
            if line.startswith("* "):
                names.add(line[2:].strip())
        return names

    def _build_target(self, command: list[str]) -> str | None:
        """Extract the container name from a build step's --install-as flag."""
        for i, part in enumerate(command):
            if part == "--install-as" and i + 1 < len(command):
                return command[i + 1]
        return None

    def _rewrite_build_dockerfile(self, command: list[str]) -> None:
        """Rewrite the build's Dockerfile in place for proot-distro compat.

        Finds the `-f <dockerfile>` arg, runs the WORKDIR/COPY/RUN translator
        (tuxcomp.dockerfile_fix), and points the build at the fixed sibling
        file. The original Dockerfile is never modified, so it still builds on
        a real docker host (Pi). Prints what changed so the user can see it.
        """
        try:
            from tuxcomp.dockerfile_fix import fix_dockerfile
        except ImportError:
            return
        for i, part in enumerate(command):
            if part == "-f" and i + 1 < len(command):
                src = Path(command[i + 1])
                if not src.exists():
                    return
                out = src.with_name(f".tuxcomp-{src.name}")
                changed, notes = fix_dockerfile(src, out)
                if changed:
                    command[i + 1] = str(out)
                    print(f"  → Dockerfile rewritten for proot-distro build ({len(notes)} change(s)):")
                    for note in notes:
                        print(f"      {note}")
                return

    def _install_target(self, command: list[str]) -> str | None:
        """Extract the container name from an install step's -n flag."""
        for i, part in enumerate(command):
            if part in ("-n", "--name") and i + 1 < len(command):
                return command[i + 1]
        return None

    def run(self, plan: Plan, health_timeout: int = 120) -> list[RunResult]:
        self.results = []
        built: set[str] = set()
        installed = self._installed_container_names()
        for i, step in enumerate(plan.steps):
            if step.kind == "note":
                self.results.append(RunResult(step.title, step.service, True, "", True))
                continue
            if step.kind == "reuse":
                # no-op step: the shared container is used as-is, nothing to run
                self.results.append(RunResult(step.title, step.service, True, "reused", True))
                print(f"[OK ] [{step.service}] {step.title} (reused)")
                continue
            if step.kind == "build" and step.service in built:
                self.results.append(RunResult(step.title, step.service, True, "already built", True))
                continue
            if step.kind == "build":
                target = self._build_target(step.command)
                if target and target in installed:
                    self.results.append(
                        RunResult(step.title, step.service, True, "container already exists", True)
                    )
                    print(f"[OK ] [{step.service}] {step.title} (skipped: already exists)")
                    built.add(step.service)
                    continue
            if step.kind == "install":
                target = self._install_target(step.command)
                if target and target in installed:
                    self.results.append(
                        RunResult(step.title, step.service, True, "container already exists", True)
                    )
                    print(f"[OK ] [{step.service}] {step.title} (skipped: already exists)")
                    continue
            if step.kind == "health":
                result = self._run_health(step, health_timeout)
            else:
                result = self._run_step(step)
            self.results.append(result)
            if result.ok and step.kind == "build":
                built.add(step.service)
            if self.verbose and step.note:
                print(f"  note: {step.note}", file=sys.stderr)
            if not result.ok:
                remaining = len(plan.steps) - i - 1
                print(
                    f"error: step '{step.title}' failed - aborting, {remaining} remaining step(s) not run",
                    file=sys.stderr,
                )
                break
        return self.results

    def _run_step(self, step) -> RunResult:
        # transparently translate a Dockerfile proot-distro build can't handle
        if step.kind == "build" and not self.dry_run:
            self._rewrite_build_dockerfile(step.command)
        if step.command:
            print(f"  $ {self._cmdline(step.command)}")
        if self.dry_run:
            print(f"{self._prefix(step.service, True)} {step.title}")
            return RunResult(step.title, step.service, True, "", True)
        try:
            # Long operations that need visible progress
            is_long_op = step.kind in ("install", "build", "provision")

            if is_long_op or self.verbose:
                if step.kind == "install":
                    print("  → Downloading and installing container image...")
                elif step.kind == "build":
                    print("  → Building container from Dockerfile (this may take several minutes)...")
                elif step.kind == "provision":
                    print("  → Installing packages inside container...")

                # Stream output directly to terminal (no buffering)
                proc = subprocess.Popen(
                    [self.proot if c == "proot-distro" else c for c in step.command],
                    stdout=None,
                    stderr=None,
                    text=True,
                )

                # Spinner + elapsed timer so it's obvious we're still working
                frames = ["|", "/", "-", "\\"]
                start_time = time.time()
                frame_i = 0
                last_print = 0.0
                while proc.poll() is None:
                    time.sleep(0.25)
                    elapsed = time.time() - start_time
                    if time.time() - last_print >= 1:
                        mins = int(elapsed // 60)
                        secs = int(elapsed % 60)
                        print(
                            f"  {frames[frame_i % 4]} still working... ({mins}m {secs:02d}s)   ",
                            end="\r",
                        )
                        sys.stdout.flush()
                        last_print = time.time()
                        frame_i += 1
                # Clear the spinner line
                print(" " * 60, end="\r")
                sys.stdout.flush()
                out = ""
            else:
                proc = subprocess.run(
                    [self.proot if c == "proot-distro" else c for c in step.command],
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
                out = proc.stdout.strip() + proc.stderr.strip()

            if proc.returncode != 0 and step.kind == "build" and "already exists" in out:
                result = RunResult(step.title, step.service, True, out, True)
            elif (
                proc.returncode != 0
                and step.kind in ("stop", "remove")
                and (
                    "not installed" in out
                    or "not found" in out
                    or "no active sessions" in out
                    or "not running" in out
                )
            ):
                # idempotent teardown: the container is already gone
                result = RunResult(step.title, step.service, True, out, True)
            elif proc.returncode != 0:
                print(f"  exit {proc.returncode}: {out[-500:]}")
                result = RunResult(step.title, step.service, False, out)
            else:
                result = RunResult(step.title, step.service, True, out)
            if result.skipped:
                print(f"{self._prefix(step.service, True)} {step.title} (skipped: already gone)")
            else:
                print(f"{self._prefix(step.service, result.ok)} {step.title}")
            return result
        except (subprocess.TimeoutExpired, OSError) as exc:
            print(f"{self._prefix(step.service, False)} {step.title}")
            return RunResult(step.title, step.service, False, str(exc))

    def _run_health(self, step, timeout: int) -> RunResult:
        if step.command:
            print(f"  $ {self._cmdline(step.command)}")
        if self.dry_run:
            print(f"{self._prefix(step.service, True)} {step.title}")
            return RunResult(step.title, step.service, True, "", True)
        print(f"  → Waiting for service to become healthy (up to {timeout}s)...")
        deadline = time.time() + timeout
        last = ""
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            try:
                proc = subprocess.run(
                    [self.proot if c == "proot-distro" else c for c in step.command],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if proc.returncode == 0:
                    print(f"{self._prefix(step.service, True)} {step.title} (healthy after {attempt} check(s))")
                    return RunResult(step.title, step.service, True, "healthy")
                last = (proc.stdout + proc.stderr).strip()[-200:]
                print(f"  • health check {attempt} failed, retrying in 5s...")
            except (subprocess.TimeoutExpired, OSError) as exc:
                last = str(exc)
                print(f"  • health check {attempt} error: {exc}")
            time.sleep(5)
        print(f"{self._prefix(step.service, False)} {step.title} (not healthy)")
        return RunResult(step.title, step.service, False, f"not healthy after {timeout}s: {last}")


@dataclass
class ServiceState:
    service: str
    container: str
    status: str = "stopped"
    pid: str = ""
    started_at: str = ""
    ports: list[str] = field(default_factory=list)


class State:
    def __init__(self, project_name: str, root: str | None = None):
        self.project_name = project_name
        self.root = Path(root or os.path.expanduser("~/.tuxcomp"))
        self.dir = self.root / project_name
        self.file = self.dir / "state.json"

    def load(self) -> dict:
        if self.file.exists():
            try:
                return json.loads(self.file.read_text())
            except json.JSONDecodeError:
                return {}
        return {}

    def save(self, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def update_services(self, services: list[ServiceState]) -> None:
        data = self.load()
        data.setdefault("services", {})
        for svc in services:
            data["services"][svc.service] = {
                "container": svc.container,
                "status": svc.status,
                "pid": svc.pid,
                "started_at": svc.started_at,
                "ports": svc.ports,
            }
        self.save(data)

    def get_service(self, service: str) -> ServiceState | None:
        data = self.load().get("services", {}).get(service)
        if not data:
            return None
        return ServiceState(
            service=service,
            container=data.get("container", service),
            status=data.get("status", "stopped"),
            pid=data.get("pid", ""),
            started_at=data.get("started_at", ""),
            ports=data.get("ports", []),
        )

    def log_path(self, service: str) -> Path:
        return self.dir / f"{service}.log"