from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from tuxcomp.model import Project, Service
from tuxcomp.roles import recipe_for_image

CLOUDFLARED_ARM64_URL = (
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64"
)
DEFAULT_PD_CONTAINERS_DIR = "/data/data/com.termux/files/usr/var/lib/proot-distro/containers"


class PlanError(Exception):
    pass


@dataclass
class Step:
    kind: str
    service: str
    title: str
    command: list[str]
    note: Optional[str] = None
    deps: list[str] = field(default_factory=list)


@dataclass
class Plan:
    project_name: str
    steps: list[Step]

    def for_service(self, service: str) -> list[Step]:
        return [s for s in self.steps if s.service == service]

    def kinds(self) -> list[str]:
        return [s.kind for s in self.steps]


def _container_name(service: Service, project_name: str) -> str:
    return service.container() or f"{project_name}-{service.name}"


def _pd_containers_dir() -> str:
    return os.environ.get("TUXCOMP_PD_DIR", DEFAULT_PD_CONTAINERS_DIR)


def _topo_sort(project: Project, active: list[str]) -> list[Service]:
    visited: dict[str, int] = {}
    order: list[Service] = []

    def visit(name: str, stack: set[str]) -> None:
        if visited.get(name) == 2:
            return
        if name in stack:
            raise PlanError(f"circular depends_on involving '{name}'")
        visited[name] = 1
        stack.add(name)
        service = project.get(name)
        if service:
            for dep in service.depends_on:
                if dep in active:
                    visit(dep, stack)
        stack.discard(name)
        visited[name] = 2
        s = project.get(name)
        if s and name in active:
            order.append(s)

    for name in active:
        visit(name, set())
    return order


