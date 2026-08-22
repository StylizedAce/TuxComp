from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Healthcheck:
    test: list[str]
    interval: int = 10
    timeout: int = 5
    retries: int = 10
    start_period: int = 30


@dataclass
class VolumeMount:
    source: str
    target: str
    read_only: bool = False
    is_named: bool = False
    is_anonymous: bool = False


@dataclass
class PortMapping:
    host: str
    container: str
    protocol: str = "tcp"


@dataclass
class BuildSpec:
    context: str
    dockerfile: str = "Dockerfile"
    args: dict[str, str] = field(default_factory=dict)


@dataclass
class ServiceTuxComp:
    """Per-service x-tuxcomp extension (TuxComp provisioning hints)."""

    role: Optional[str] = None
    distro: Optional[str] = None
    packages: list[str] = field(default_factory=list)
    from_golden: Optional[str] = None
    reuse: Optional[str] = None
    raw: bool = False


@dataclass
class CloudflaredConfig:
    token: Optional[str] = None
    tunnel: Optional[str] = None


@dataclass
class DeployConfig:
    """Top-level x-tuxcomp.deploy: how to push this project to a target phone.

    host/port are optional — `tuxcomp remote` config can supply them (set once,
    inferred for the future). build/sync/remote_dir stay project-local.

    pip_wheels: pre-download aarch64 wheels on the PC and push them, so the
    phone's pip installs come from local wheels instead of PyPI (much faster).
    """

    host: str = ""
    port: int = 22
    remote_dir: Optional[str] = None
    build: Optional[str] = None
    sync: list[str] = field(default_factory=list)
    pip_wheels: bool = False
    requirements: Optional[str] = None


@dataclass
class ProjectTuxComp:
    """Top-level x-tuxcomp extension (project-wide TuxComp settings)."""

    cloudflared: Optional[CloudflaredConfig] = None
    deploy: Optional[DeployConfig] = None


@dataclass
class Service:
    name: str
    image: Optional[str] = None
    build: Optional[BuildSpec] = None
    container_name: Optional[str] = None
    command: Optional[list[str]] = None
    entrypoint: Optional[list[str]] = None
    environment: dict[str, str] = field(default_factory=dict)
    env_file: list[str] = field(default_factory=list)
    ports: list[PortMapping] = field(default_factory=list)
    volumes: list[VolumeMount] = field(default_factory=list)
    depends_on: dict[str, dict] = field(default_factory=dict)
    restart: str = "no"
    profiles: list[str] = field(default_factory=list)
    healthcheck: Optional[Healthcheck] = None
    expose: list[str] = field(default_factory=list)
    tuxcomp: Optional[ServiceTuxComp] = None

    def container(self) -> str:
        return self.container_name or self.name


@dataclass
class Project:
    name: str
    services: dict[str, Service] = field(default_factory=dict)
    volumes: dict[str, dict] = field(default_factory=dict)
    networks: dict[str, dict] = field(default_factory=dict)
    source_dir: str = "."
    source_file: str = "docker-compose.yml"
    tuxcomp: Optional[ProjectTuxComp] = None

    def service_list(self) -> list[Service]:
        return list(self.services.values())

    def get(self, name: str) -> Optional[Service]:
        return self.services.get(name)