from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from tuxcomp import __version__
from tuxcomp.parser import ComposeError, parse_compose_file
from tuxcomp.planner import (
    PlanError,
    bind_args,
    build_plan,
    down_plan,
    env_args,
    service_start_command,
    volume_dirs_for,
)
from tuxcomp.runner import Runner, ServiceState, State

DEFAULT_PD_DIR = "/data/data/com.termux/files/usr/var/lib/proot-distro/containers"

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _clean(line: str) -> str:
    return _ANSI_RE.sub("", line)


def _proot() -> str:
    return os.environ.get("TUXCOMP_PROOT", "proot-distro")


def _pd_dir() -> str:
    return os.environ.get("TUXCOMP_PD_DIR", DEFAULT_PD_DIR)


def _tux_root() -> str:
    home = os.environ.get("TUXCOMP_HOME") or os.environ.get("HOME")
    if home:
        return os.path.join(home, ".tuxcomp")
    return os.path.expanduser("~/.tuxcomp")


def _registry_dir() -> str:
    return os.path.join(_tux_root(), "registry")


def _registry_path(container: str) -> str:
    return os.path.join(_registry_dir(), f"{container}.json")


def _load_registry(container: str) -> dict | None:
    path = _registry_path(container)
    if not os.path.exists(path):
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _save_registry(entry: dict) -> None:
    os.makedirs(_registry_dir(), exist_ok=True)
    Path(_registry_path(entry["container"])).write_text(
        json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _delete_registry(container: str) -> None:
    path = _registry_path(container)
    if os.path.exists(path):
        os.remove(path)


def _remotes_path() -> str:
    return os.path.join(_tux_root(), "remotes.json")


def _load_remotes() -> dict:
    if not os.path.exists(_remotes_path()):
        return {"default": None, "remotes": {}}
    try:
        data = json.loads(Path(_remotes_path()).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"default": None, "remotes": {}}
    data.setdefault("default", None)
    data.setdefault("remotes", {})
    return data


def _save_remotes(data: dict) -> None:
    os.makedirs(_tux_root(), exist_ok=True)
    Path(_remotes_path()).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _cmd_remote(args: argparse.Namespace) -> int:
    data = _load_remotes()
    if args.action == "add":
        if not args.name or not args.host:
            print("error: tuxcomp remote add <name> --host <host> [--port N] [--default]", file=sys.stderr)
            return 1
        data["remotes"][args.name] = {"host": args.host, "port": args.port}
        if args.default or data["default"] is None:
            data["default"] = args.name
        _save_remotes(data)
        tag = " (default)" if data["default"] == args.name else ""
        print(f"added remote '{args.name}' -> {args.host}:{args.port}{tag}")
        return 0
    if args.action == "list":
        if not data["remotes"]:
            print("no remotes configured (tuxcomp remote add <name> --host <host>)")
            return 0
        for name, cfg in sorted(data["remotes"].items()):
            marker = "*" if name == data["default"] else " "
            print(f"{marker} {name:<20} {cfg['host']}:{cfg.get('port', 22)}")
        return 0
    if args.action in ("remove", "set-default"):
        if not args.name or args.name not in data["remotes"]:
            print(f"error: no remote '{args.name}'", file=sys.stderr)
            return 1
        if args.action == "remove":
            del data["remotes"][args.name]
            if data["default"] == args.name:
                data["default"] = next(iter(data["remotes"]), None)
            _save_remotes(data)
            print(f"removed remote '{args.name}'")
        else:
            data["default"] = args.name
            _save_remotes(data)
            print(f"default remote: {args.name}")
        return 0
    print(f"error: unknown remote action '{args.action}'", file=sys.stderr)
    return 1


def _resolve_deploy_target(deploy, args) -> tuple[str, int] | None:
    """Resolve ssh host/port: --remote flag > compose x-tuxcomp.deploy > default remote."""
    if getattr(args, "remote", None):
        data = _load_remotes()
        cfg = data["remotes"].get(args.remote)
        if not cfg:
            print(f"error: unknown remote '{args.remote}' (tuxcomp remote list)", file=sys.stderr)
            return None
        return str(cfg["host"]), int(cfg.get("port", 22))
    if deploy and deploy.host:
        return deploy.host, deploy.port
    data = _load_remotes()
    default = data.get("default")
    if default and default in data["remotes"]:
        cfg = data["remotes"][default]
        return str(cfg["host"]), int(cfg.get("port", 22))
    print(
        "error: no deploy target - add one:\n"
        "    tuxcomp remote add phone --host root@192.168.1.153 --port 8022 --default\n"
        "  or set x-tuxcomp.deploy.host in the compose file",
        file=sys.stderr,
    )
    return None


def _make_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("-f", "--file", default=None, help="compose file (default: docker-compose.yml when needed)")
    parent.add_argument("--project", default=None, help="override project name")
    parent.add_argument("--dry-run", action="store_true", help="show translated commands without running them")
    parent.add_argument("-v", "--verbose", action="store_true", help="print step notes")
    return parent


def _parse_args(args: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tuxcomp",
        description="Docker Compose orchestration for proot-distro on Termux",
    )
    parser.add_argument("--version", action="version", version=f"tuxcomp {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    parent = _make_parent()

    sub.add_parser("plan", parents=[parent], help="print the translated proot-distro sequence without running it")

    up = sub.add_parser("up", parents=[parent], help="create and start services")
    up.add_argument("container", nargs="?", help="container name to restart from saved config (no -f needed)")
    up.add_argument("--profile", action="append", default=[], help="activate a profile")
    up.add_argument("--no-detach", action="store_true", help="run login without -d (stay attached)")
    up.add_argument("--health-timeout", type=int, default=120)
    up.add_argument("--force-ports", action="store_true", help="skip the port-conflict check")

    down = sub.add_parser("down", parents=[parent], help="stop services (soft stop: keep containers/volumes/state)")
    down.add_argument("container", nargs="?", help="container name to stop (no -f needed)")

    ps = sub.add_parser("ps", parents=[parent], help="list containers (no -f: all installed; with -f: managed services)")
    ps.add_argument("-a", "--all", action="store_true", help="show all containers (default shows running only)")

    sub.add_parser("list", parents=[parent], help="list installed proot-distro containers")

    logs = sub.add_parser("logs", parents=[parent], help="show service/container logs")
    logs.add_argument("service", nargs="?", help="container or service name (default: all services with -f)")
    logs.add_argument("-n", "--lines", type=int, default=30, help="number of tail lines (default: 30)")
    logs.add_argument("--tail", type=int, default=None, dest="tail_n", help="alias for --lines (docker style)")
    logs.add_argument("--follow", action="store_true", help="keep streaming new log lines (docker -f)")

    exec_ = sub.add_parser("exec", parents=[parent], help="run a command in a container (docker exec)")
    exec_.add_argument("service", help="container name, or service name with -f")
    exec_.add_argument("cmd", nargs="*", default=None, help="command to run (default: interactive shell)")

    stop = sub.add_parser("stop", parents=[parent], help="stop a container (docker stop)")
    stop.add_argument("container", nargs="?", help="container name, or service name with -f (default: all with -f)")

    start = sub.add_parser("start", parents=[parent], help="start a stopped container from its saved config (docker start)")
    start.add_argument("container", help="container name (saved by a previous up)")
    start.add_argument("--health-timeout", type=int, default=120)

    rmi = sub.add_parser("rmi", parents=[parent], help="remove container(s) AND their volumes/state (docker rmi -f + volume rm)")
    rmi.add_argument("container", nargs="?", help="container name (no -f), or compose-managed project (with -f)")
    rmi.add_argument("--force", action="store_true", help="skip confirmation")

    rebuild = sub.add_parser(
        "rebuild",
        parents=[parent],
        help="stop + remove container + re-up from its saved compose (keeps volumes/state; picks up new code/config)",
    )
    rebuild.add_argument("container", nargs="?", help="container name (no -f), or all services of a project (with -f)")
    rebuild.add_argument("--health-timeout", type=int, default=120)

    deploy = sub.add_parser(
        "deploy",
        parents=[parent],
        help="PC-side: build locally, push artifacts to a phone and `up` it (uses x-tuxcomp.deploy + remote config)",
    )
    deploy.add_argument("--remote", default=None, help="remote name (tuxcomp remote list); defaults to compose host or default remote")

    remote = sub.add_parser(
        "remote",
        parents=[parent],
        help="manage deploy targets (ssh host/port, set once, inferred later)",
    )
    remote.add_argument("action", choices=["add", "list", "remove", "set-default"])
    remote.add_argument("name", nargs="?", help="remote name (required for add/remove/set-default)")
    remote.add_argument("--host", default=None, help="ssh target, e.g. root@192.168.1.153")
    remote.add_argument("--port", type=int, default=22, help="ssh port (default 22)")
    remote.add_argument("--default", action="store_true", help="make this the default remote")

    return parser.parse_args(args)


def _load_project(args: argparse.Namespace):
    project = parse_compose_file(args.file or "docker-compose.yml")
    if args.project:
        project.name = args.project
    return project


def _prebuilt_guard(project) -> int:
    """Fail loudly if a prebuilt service's host bind source doesn't exist.

    Only relative host-path binds are checked (named/anonymous volumes are
    created by up, and build: services compile on-device).
    """
    missing: list[str] = []
    for service in project.service_list():
        if service.build:
            continue
        for mount in service.volumes:
            if mount.is_named or mount.is_anonymous:
                continue
            src = mount.source
            if os.path.isabs(src) or src.startswith("~"):
                continue
            host_path = os.path.join(project.source_dir, src)
            if not os.path.exists(host_path):
                missing.append(f"  {service.name}: {src} -> {host_path}")
    if missing:
        print(
            "error: prebuilt bind source(s) not found on this machine:",
            file=sys.stderr,
        )
        for line in missing:
            print(line, file=sys.stderr)
        print(
            "\n→ these services serve prebuilt artifacts (e.g. an Angular dist/).\n"
            "  Build them on your PC and push with:\n"
            "      tuxcomp deploy -f <compose.yml>\n"
            "  (deploy runs the host build first, then scp + up)\n"
            "  or use a compose with `build:` to compile on-device instead.",
            file=sys.stderr,
        )
        return 1
    return 0


_DOCKERFILE_PORT_RE = re.compile(
    r"(?:--port[= ](\d+))|(?:--bind[= ]\S+:(\d+))|(?:EXPOSE[ \t]+(\d+))",
    re.IGNORECASE,
)


def _dockerfile_ports(project) -> list[str]:
    """Ports a service's Dockerfile says the app will listen on.

    proot shares localhost (no NAT), so the app's *actual* listen port is what
    matters — the compose `ports:` mapping is just documentation. Reading
    EXPOSE/--port/--bind from the Dockerfile catches conflicts the compose
    mapping alone would miss (e.g. Dockerfile CMD hardcodes 8000 while the
    compose says 8080).
    """
    ports: list[str] = []
    for service in project.service_list():
        if not service.build:
            continue
        df = Path(project.source_dir) / service.build.context / service.build.dockerfile
        if not df.exists():
            df = Path(service.build.context) / service.build.dockerfile
        if not df.exists():
            continue
        try:
            text = df.read_text(encoding="utf-8")
        except OSError:
            continue
        for m in _DOCKERFILE_PORT_RE.finditer(text):
            port = next((g for g in m.groups() if g), None)
            if port and port not in ports:
                ports.append(port)
    return ports


def _running_port_owners() -> dict[str, str]:
    """Map host port -> running container name (from saved registry entries)."""
    owners: dict[str, str] = {}
    running = _running_containers()
    for name in _registry_containers():
        if name not in running:
            continue
        entry = _load_registry(name) or {}
        for port in entry.get("ports", []):
            owners.setdefault(str(port), name)
    return owners


def _check_port_conflicts(project, force: bool = False) -> int:
    """Detect port conflicts before building: same-project dups and
    collisions with ports owned by other running containers.

    Returns 1 on conflict unless --force-ports (docker fails at bind time too;
    we fail earlier with a clear message, saving a long build).
    """
    declared: list[tuple[str, str, str]] = []  # (service, port, origin)
    for service in project.service_list():
        for p in service.ports:
            host = p.host or p.container
            if host:
                declared.append((service.name, host, "compose"))
    for port in _dockerfile_ports(project):
        if not any(d[1] == port for d in declared):
            declared.append(("(Dockerfile)", port, "Dockerfile"))

    seen: dict[str, str] = {}
    for svc, port, origin in declared:
        if port in seen and seen[port] != svc:
            print(
                f"error: port {port} declared by both '{seen[port]}' and '{svc}' "
                f"({origin}) - change one of them in the compose file",
                file=sys.stderr,
            )
            return 1
        seen.setdefault(port, svc)

    if force:
        return 0

    project_containers = {
        (svc.container() or svc.name) for svc in project.service_list()
    }
    owners = _running_port_owners()
    problems: list[str] = []
    for svc, port, origin in declared:
        owner = owners.get(port)
        if owner and owner not in project_containers:
            problems.append(
                f"  port {port} ({svc}, {origin}) is owned by running container '{owner}'"
            )
    if problems:
        print(
            "error: port conflict(s) - services would fail to bind (proot shares localhost):",
            file=sys.stderr,
        )
        for line in problems:
            print(line, file=sys.stderr)
        print(
            "\n  fix: stop the owning container (tuxcomp down <name>),\n"
            "  or change the port in the compose file,\n"
            "  or re-run with --force-ports to proceed anyway.",
            file=sys.stderr,
        )
        return 1
    return 0


def _health_command(project, service, container: str) -> list[str] | None:
    test = service.healthcheck.test if service.healthcheck else None
    if not test:
        return None
    if test[0] == "CMD-SHELL":
        return [_proot(), "login", container, "--", "/bin/sh", "-c", " ".join(test[1:])]
    if test[0] == "CMD":
        return [_proot(), "login", container, "--", *test[1:]]
    return [_proot(), "login", container, "--", *test]


def _cmd_plan(args: argparse.Namespace) -> int:
    try:
        project = _load_project(args)
        plan = build_plan(project)
    except (ComposeError, PlanError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return _print_plan(plan, verbose=args.verbose)


def _print_plan(plan, verbose: bool = False) -> int:
    print(f"project: {plan.project_name}")
    for step in plan.steps:
        marker = " " if step.kind == "note" else "+"
        print(f"{marker} [{step.kind:7s}] {step.title}")
        if step.command:
            cmd = [("proot-distro" if c == "proot-distro" else c) for c in step.command]
            print(f"          $ {' '.join(_q(c) for c in cmd)}")
        if step.note and (verbose or step.kind in ("start", "health")):
            print(f"          note: {step.note}")
    return 0


def _q(s: str) -> str:
    return s if s and s.replace("_", "").replace("-", "").isalnum() else repr(s)


def _cmd_up(args: argparse.Namespace) -> int:
    # docker-style: tuxcomp up <container> restarts from saved config (no compose)
    if not args.file and args.container:
        return _cmd_start(args)
    try:
        project = _load_project(args)
        plan = build_plan(project, profiles=args.profile, detach=not args.no_detach)
    except (ComposeError, PlanError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        return _print_plan(plan, verbose=args.verbose)

    guard_failed = _prebuilt_guard(project)
    if guard_failed:
        return guard_failed

    conflict = _check_port_conflicts(project, force=args.force_ports)
    if conflict:
        return conflict

    runner = Runner(dry_run=False, verbose=args.verbose, proot=_proot())
    results = runner.run(plan, health_timeout=args.health_timeout)
    failed = [r for r in results if not r.ok]
    if failed:
        print(f"error: {len(failed)} step(s) failed", file=sys.stderr)
        return 1

    state = State(project.name)
    started = []
    for service in project.service_list():
        if service.tuxcomp and service.tuxcomp.reuse:
            continue
        container = service.container() or f"{project.name}-{service.name}"
        ports = [p.host or p.container for p in service.ports]
        started.append(
            ServiceState(
                service=service.name,
                container=container,
                status="running",
                ports=ports,
            )
        )
        _save_registry(
            {
                "container": container,
                "project": project.name,
                "compose_file": os.path.abspath(project.source_file),
                "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "ports": ports,
                "start": service_start_command(project, service, container, detach=True),
                "health": _health_command(project, service, container),
                "tunnel": bool(project.tuxcomp and project.tuxcomp.cloudflared),
                "tunnel_mode": (
                    "host"
                    if (project.tuxcomp and project.tuxcomp.cloudflared and not project.tuxcomp.cloudflared.token)
                    else "container"
                ),
                "tunnel_name": (
                    project.tuxcomp.cloudflared.tunnel
                    if (project.tuxcomp and project.tuxcomp.cloudflared and project.tuxcomp.cloudflared.tunnel)
                    else None
                ),
                "volume_dirs": sorted(
                    set(
                        bind_args(project, service, volume_dirs_for(project))[i + 1].split(":")[0]
                        for i in range(0, len(bind_args(project, service, volume_dirs_for(project))), 2)
                    )
                ),
            }
        )
    state.update_services(started)
    print(f"started {len(started)} service(s)")
    return 0


def _cmd_down(args: argparse.Namespace) -> int:
    # docker-style: tuxcomp down <container> soft-stops a registered container (no compose)
    if not args.file and args.container:
        return _stop_container(args.container)
    try:
        project = _load_project(args)
        plan = down_plan(project)
    except (ComposeError, PlanError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        return _print_plan(plan, verbose=args.verbose)

    runner = Runner(dry_run=False, verbose=args.verbose, proot=_proot())
    results = runner.run(plan)
    failed = [r for r in results if not r.ok]
    state = State(project.name)
    data = state.load()
    data.setdefault("services", {})
    for svc in project.services:
        data["services"][svc] = {"status": "stopped", "container": svc}
    state.save(data)
    if failed:
        return 1
    print("stopped all services")
    return 0


def _stop_container(container: str) -> int:
    try:
        proc = subprocess.run([_proot(), "kill", container])
        if proc.returncode == 0:
            print(f"stopped {container}")
        return proc.returncode
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _installed_containers() -> list[str]:
    try:
        proc = subprocess.run([_proot(), "list"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return []
    names: list[str] = []
    for line in (proc.stdout + "\n" + proc.stderr).splitlines():
        line = _clean(line).strip()
        if line.startswith("* "):
            names.append(line[2:].strip())
    return names


def _running_containers() -> set[str]:
    try:
        proc = subprocess.run([_proot(), "ps"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return set()
    running: set[str] = set()
    for line in (proc.stdout + "\n" + proc.stderr).splitlines():
        parts = _clean(line).split()
        if len(parts) >= 2 and parts[0].isdigit():
            running.add(parts[1])
    return running


def _cmd_ps(args: argparse.Namespace) -> int:
    if not args.file:
        installed = _installed_containers()
        running = _running_containers()
        if not installed:
            print("no containers installed")
            return 0
        print(f"{'CONTAINER':<24} {'STATUS':<10} {'PORTS':<16} CREATED")
        for name in installed:
            if not args.all and name not in running:
                continue
            status = "running" if name in running else "stopped"
            entry = _load_registry(name) or {}
            ports = ",".join(entry.get("ports", [])) or "-"
            created = (entry.get("created_at") or "-")[:16]
            print(f"{name:<24} {status:<10} {ports:<16} {created}")
        return 0

    try:
        project = _load_project(args)
    except ComposeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    state = State(project.name)
    data = state.load().get("services", {})
    running = _running_containers()
    print(f"{'SERVICE':<20} {'CONTAINER':<28} {'STATUS':<10} PORTS")
    for service in project.service_list():
        info = data.get(service.name, {})
        container = info.get("container") or service.container() or f"{project.name}-{service.name}"
        status = info.get("status", "stopped")
        if container in running:
            status = "running"
        if not args.all and status != "running":
            continue
        ports = ",".join(info.get("ports", [])) or "-"
        print(f"{service.name:<20} {container:<28} {status:<10} {ports}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    try:
        proc = subprocess.run([_proot(), "list"], capture_output=True, text=True)
        print(proc.stdout.strip() or proc.stderr.strip())
        return proc.returncode
    except OSError as exc:
        print(f"error: cannot run {_proot()}: {exc}", file=sys.stderr)
        return 1


def _log_path(container: str) -> str:
    return os.path.join(_pd_dir(), container, "rootfs", "var", "log", "tuxcomp", f"{container}.log")


def _cmd_logs(args: argparse.Namespace) -> int:
    lines = args.tail_n if args.tail_n is not None else args.lines

    if not args.file:
        if not args.service:
            print("error: tuxcomp logs <container> [--tail N] [--follow]", file=sys.stderr)
            return 1
        path = _log_path(args.service)
        if not os.path.exists(path):
            print(
                f"error: no captured log for '{args.service}' ({path}) - "
                f"services started via image entrypoint write to /dev/null; "
                f"add a service `command:` to capture logs",
                file=sys.stderr,
            )
            return 1
        return _tail_file(path, lines, args.follow)

    try:
        project = _load_project(args)
    except ComposeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    services = [args.service] if args.service else list(project.services.keys())
    shown = False
    failed = False
    for name in services:
        service = project.get(name)
        if not service:
            print(f"error: no service '{name}'", file=sys.stderr)
            failed = True
            continue
        path = _log_path(service.container())
        if not os.path.exists(path):
            print(f"=== {name} === (no captured log: {path})", file=sys.stderr)
            failed = True
            continue
        shown = True
        print(f"=== {name} ===")
        if _tail_file(path, lines, args.follow) != 0:
            failed = True
    if not shown and failed:
        return 1
    return 0


def _tail_file(path: str, lines: int, follow: bool) -> int:
    try:
        proc = subprocess.run(
            ["tail", "-n", str(lines)] + (["-f"] if follow else []) + [path],
        )
        return proc.returncode
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_exec(args: argparse.Namespace) -> int:
    if not args.file:
        container = args.service
        cmd = [_proot(), "login", container]
        if args.cmd:
            cmd += ["--", *args.cmd]
        try:
            return subprocess.call(cmd)
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    try:
        project = _load_project(args)
    except ComposeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    service = project.get(args.service)
    if not service:
        print(f"error: no service '{args.service}'", file=sys.stderr)
        return 1
    container = service.container() or f"{project.name}-{service.name}"
    cmd = [_proot(), "login", container]
    cmd += bind_args(project, service, volume_dirs_for(project))
    cmd += env_args(service)
    if args.cmd:
        cmd += ["--", *args.cmd]
    try:
        return subprocess.call(cmd)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _cmd_stop(args: argparse.Namespace) -> int:
    if not args.file:
        if not args.container:
            print("error: tuxcomp stop <container>", file=sys.stderr)
            return 1
        return _stop_container(args.container)
    return _cmd_down(args)


def _cmd_start(args: argparse.Namespace) -> int:
    entry = _load_registry(args.container)
    if not entry:
        print(
            f"error: no saved config for '{args.container}' - run 'tuxcomp up -f <compose>' once first",
            file=sys.stderr,
        )
        return 1
    cmd = [_proot() if c == "proot-distro" else c for c in entry["start"]]
    print(f"start {args.container}")
    print(f"  $ {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        print(f"  exit {proc.returncode}: {(proc.stdout + proc.stderr).strip()[-500:]}", file=sys.stderr)
        return 1

    health = entry.get("health")
    if health:
        deadline = time.time() + args.health_timeout
        last = ""
        while time.time() < deadline:
            try:
                probe = subprocess.run(health, capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as exc:
                last = str(exc)
                time.sleep(2)
                continue
            if probe.returncode == 0:
                print(f"healthy ({args.container})")
                break
            last = (probe.stdout + probe.stderr).strip()
            time.sleep(2)
        else:
            print(f"error: not healthy after {args.health_timeout}s: {last}", file=sys.stderr)
            return 1

    if entry.get("tunnel"):
        if entry.get("tunnel_mode") == "host":
            # Host mode: ensure the shared global cloudflared is running (never
            # kill it - other projects may rely on the same instance).
            tunnel = entry.get("tunnel_name")
            name_part = f" {shlex.quote(tunnel)}" if tunnel else ""
            tunnel_cmd = ["/bin/sh", "-c", (
                "if pgrep -f '[c]loudflared tunnel run' >/dev/null; then "
                "echo 'host cloudflared already running - leaving it alone'; "
                "else "
                "mkdir -p /var/log/tuxcomp && "
                f"nohup cloudflared tunnel run{name_part} > /var/log/tuxcomp/cloudflared.log 2>&1 & disown; "
                "echo 'host cloudflared started'; "
                "fi"
            )]
        else:
            tunnel_cmd = [
                _proot(),
                "login",
                args.container,
                "-d",
                "--",
                "/bin/sh",
                "/root/.tuxcomp/start-tunnel.sh",
            ]
        try:
            subprocess.run(tunnel_cmd, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"error: tunnel start failed: {exc}", file=sys.stderr)
            return 1
        print(f"tunnel restarted ({args.container})")
    return 0


def _ask_yes(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _cmd_rmi(args: argparse.Namespace) -> int:
    if not args.file:
        return _rmi_container(args.container, force=args.force)
    return _rmi_compose(args, force=args.force)


def _rmi_container(container: str | None, force: bool = False) -> int:
    if not container:
        print("error: tuxcomp rmi <container>", file=sys.stderr)
        return 1
    entry = _load_registry(container)
    targets = [container]
    if entry:
        targets += entry.get("volume_dirs", [])
        targets.append(os.path.join(_tux_root(), entry.get("project", "")))
    print("removing:")
    for t in targets:
        print(f"  - {t}")
    if not force and not _ask_yes("permanently remove container + volumes + state?"):
        print("aborted")
        return 1
    try:
        subprocess.run([_proot(), "kill", container], capture_output=True, text=True)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
    try:
        proc = subprocess.run([_proot(), "remove", container], capture_output=True, text=True)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if proc.returncode != 0 and "not found" not in (proc.stdout + proc.stderr).lower() and "not installed" not in (proc.stdout + proc.stderr).lower():
        print(f"error: remove failed: {(proc.stdout + proc.stderr).strip()[-500:]}", file=sys.stderr)
        return 1
    _delete_registry(container)
    for t in targets[1:]:
        if os.path.exists(t):
            shutil.rmtree(t, ignore_errors=True)
    print(f"removed {container}")
    return 0


def _rmi_compose(args: argparse.Namespace, force: bool = False) -> int:
    try:
        project = _load_project(args)
    except ComposeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    plan = down_plan(project, remove=True)
    installed = set(_installed_containers())
    # only attempt removal of containers that actually exist - services behind
    # inactive profiles (e.g. demo) were never provisioned
    kept_steps = []
    for step in plan.steps:
        if step.kind == "remove":
            name = step.title.split()[-1].strip("()")
            if name not in installed:
                print(f"  - {name} (skip: not installed)")
                continue
        kept_steps.append(step)
    plan.steps = kept_steps
    containers = [s.title.split()[-1].strip("()") for s in plan.steps if s.kind == "remove"]
    volume_dirs = list(volume_dirs_for(project).values())
    state_dir = os.path.join(_tux_root(), project.name)
    targets = containers + volume_dirs + [state_dir]
    print("removing:")
    for t in targets:
        print(f"  - {t}")
    if not force and not _ask_yes("permanently remove all services, volumes and state?"):
        print("aborted")
        return 1
    runner = Runner(dry_run=False, verbose=args.verbose, proot=_proot())
    results = runner.run(plan)
    failed = [r for r in results if not r.ok]
    if failed:
        # leave registry/volumes/state intact so a retry can complete cleanly
        return 1
    for container in containers:
        _delete_registry(container)
    for t in volume_dirs + [state_dir]:
        if os.path.exists(t):
            shutil.rmtree(t, ignore_errors=True)
    print("removed project services, volumes and state")
    return 0


def _cmd_rebuild(args: argparse.Namespace) -> int:
    # docker-style: tuxcomp rebuild <container> -> remove + re-up from saved compose
    if not args.file:
        if not args.container:
            print("error: tuxcomp rebuild <container>", file=sys.stderr)
            return 1
        entry = _load_registry(args.container)
        if not entry:
            print(
                f"error: no saved config for '{args.container}' - run 'tuxcomp up -f <compose>' once first",
                file=sys.stderr,
            )
            return 1
        if entry.get("reuse"):
            print(f"error: '{args.container}' reuses a shared container - never remove it", file=sys.stderr)
            return 1
        compose_file = entry.get("compose_file")
        if not compose_file or not os.path.exists(compose_file):
            print(
                f"error: saved compose file for '{args.container}' not found ({compose_file}) - "
                f"run 'tuxcomp up -f <compose>' again",
                file=sys.stderr,
            )
            return 1
        original_created = entry.get("created_at")
        print(f"rebuild {args.container} (from {compose_file})")
        rc = _remove_container(args.container)
        if rc != 0:
            return rc
        return _run_up(compose_file, args, preserve_created=original_created)

    # with -f: rebuild every service of the project
    try:
        project = _load_project(args)
    except ComposeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    plan = down_plan(project, remove=True)
    containers: list[str] = []
    for step in plan.steps:
        if step.kind == "remove":
            containers.append(step.title.split()[-1].strip("()"))
    if not containers:
        print("nothing to rebuild")
        return 0
    print(f"rebuild {len(containers)} container(s) for project '{project.name}'")
    for container in containers:
        rc = _remove_container(container)
        if rc != 0:
            return rc
    return _run_up(args.file or "docker-compose.yml", args)


def _remove_container(container: str) -> int:
    try:
        subprocess.run([_proot(), "kill", container], capture_output=True, text=True, timeout=60)
    except OSError:
        pass
    try:
        proc = subprocess.run([_proot(), "remove", container], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    out = (proc.stdout + proc.stderr).lower()
    if proc.returncode != 0 and "not found" not in out and "not installed" not in out:
        print(f"error: remove {container} failed: {(proc.stdout + proc.stderr).strip()[-500:]}", file=sys.stderr)
        return 1
    return 0


def _run_up(compose_file: str, args: argparse.Namespace, preserve_created: str | None = None) -> int:
    """Re-run tuxcomp up on a compose file (rebuild path)."""
    up_args = [
        sys.argv[0] if sys.argv and sys.argv[0] else "tuxcomp",
        "up",
        "-f",
        compose_file,
        "--health-timeout",
        str(args.health_timeout),
    ]
    if getattr(args, "verbose", False):
        up_args.append("-v")
    if preserve_created:
        # remember created_at so ps -a still shows true age after rebuild
        before = {
            c: (entry.get("created_at") if (entry := _load_registry(c)) else None)
            for c in _registry_containers()
        }
    try:
        proc = subprocess.call(up_args)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if preserve_created:
        for c, created in before.items():
            entry = _load_registry(c)
            if entry and created:
                entry["created_at"] = created
                _save_registry(entry)
    return proc


def _registry_containers() -> list[str]:
    d = _registry_dir()
    if not os.path.isdir(d):
        return []
    return [p[:-5] for p in os.listdir(d) if p.endswith(".json")]


def _scp(host: str, port: int, src: str, dest: str) -> int:
    cmd = ["scp", "-q", "-P", str(port)]
    if os.path.isdir(src):
        cmd += ["-r"]
    cmd += [src, f"{host}:{dest}"]
    print(f"  $ {' '.join(shlex.quote(c) for c in cmd)}")
    try:
        return subprocess.call(cmd)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _ssh(host: str, port: int, cmd: str) -> int:
    full = ["ssh", "-q", "-p", str(port), host, cmd]
    print(f"  $ {' '.join(shlex.quote(c) for c in full)}")
    try:
        return subprocess.call(full)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _ssh_out(host: str, port: int, cmd: str, timeout: int = 30) -> str:
    """Run a remote command and return its stdout (empty string on failure)."""
    full = ["ssh", "-q", "-o", "ConnectTimeout=10", "-p", str(port), host, cmd]
    try:
        proc = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
        return proc.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _prefetch_wheels(project, deploy) -> int:
    """Download aarch64 wheels for the project's requirements into wheels/.

    The wheels dir is auto-added to deploy.sync so it ships with the repo.
    The phone's build (via the Dockerfile preprocessor) then installs from
    local wheels instead of PyPI - dramatically faster on-device.
    """
    req_rel = deploy.requirements or "requirements.txt"
    req_path = os.path.join(project.source_dir, req_rel)
    if not os.path.exists(req_path):
        print(
            f"warning: pip_wheels enabled but '{req_rel}' not found - skipping prefetch",
            file=sys.stderr,
        )
        return 0
    wheels_dir = os.path.join(project.source_dir, "wheels")
    os.makedirs(wheels_dir, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--platform",
        "manylinux2014_aarch64",
        "--only-binary=:all:",
        "--dest",
        wheels_dir,
        "-r",
        req_path,
    ]
    print(f"  → prefetching aarch64 wheels ({req_rel})...")
    try:
        proc = subprocess.run(cmd, text=True)
    except OSError as exc:
        print(f"error: pip download failed to run: {exc}", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        print(
            "warning: some packages have no aarch64 wheel - the phone build will "
            "fall back to PyPI for those (still works, just slower)",
            file=sys.stderr,
        )
    if "wheels" not in deploy.sync:
        deploy.sync.append("wheels")
    return 0


def _cmd_deploy(args: argparse.Namespace) -> int:
    try:
        project = _load_project(args)
    except ComposeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    deploy = project.tuxcomp and project.tuxcomp.deploy
    if not deploy:
        print(
            "error: no x-tuxcomp.deploy configured in the compose file\n"
            "  example (host/port optional when using `tuxcomp remote`):\n"
            "    x-tuxcomp:\n"
            "      deploy:\n"
            "        remote_dir: ~/upload-tool\n"
            "        build: \"npm run build -- --configuration=docker\"\n"
            "        sync:\n"
            "          - dist/upload-tool/browser\n"
            "          - nginx.conf\n"
            "          - docker-compose.pi.yml",
            file=sys.stderr,
        )
        return 1

    target = _resolve_deploy_target(deploy, args)
    if not target:
        return 1
    host, port = target

    print(f"deploy {project.name} -> {host}:{port}")

    # 0. self-upgrade tuxcomp on the target so the phone always runs the
    #    latest tooling before any service is pushed.
    remote_version = _ssh_out(host, port, "tuxcomp --version 2>/dev/null || echo none")
    if remote_version and remote_version != f"tuxcomp {__version__}":
        print(f"  → older tuxcomp on target ({remote_version}); upgrading to tuxcomp {__version__}")
        if _ssh(host, port, "pip install --upgrade git+https://github.com/StylizedAce/TuxComp.git") != 0:
            print("error: could not upgrade tuxcomp on target", file=sys.stderr)
            return 1
        print(f"  → tuxcomp upgraded on target ({host})")
    elif not remote_version:
        print(f"  → tuxcomp not found on target; installing tuxcomp {__version__}")
        if _ssh(host, port, "pip install git+https://github.com/StylizedAce/TuxComp.git") != 0:
            print("error: could not install tuxcomp on target", file=sys.stderr)
            return 1
        print(f"  → tuxcomp installed on target ({host})")
    else:
        print(f"  → tuxcomp on target already up to date ({remote_version})")

    # 1. build locally (guarantees the pushed dist is fresh)
    if deploy.build:
        print(f"  → build: {deploy.build}")
        try:
            proc = subprocess.run(deploy.build, shell=True, text=True)
        except OSError as exc:
            print(f"error: build failed to run: {exc}", file=sys.stderr)
            return 1
        if proc.returncode != 0:
            print(f"error: local build failed (exit {proc.returncode}) - nothing pushed", file=sys.stderr)
            return 1

    # 1b. prefetch aarch64 wheels so the phone's pip installs use local wheels
    if deploy.pip_wheels:
        if _prefetch_wheels(project, deploy) != 0:
            return 1

    # 2. resolve the remote dir to an absolute path (scp's SFTP mode does not
    #    expand ~ on the remote side); default to ~/<project>
    remote_root = deploy.remote_dir or f"~/{project.name}"
    if remote_root in ("~", "~/") or remote_root.startswith("~/"):
        try:
            home_probe = subprocess.run(
                ["ssh", "-q", "-p", str(port), host, "echo $HOME"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            home = home_probe.stdout.strip()
            if not home:
                home = f"/data/data/com.termux/files/home"
            remote_root = home + remote_root[1:]
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"error: could not resolve remote home: {exc}", file=sys.stderr)
            return 1
    remote_root = remote_root.rstrip("/")

    # 3. make sure remote dir exists
    if _ssh(host, port, f"mkdir -p {shlex.quote(remote_root)}") != 0:
        print("error: could not create remote dir", file=sys.stderr)
        return 1

    # 4. always push the compose file itself, then the sync artifacts.
    #    For build: services the Dockerfile (and its requirements file) are
    #    required on the phone - auto-include them.
    compose_src = os.path.abspath(project.source_file)
    if _scp(host, port, compose_src, f"{remote_root}/{os.path.basename(compose_src)}") != 0:
        return 1
    auto_sync: list[str] = []
    for service in project.service_list():
        if service.build:
            rel_df = os.path.join(service.build.context, service.build.dockerfile)
            rel_df = rel_df.replace("\\", "/").lstrip("./")
            if rel_df not in deploy.sync and os.path.exists(os.path.join(project.source_dir, rel_df)):
                auto_sync.append(rel_df)
            req_rel = deploy.requirements or "requirements.txt"
            if req_rel not in deploy.sync:
                req_path = os.path.join(project.source_dir, req_rel)
                if os.path.exists(req_path):
                    auto_sync.append(req_rel)
    for rel in list(deploy.sync) + auto_sync:
        src = os.path.join(project.source_dir, rel)
        if not os.path.exists(src):
            print(f"error: sync path not found: {rel} ({src})", file=sys.stderr)
            return 1
        # remote paths are always POSIX (the phone is Linux) even on Windows
        rel_posix = rel.replace("\\", "/").rstrip("/")
        # preserve the relative path on the target so compose-relative binds resolve
        if os.path.isdir(src):
            # scp -r src into the EXISTING parent dir: scp creates the target
            # dir itself (basename of src), so target <parent>/<basename>.
            # Passing the target dir itself would nest (target/target).
            parent = remote_root + "/" + os.path.dirname(rel_posix).replace("\\", "/")
            parent = parent.rstrip("/")
            if _ssh(host, port, f"mkdir -p {shlex.quote(parent)}") != 0:
                print(f"error: could not create remote dir {parent}", file=sys.stderr)
                return 1
            if _scp(host, port, src, f"{parent}/") != 0:
                return 1
        else:
            dest = remote_root + "/" + rel_posix
            parent = os.path.dirname(dest) or remote_root
            if _ssh(host, port, f"mkdir -p {shlex.quote(parent)}") != 0:
                print(f"error: could not create remote dir {parent}", file=sys.stderr)
                return 1
            if _scp(host, port, src, dest) != 0:
                return 1

    # 5. up on the phone
    compose_base = os.path.basename(compose_src)
    return _ssh(
        host,
        port,
        f"cd {shlex.quote(remote_root)} && tuxcomp up -f {shlex.quote(compose_base)}",
    )


def main(args: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parsed = _parse_args(args)
    handlers = {
        "plan": _cmd_plan,
        "up": _cmd_up,
        "down": _cmd_down,
        "ps": _cmd_ps,
        "list": _cmd_list,
        "logs": _cmd_logs,
        "exec": _cmd_exec,
        "stop": _cmd_stop,
        "start": _cmd_start,
        "rmi": _cmd_rmi,
        "rebuild": _cmd_rebuild,
        "deploy": _cmd_deploy,
        "remote": _cmd_remote,
    }
    handler = handlers[parsed.command]
    try:
        return handler(parsed)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