def build_plan(project: Project, profiles: list[str] | None = None, detach: bool = True) -> Plan:
    profiles = profiles or []
    steps: list[Step] = []

    active: list[str] = []
    for name, service in project.services.items():
        if not service.profiles:
            active.append(name)
        elif any(p in service.profiles for p in profiles):
            active.append(name)

    if not active:
        raise PlanError("no services match the requested profiles")

    ordered = _topo_sort(project, active)

    volume_dirs = volume_dirs_for(project)
    ensured_volumes: set[str] = set()

    def ensure_volume(v: str, service_name: str) -> None:
        if v in ensured_volumes:
            return
        ensured_volumes.add(v)
        steps.append(
            Step(
                kind="volume",
                service=service_name,
                title=f"ensure named volume {v}",
                command=["mkdir", "-p", volume_dirs[v]],
                note=f"volume '{v}' -> {volume_dirs[v]} (shared across services)",
            )
        )

    def bind_args_with_notes(service: Service) -> list[str]:
        args = bind_args(project, service, volume_dirs)
        for mount in service.volumes:
            if mount.read_only:
                steps.append(
                    Step(
                        kind="note",
                        service=service.name,
                        title="read-only bind mount",
                        command=[],
                        note=f"{mount.source}:{mount.target} declared :ro - proot binds are read-write; enforce ro via app config",
                    )
                )
        return args

    def start_command(service: Service, container: str) -> list[str]:
        return service_start_command(project, service, container, detach=detach)

    def run_command(service: Service, container: str) -> list[str]:
        cmd = ["proot-distro", "run", container]
        cmd += bind_args_with_notes(service)
        cmd += env_args(service)
        if detach:
            cmd.append("-d")
        if service.command:
            cmd += ["--", *service.command]
        return cmd

    for service in ordered:
        container = _container_name(service, project.name)
        tux = service.tuxcomp
        dep_names = [d for d in service.depends_on if d in active]

        # --- Provisioning: reuse / golden clone / role recipe / raw image / build ---
        run_target = container
        if tux and tux.reuse:
            target = tux.reuse
            steps.append(
                Step(
                    kind="reuse",
                    service=service.name,
                    title=f"reuse existing container {target}",
                    command=[],
                    note=f"service '{service.name}' maps to existing container '{target}' - no install, no provisioning",
                    deps=dep_names,
                )
            )
            run_target = target
        elif tux and tux.from_golden:
            steps.append(
                Step(
                    kind="clone",
                    service=service.name,
                    title=f"clone golden base {tux.from_golden} -> {container}",
                    command=[
                        "cp",
                        "-a",
                        os.path.join(_pd_containers_dir(), tux.from_golden),
                        os.path.join(_pd_containers_dir(), container),
                    ],
                    note=(
                        f"directory copy of container '{tux.from_golden}' "
                        f"(proot-distro restore has no -n flag). "
                        f"Requires {_pd_containers_dir()}/{tux.from_golden} to exist"
                    ),
                    deps=dep_names,
                )
            )
        elif tux and tux.role:
            recipe = None
            distro = tux.distro or "ubuntu"
            packages = tux.packages
            steps.append(
                Step(
                    kind="install",
                    service=service.name,
                    title=f"install {distro} for role '{tux.role}'",
                    command=["proot-distro", "install", distro, "-n", container],
                    deps=dep_names,
                )
            )
            if packages:
                steps.append(
                    Step(
                        kind="provision",
                        service=service.name,
                        title=f"provision role '{tux.role}': apt install {' '.join(packages)}",
                        command=_apt_install_command(container, packages),
                        deps=[service.name],
                    )
                )
        elif service.image and recipe_for_image(service.image) and not (tux and tux.raw):
            recipe = recipe_for_image(service.image)
            steps.append(
                Step(
                    kind="install",
                    service=service.name,
                    title=f"install {recipe.distro} for {service.image} (role '{recipe.role}')",
                    command=["proot-distro", "install", recipe.distro, "-n", container],
                    deps=dep_names,
                    note=(
                        f"image '{service.image}' -> role recipe: {recipe.note}. "
                        f"Set x-tuxcomp.raw: true on the service to force literal image install"
                    ),
                )
            )
            if recipe.packages:
                steps.append(
                    Step(
                        kind="provision",
                        service=service.name,
                        title=f"provision {service.image}: apt install {' '.join(recipe.packages)}",
                        command=_apt_install_command(container, recipe.packages),
                        deps=[service.name],
                        note=f"installs {recipe.note}",
                    )
                )
            if not service.command and not service.entrypoint:
                steps.append(
                    Step(
                        kind="note",
                        service=service.name,
                        title="role-mapped service has no command",
                        command=[],
                        note=(
                            f"'{service.image}' maps to distro + apt packages, so the image's "
                            f"entrypoint is lost. Add a service `command:` (e.g. mysqld, "
                            f"redis-server) or nothing will run at start"
                        ),
                    )
                )
        elif service.image:
            steps.append(
                Step(
                    kind="install",
                    service=service.name,
                    title=f"install image {service.image}",
                    command=["proot-distro", "install", service.image, "-n", container],
                    deps=dep_names,
                )
            )
        elif service.build:
            args: list[str] = []
            for k, v in service.build.args.items():
                args.extend(["--build-arg", f"{k}={v}"])
            # compose semantics: build context + dockerfile are relative to the
            # compose file's directory, not the shell CWD (proot-distro resolves
            # them against CWD, so make them absolute against source_dir)
            context = service.build.context
            if not Path(context).is_absolute():
                context = str(Path(project.source_dir) / context)
            dockerfile = (
                str(Path(context) / service.build.dockerfile)
                if not Path(service.build.dockerfile).is_absolute()
                else service.build.dockerfile
            )
            steps.append(
                Step(
                    kind="build",
                    service=service.name,
                    title=f"build {dockerfile} (context {context})",
                    command=[
                        "proot-distro",
                        "build",
                        context,
                        "-f",
                        dockerfile,
                        *args,
                        "--install-as",
                        container,
                    ],
                    deps=dep_names,
                )
            )
            provisioned = False
        else:
            raise PlanError(f"service '{service.name}' has neither image nor build")

        for v in service.volumes:
            if v.is_named:
                ensure_volume(v.source, service.name)

        if not (tux and tux.reuse):
            steps.append(
                Step(
                    kind="hosts",
                    service=service.name,
                    title=f"register service names in {container} /etc/hosts",
                    command=_hosts_command(project, active, container),
                    deps=[service.name],
                    note=(
                        f"aliases 127.0.0.1 {' '.join(active)} (service + container names) "
                        f"so compose-style hostnames resolve over shared localhost (no DNS daemon)"
                    ),
                )
            )

        if service.entrypoint or service.command:
            start = Step(
                kind="start",
                service=service.name,
                title=f"start {service.name} ({run_target})",
                command=start_command(service, run_target),
                deps=dep_names,
                note=_start_note(service)
                + f"; logs captured at /var/log/tuxcomp/{run_target}.log (tuxcomp logs {run_target})",
            )
        else:
            start = Step(
                kind="start",
                service=service.name,
                title=f"start {service.name} ({run_target}) via image entrypoint",
                command=run_command(service, run_target),
                deps=dep_names,
                note=_start_note(service)
                + "; no command: defined - logs go to /dev/null; add a service command: to capture logs",
            )
        steps.append(start)

        if service.healthcheck:
            steps.append(
                Step(
                    kind="health",
                    service=service.name,
                    title=f"wait for {service.name} health",
                    command=_health_command(service, run_target),
                    deps=[service.name],
                    note=(
                        f"poll every {service.healthcheck.interval}s, "
                        f"up to {service.healthcheck.retries} tries "
                        f"(start_period {service.healthcheck.start_period}s)"
                    ),
                )
            )

    if project.tuxcomp and project.tuxcomp.cloudflared:
        cf = project.tuxcomp.cloudflared
        tunnel_container = cf.container or "tuxcomp-cloudflared"
        # Shared, named proot container: one cloudflared instance per tunnel
        # serves every exposed service on the device. Android's /etc/resolv.conf
        # is read-only, so Go's DNS resolver (used by cloudflared) fails on the
        # Termux host shell; inside a proot container DNS works. Named
        # containers allow multiple tunnels (different accounts) side by side.
        # The runner skips the install step when the container already exists.
        steps.append(
            Step(
                kind="install",
                service="__tunnel__",
                title=f"ensure proot container {tunnel_container}",
                command=["proot-distro", "install", "debian", "-n", tunnel_container],
                note="shared cloudflared container - created once, reused by every project",
            )
        )
        steps.append(
            Step(
                kind="tunnel",
                service="__tunnel__",
                title=f"install cloudflared binary in {tunnel_container}",
                command=_tunnel_install_command(tunnel_container),
                note=(
                    f"installs cloudflared arm64 ({CLOUDFLARED_ARM64_URL}) into "
                    f"proot container {tunnel_container} (DNS works inside proot), "
                    f"replacing it when a newer release is out - a stale connector "
                    f"stays alive but drops all traffic, so it is version-checked "
                    f"on every deploy"
                ),
            )
        )
        if cf.token:
            # Owning project: always overwrite the token (last deploy wins).
            steps.append(
                Step(
                    kind="tunnel",
                    service="__tunnel__",
                    title=f"write tunnel token in {tunnel_container}",
                    command=_tunnel_token_command(tunnel_container, cf.token),
                    note=(
                        f"token stored at /root/.tuxcomp/tunnel-token (chmod 600) inside "
                        f"{tunnel_container}; always overwritten on deploy"
                    ),
                )
            )
        steps.append(
            Step(
                kind="tunnel",
                service="__tunnel__",
                title=f"verify tunnel token in {tunnel_container}",
                command=_tunnel_token_ensure_command(tunnel_container, cf.token),
                note=(
                    "checks that the token file exists inside the container; "
                    "rewrites it if missing (e.g. after container recreation)"
                ),
            )
        )
        steps.append(
            Step(
                kind="tunnel",
                service="__tunnel__",
                title=f"start cloudflared daemon in {tunnel_container}",
                command=tunnel_start_command(tunnel_container),
                note=(
                    f"starts cloudflared daemon if it is not already running "
                    f"(idempotent - repeated deploys never stack duplicate connectors; "
                    f"survives ssh close via proot -d); logs to the session - view with "
                    f"`tuxcomp logs {tunnel_container}`"
                    + (f"; tunnel: {cf.tunnel}" if cf.tunnel else "")
                ),
            )
        )
        steps.append(
            Step(
                kind="health",
                service="__tunnel__",
                title=f"wait for cloudflared health in {tunnel_container}",
                command=_tunnel_health_command(tunnel_container),
                note=(
                    "polls the cloudflared process inside the container; "
                    "Ctrl+C skips without undoing anything"
                ),
            )
        )

    return Plan(project_name=project.name, steps=steps)


