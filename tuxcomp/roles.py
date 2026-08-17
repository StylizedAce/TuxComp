from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Recipe:
    distro: str
    packages: list[str] = field(default_factory=list)
    role: str = "base"
    note: str = ""


KNOWN_INFRA_IMAGES: dict[str, Recipe] = {
    "mysql": Recipe(distro="ubuntu", packages=["mysql-server"], role="db", note="mysql-server from ubuntu apt"),
    "mariadb": Recipe(distro="debian", packages=["mariadb-server"], role="db", note="mariadb-server from debian apt"),
    "postgres": Recipe(distro="ubuntu", packages=["postgresql"], role="db", note="postgresql from ubuntu apt"),
    "redis": Recipe(distro="debian", packages=["redis-server"], role="cache", note="redis-server from debian apt"),
    "nginx": Recipe(distro="debian", packages=["nginx"], role="web", note="nginx from debian apt"),
    "alpine": Recipe(distro="alpine", packages=[], role="base", note="alpine rootfs (no extra packages)"),
    "ubuntu": Recipe(distro="ubuntu", packages=[], role="base", note="ubuntu rootfs (no extra packages)"),
    "debian": Recipe(distro="debian", packages=[], role="base", note="debian rootfs (no extra packages)"),
}


def image_base_name(image: str) -> str:
    name = image.split("/")[-1]
    name = name.split(":")[0]
    return name.lower()


def recipe_for_image(image: str) -> Optional[Recipe]:
    return KNOWN_INFRA_IMAGES.get(image_base_name(image))