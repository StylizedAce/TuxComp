from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from tuxcomp.model import (
    BuildSpec,
    CloudflaredConfig,
    DeployConfig,
    Healthcheck,
    PortMapping,
    Project,
    ProjectTuxComp,
    Service,
    ServiceTuxComp,
    VolumeMount,
)


class ComposeError(Exception):
    pass


_VAR_RE = re.compile(r"\$(\$|\{[^}]*\}|[A-Za-z_][A-Za-z0-9_]*)")
_INT_RE = re.compile(r"^\d+$")
_DURATION_RE = re.compile(r"^(\d+)(ms|s|m|h)?$")


def parse_duration(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    match = _DURATION_RE.match(text)
    if not match:
        return default
    amount = int(match.group(1))
    unit = match.group(2) or "s"
    mult = {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return max(1, int(amount * mult))


def interpolate(value: Any, env: dict[str, str] | None = None) -> Any:
    env = env if env is not None else os.environ

    def _expand(match: re.Match) -> str:
        token = match.group(1)
        if token == "$":
            return "$"
        if token.startswith("{"):
            body = token[1:-1]
            name, sep, rest = body.partition(":")
            if not sep:
                name, sep, rest = body.partition("-") if "-" in body else (body, "", "")
                if sep == "?":
                    name, sep, rest = body.partition("?")
            if sep == ":" and rest.startswith("-"):
                return env.get(name) or rest[1:]
            if sep == ":" and rest.startswith("?"):
                if not env.get(name):
                    raise ComposeError(f"Required env var ${name} is not set")
                return env[name]
            if sep == "-":
                return env.get(name) if env.get(name) is not None else rest
            if sep == "?":
                if env.get(name) is None:
                    raise ComposeError(f"Required env var ${name} is not set")
                return env.get(name)
            return env.get(name, "")
        return env.get(token, "")

    if isinstance(value, str):
        return _VAR_RE.sub(_expand, value)
    if isinstance(value, list):
        return [interpolate(v, env) for v in value]
    if isinstance(value, dict):
        return {k: interpolate(v, env) for k, v in value.items()}
    return value


def _coerce_env(value: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            result[str(k)] = "" if v is None else str(v)
        return result
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and "=" in item:
                k, _, v = item.partition("=")
                result[k.strip()] = v
        return result
    return result


def _parse_ports(raw: Any) -> list[PortMapping]:
    ports: list[PortMapping] = []
    if not raw:
        return ports
    for item in raw:
        if isinstance(item, dict):
            host = str(item.get("published", item.get("host", "")))
            container = str(item.get("target", item.get("container", "")))
            protocol = str(item.get("protocol", "tcp"))
        else:
            text = str(item)
            protocol = "udp" if "/udp" in text else "tcp"
            text = text.split("/")[0]
            if ":" in text:
                host, container = text.rsplit(":", 1)
            else:
                host, container = "", text
        ports.append(PortMapping(host=host, container=container, protocol=protocol))
    return ports


def _parse_volumes(raw: Any, declared: dict[str, dict]) -> list[VolumeMount]:
    volumes: list[VolumeMount] = []
    if not raw:
        return volumes
    for item in raw:
        if isinstance(item, dict):
            source = str(item.get("source", ""))
            target = str(item.get("target", item.get("destination", "")))
            ro = bool(item.get("read_only", False))
            volumes.append(
                VolumeMount(
                    source=source,
                    target=target,
                    read_only=ro,
                    is_named=bool(source and source in declared),
                )
            )
            continue
        text = str(item)
        ro = text.endswith(":ro")
        if ro:
            text = text[:-3]
        parts = text.split(":")
        if len(parts) == 1:
            volumes.append(VolumeMount(source="", target=parts[0], read_only=ro, is_anonymous=True))
        elif len(parts) == 2:
            src, tgt = parts
            volumes.append(
                VolumeMount(
                    source=src,
                    target=tgt,
                    read_only=ro,
                    is_named=src in declared,
                )
            )
        else:
            raise ComposeError(f"Unsupported volume syntax: {item!r}")
    return volumes


def _parse_healthcheck(raw: Any) -> Healthcheck | None:
    if not raw:
        return None
    if raw is False:
        return None
    if isinstance(raw, dict):
        test = raw.get("test")
        if isinstance(test, str):
            test = ["CMD-SHELL", test]
        elif not isinstance(test, list):
            test = ["CMD-SHELL", "true"]
        return Healthcheck(
            test=[str(t) for t in test],
            interval=parse_duration(raw.get("interval"), 10),
            timeout=parse_duration(raw.get("timeout"), 5),
            retries=int(raw.get("retries", 10)),
            start_period=parse_duration(raw.get("start_period"), 0),
        )
    return None


def _parse_depends_on(raw: Any) -> dict[str, dict]:
    result: dict[str, dict] = {}
    if isinstance(raw, list):
        for name in raw:
            result[str(name)] = {}
    elif isinstance(raw, dict):
        for name, spec in raw.items():
            result[str(name)] = dict(spec or {})
    return result


def _parse_command(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return ["/bin/sh", "-c", raw]
    return [str(x) for x in raw]


def _parse_tuxcomp_service(raw: dict) -> ServiceTuxComp | None:
    ext = raw.get("x-tuxcomp") or {}
    if not isinstance(ext, dict) or not ext:
        return None
    packages = ext.get("packages") or []
    if isinstance(packages, str):
        packages = [packages]
    return ServiceTuxComp(
        role=str(ext["role"]) if ext.get("role") else None,
        distro=str(ext["distro"]) if ext.get("distro") else None,
        packages=[str(p) for p in packages],
        from_golden=str(ext["from_golden"]) if ext.get("from_golden") else None,
        reuse=str(ext["reuse"]) if ext.get("reuse") else None,
        raw=bool(ext.get("raw", False)),
    )


def _parse_tuxcomp_project(raw: dict) -> ProjectTuxComp | None:
    ext = raw.get("x-tuxcomp") or {}
    if not isinstance(ext, dict) or not ext:
        return None
    cloudflared = ext.get("cloudflared") or {}
    cf = None
    if isinstance(cloudflared, dict):
        cf = CloudflaredConfig(
            token=str(cloudflared["token"]) if cloudflared.get("token") else None,
            tunnel=str(cloudflared["tunnel"]) if cloudflared.get("tunnel") else None,
        )
    elif cloudflared is True:
        cf = CloudflaredConfig()
    deploy = ext.get("deploy") or {}
    dp = None
    if isinstance(deploy, dict) and deploy:
        sync = deploy.get("sync") or []
        if isinstance(sync, str):
            sync = [sync]
        dp = DeployConfig(
            host=str(deploy.get("host") or ""),
            port=int(deploy.get("port", 22)),
            remote_dir=str(deploy["remote_dir"]) if deploy.get("remote_dir") else None,
            build=str(deploy["build"]) if deploy.get("build") else None,
            sync=[str(p) for p in sync],
            pip_wheels=bool(deploy.get("pip_wheels", False)),
            requirements=str(deploy["requirements"]) if deploy.get("requirements") else None,
            env_sync=str(deploy["env_sync"]) if deploy.get("env_sync") else None,
        )
    return ProjectTuxComp(cloudflared=cf, deploy=dp)


def _parse_service(name: str, raw: dict, declared_volumes: dict[str, dict], source_dir: str) -> Service:
    image = raw.get("image")
    build_raw = raw.get("build")
    build = None
    if build_raw:
        if isinstance(build_raw, str):
            build = BuildSpec(context=str(build_raw))
        else:
            ctx = build_raw.get("context", ".")
            if not isinstance(ctx, str):
                ctx = "."
            build = BuildSpec(
                context=str(ctx),
                dockerfile=str(build_raw.get("dockerfile", "Dockerfile")),
                args={str(k): str(v) for k, v in (build_raw.get("args") or {}).items()},
            )
    if not image and not build:
        raise ComposeError(f"service '{name}' requires 'image' or 'build'")

    env = _coerce_env(raw.get("environment"))
    env_file = raw.get("env_file") or []
    if isinstance(env_file, str):
        env_file = [env_file]
    env_file = [str(f) for f in env_file]
    for f in env_file:
        path = Path(source_dir) / f
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                env.setdefault(k.strip(), v)

    service = Service(
        name=name,
        image=str(image) if image else None,
        build=build,
        container_name=str(raw["container_name"]) if raw.get("container_name") else None,
        command=_parse_command(raw.get("command")),
        entrypoint=_parse_command(raw.get("entrypoint")),
        environment=env,
        env_file=env_file,
        ports=_parse_ports(raw.get("ports")),
        volumes=_parse_volumes(raw.get("volumes"), declared_volumes),
        depends_on=_parse_depends_on(raw.get("depends_on")),
        restart=str(raw.get("restart", "no")),
        profiles=[str(p) for p in (raw.get("profiles") or [])],
        healthcheck=_parse_healthcheck(raw.get("healthcheck")),
        expose=[str(p) for p in (raw.get("expose") or [])],
        tuxcomp=_parse_tuxcomp_service(raw),
    )
    return service


def parse_compose_file(path: str | Path, env: dict[str, str] | None = None) -> Project:
    path = Path(path)
    if not path.exists():
        raise ComposeError(f"compose file not found: {path}")
    if env is None:
        env = dict(os.environ)
        dotenv = path.parent / ".env"
        if dotenv.exists():
            for line in dotenv.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env.setdefault(key.strip(), value.strip())
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ComposeError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict) or "services" not in raw:
        raise ComposeError(f"{path}: missing top-level 'services' key")

    raw = interpolate(raw, env)
    declared_volumes: dict[str, dict] = raw.get("volumes") or {}
    parent_name = path.parent.name or Path.cwd().name
    name = str(raw.get("name") or parent_name)
    source_dir = str(path.parent)

    services = {
        str(sname): _parse_service(str(sname), sraw, declared_volumes, source_dir)
        for sname, sraw in (raw.get("services") or {}).items()
    }
    if not services:
        raise ComposeError(f"{path}: no services defined")

    return Project(
        name=name,
        services=services,
        volumes=declared_volumes,
        networks=raw.get("networks") or {},
        source_dir=source_dir,
        source_file=str(path),
        tuxcomp=_parse_tuxcomp_project(raw),
    )