def _q_sh(s: str) -> str:
    return shlex.quote(s)


def service_start_command(
    project: Project,
    service: Service,
    container: str,
    detach: bool = True,
) -> list[str]:
    """Full proot-distro login command that starts a service container (log-captured).

    Shared by the up plan and the `start` command (replay via registry).
    """
    cmd = ["proot-distro", "login", container]
    cmd += bind_args(project, service, volume_dirs_for(project))
    cmd += env_args(service)
    if detach:
        cmd.append("-d")
    cmd.append("--")
    inner: list[str] = []
    if service.entrypoint:
        inner += service.entrypoint
    if service.command:
        inner += service.command
    else:
        inner = ["/bin/sh", "-c", "echo '[tuxcomp] container started'; exec sleep infinity"]
    logfile = f"/var/log/tuxcomp/{container}.log"
    shell = (
        f"mkdir -p /var/log/tuxcomp && "
        f"exec {' '.join(_q_sh(a) for a in inner)} >> {logfile} 2>&1"
    )
    cmd += ["/bin/sh", "-c", shell]
    return cmd


def volume_dirs_for(project: Project) -> dict[str, str]:
    return {
        v: os.path.expanduser(f"~/.tuxcomp/volumes/{project.name}-{v}")
        for v in project.volumes
    }


def env_args(service: Service) -> list[str]:
    args: list[str] = []
    for k, v in service.environment.items():
        args.extend(["-e", f"{k}={v}"])
    return args


def bind_args(project: Project, service: Service, volume_dirs: dict[str, str]) -> list[str]:
    args: list[str] = []
    for mount in service.volumes:
        if mount.is_named:
            src = volume_dirs[mount.source]
        elif mount.is_anonymous:
            src = os.path.expanduser(f"~/.tuxcomp/volumes/{project.name}-{service.name}-anon")
        else:
            src = mount.source
            if not os.path.isabs(src) and not src.startswith("~"):
                # compose semantics: relative bind sources resolve against the
                # compose file's directory; make them absolute so proot resolves
                # them against any CWD
                src = os.path.abspath(os.path.join(project.source_dir, src))
        args.extend(["-b", f"{src}:{mount.target}"])
    return args


def _apt_install_command(container: str, packages: list[str]) -> list[str]:
    shell = (
        "DEBIAN_FRONTEND=noninteractive apt-get update -qq && "
        f"DEBIAN_FRONTEND=noninteractive apt-get install -y -qq {' '.join(packages)}"
    )
    return ["proot-distro", "login", container, "-e", "DEBIAN_FRONTEND=noninteractive", "--", "/bin/sh", "-c", shell]


def _hosts_command(project: Project, active: list[str], container: str) -> list[str]:
    # register both compose service names AND container names so apps can use
    # either (docker DNS supports both; proot needs explicit /etc/hosts entries).
    # The tuxcomp line is REPLACED on every up so name sets stay in sync.
    names: list[str] = []
    for service_name in active:
        service = project.get(service_name)
        names.append(service_name)
        if service:
            cname = _container_name(service, project.name)
            if cname not in names:
                names.append(cname)
    line = "127.0.0.1 " + " ".join(names) + " # tuxcomp"
    shell = (
        f"grep -v '# tuxcomp' /etc/hosts > /etc/hosts.tmp && "
        f"mv /etc/hosts.tmp /etc/hosts; "
        f"echo '{line}' >> /etc/hosts"
    )
    return ["proot-distro", "login", container, "--", "/bin/sh", "-c", shell]


def _tunnel_install_command(container: str) -> list[str]:
    """Install the cloudflared binary, refreshing it when a newer release exists.

    A connector that has gone stale keeps its process alive while silently
    dropping every connection, so the process-alive health check keeps passing
    while all public hostnames hang. Comparing the installed version against the
    latest GitHub release on every deploy keeps it fresh.

    The download lands on a temp path and is only swapped in once it reports a
    version, so a failed or truncated download can never break a working tunnel.
    If GitHub cannot be reached an already-installed binary is kept as-is. When
    the binary is replaced the running daemon is stopped, so the following
    "start cloudflared daemon" step brings the tunnel up on the new version.
    """
    shell = (
        f"BIN=/usr/local/bin/cloudflared; "
        f"URL={CLOUDFLARED_ARM64_URL}; "
        # Installed version, or the literal "unknown" when there is no binary.
        f"cur=unknown; "
        f'if [ -x "$BIN" ]; then '
        f'cur=$("$BIN" --version 2>/dev/null | '
        f"sed -n 's/^cloudflared version \\([^ ]*\\).*/\\1/p'); "
        f'[ -n "$cur" ] || cur=unknown; fi; '
        # Latest published tag, straight from the releases API.
        f"latest=$(curl -fsSL https://api.github.com/repos/cloudflare/cloudflared/releases/latest "
        f"2>/dev/null | sed -n 's/.*\"tag_name\"[^\"]*\"\\([^\"]*\\)\".*/\\1/p' | sed 's/^v//'); "
        f'if [ -z "$latest" ]; then '
        f'if [ -x "$BIN" ]; then '
        f'echo "cloudflared $cur kept - could not check the latest release"; exit 0; fi; '
        f'echo "cloudflared: cannot reach GitHub to download the binary" >&2; exit 1; fi; '
        f'if [ -x "$BIN" ] && [ "$cur" = "$latest" ]; then '
        f'echo "cloudflared $cur is up to date"; exit 0; fi; '
        f'if curl -fsSL -o "$BIN.new" "$URL" && chmod +x "$BIN.new" '
        f'&& "$BIN.new" --version >/dev/null 2>&1 && mv -f "$BIN.new" "$BIN"; then '
        f'echo "cloudflared $cur -> $latest"; '
        # Anchor on the binary path so pkill cannot match proot\'s own wrapper,
        # which also contains the string "cloudflared".
        f"pkill -f '^/usr/local/bin/cloudflared' >/dev/null 2>&1 || true; sleep 2; "
        f'else rm -f "$BIN.new"; '
        f'echo "cloudflared download failed - keeping $cur" >&2; exit 1; fi'
    )
    return ["proot-distro", "login", container, "--", "/bin/sh", "-c", shell]


def _host_token_path(container: str) -> str:
    """Per-container host token path: ~/.tuxcomp/tokens/<container>."""
    return f"/data/data/com.termux/files/home/.tuxcomp/tokens/{container}"


def _tunnel_token_command(container: str, token: str) -> list[str]:
    """Write the tunnel token inside the container AND persist to host.

    Two writes happen:
      1. Inside the container at /root/.tuxcomp/tunnel-token (cloudflared reads this)
      2. On the host at ~/.tuxcomp/tokens/<container> (survives container rmi)
    """
    host_path = _host_token_path(container)
    shell = (
        f"mkdir -p /root/.tuxcomp && "
        f"printf '%s' '{token}' > /root/.tuxcomp/tunnel-token && "
        f"chmod 600 /root/.tuxcomp/tunnel-token && "
        f"mkdir -p $(dirname {host_path}) && "
        f"printf '%s' '{token}' > {host_path} && "
        f"chmod 600 {host_path} && "
        f"echo 'token written to container + host ({host_path})'"
    )
    return ["proot-distro", "login", container, "--", "/bin/sh", "-c", shell]


def _tunnel_token_ensure_command(container: str, token: str | None) -> list[str]:
    """Verify the token file exists inside the container; restore if missing.

    Runs on the Termux host (not inside the container) so it can access the
    host filesystem at ~/.tuxcomp/tokens/<container> and copy it in.
    Handles the case where the container was recreated (e.g. rmi + ensure)
    and lost its filesystem state.

    When the owning project's token is known it's passed in and written
    directly.  Otherwise the per-container host file is used as the source
    of truth.
    """
    host_path = _host_token_path(container)
    if token:
        # Token known: idempotent write (skip if file already present).
        shell = (
            f"proot-distro login {container} -- test -f /root/.tuxcomp/tunnel-token || "
            f"(proot-distro login {container} -- sh -c "
            f"\"mkdir -p /root/.tuxcomp && printf '%s' '{token}' > /root/.tuxcomp/tunnel-token && "
            f"chmod 600 /root/.tuxcomp/tunnel-token\" && "
            f"echo 'token rewritten (was missing)')"
        )
    else:
        # No inline token: copy from per-container host file if present.
        shell = (
            f"proot-distro login {container} -- test -f /root/.tuxcomp/tunnel-token || "
            f"(test -f {host_path} && "
            f"cat {host_path} | proot-distro login {container} -- sh -c "
            f"\"mkdir -p /root/.tuxcomp && cat > /root/.tuxcomp/tunnel-token && chmod 600 /root/.tuxcomp/tunnel-token\" && "
            f"echo 'token restored from host ({host_path})') || "
            f"echo 'WARN: tunnel-token missing — deploy the owning project (with a token) first'"
        )
    return ["/bin/sh", "-c", shell]


def tunnel_start_command(container: str) -> list[str]:
    """Start cloudflared as the container's foreground process, exactly once.

    The -d flag detaches the proot session from the terminal, but the
    container stays alive as long as cloudflared runs. This is fundamentally
    more robust than background-job approaches: no nohup, no disown, no
    orphan risk — the container lifecycle IS cloudflared's lifecycle.

    Idempotent: when a daemon is already running the step is a no-op. Without
    that guard every `tuxcomp up`/`rebuild` stacked another connector onto the
    same tunnel, so Cloudflare showed several replicas for one device. The
    guard is anchored on the binary path so it can never match proot's own
    wrapper, whose command line also contains the word "cloudflared".

    If the binary was refreshed, the install step has already stopped the old
    daemon, so this still brings the tunnel up on the new version.

    IMPORTANT: do NOT pass --logfile here. In detached proot sessions on
    Termux, cloudflared exits immediately when asked to open a log file
    (the redirect fails and the process dies in ~2s). Without --logfile it
    survives and logs to the session (view via `tuxcomp logs <container>`).
    """
    shell = (
        f"if pgrep -f '^/usr/local/bin/cloudflared tunnel run' >/dev/null 2>&1; then "
        f"echo 'cloudflared already running - not starting a second one'; exit 0; fi; "
        f"exec /usr/local/bin/cloudflared tunnel run --token-file /root/.tuxcomp/tunnel-token"
    )
    return ["proot-distro", "login", container, "-d", "--", "/bin/sh", "-c", shell]


def _tunnel_health_command(container: str) -> list[str]:
    """Health probe: the cloudflared process must be alive inside the container."""
    return ["proot-distro", "login", container, "--", "/bin/sh", "-c", "pgrep -f '[c]loudflared tunnel run' >/dev/null"]


def _start_note(service: Service) -> str:
    notes: list[str] = []
    if service.ports:
        host_ports = ",".join(p.host or p.container for p in service.ports)
        notes.append(
            f"ports {host_ports} - proot shares localhost: container port is reachable "
            f"at 127.0.0.1:{host_ports}"
        )
    if service.restart not in ("no", "none"):
        notes.append(f"restart policy '{service.restart}' tracked in state (V1: manual restart)")
    if service.profiles:
        notes.append(f"profile(s): {','.join(service.profiles)} - starts only with matching --profile")
    if service.tuxcomp and service.tuxcomp.reuse:
        notes.append(f"reuses existing container '{service.tuxcomp.reuse}' (shared, do not stop via down)")
    return "; ".join(notes)


def _health_command(service: Service, container: str) -> list[str]:
    test = service.healthcheck.test if service.healthcheck else ["CMD", "true"]
    if test[0] == "CMD-SHELL":
        return ["proot-distro", "login", container, "--", "/bin/sh", "-c", " ".join(test[1:])]
    if test[0] == "CMD":
        return ["proot-distro", "login", container, "--", *test[1:]]
    return ["proot-distro", "login", container, "--", *test]


def down_plan(project: Project, active: set[str] | None = None, remove: bool = False) -> Plan:
    active = active or set(project.services.keys())
    steps: list[Step] = []
    ordered = list(project.services.values())
    for service in reversed(ordered):
        if service.name not in active:
            continue
        if service.tuxcomp and service.tuxcomp.reuse:
            steps.append(
                Step(
                    kind="note",
                    service=service.name,
                    title=f"skip {service.name} (reuses shared container '{service.tuxcomp.reuse}')",
                    command=[],
                )
            )
            continue
        container = _container_name(service, project.name)
        steps.append(
            Step(
                kind="stop",
                service=service.name,
                title=f"stop {service.name} ({container})",
                command=["proot-distro", "kill", container],
            )
        )
        if remove:
            steps.append(
                Step(
                    kind="remove",
                    service=service.name,
                    title=f"remove container {container}",
                    command=["proot-distro", "remove", container],
                )
            )
    # Shared tunnel containers are NOT stopped by project down — they serve
    # every project on the device. Stop one explicitly:
    #   tuxcomp down tuxcomp-cloudflared        (or any named tunnel container)
    return Plan(project_name=project.name, steps=steps)