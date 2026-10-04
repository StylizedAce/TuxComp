import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from tuxcomp import planner
from tuxcomp.model import Project, Service, ServiceTuxComp
from tuxcomp.parser import ComposeError, parse_compose_file


@pytest.fixture
def allowed_two(monkeypatch):
    monkeypatch.setattr(planner, "_allowed_cpus", lambda: 2)


def _compose(tmp_path, threads):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "services:\n"
        "  app:\n"
        "    image: app:latest\n"
        "    x-tuxcomp:\n"
        f"      resources:\n"
        f"        threads: {threads}\n",
        encoding="utf-8",
    )
    return parse_compose_file(compose)


def test_threads_auto_parsed(tmp_path):
    project = _compose(tmp_path, "auto")
    assert project.get("app").tuxcomp.threads == "auto"


def test_threads_integer_parsed(tmp_path):
    project = _compose(tmp_path, 4)
    assert project.get("app").tuxcomp.threads == "4"


def test_threads_invalid_rejected(tmp_path):
    with pytest.raises(ComposeError, match="threads"):
        _compose(tmp_path, "many")


def test_resource_env_auto_uses_allowed_cpus(allowed_two):
    service = Service(name="app", image="app:latest", tuxcomp=ServiceTuxComp(threads="auto"))
    args = planner.resource_env(service)
    assert args == [
        "-e", "OMP_NUM_THREADS=2",
        "-e", "OPENBLAS_NUM_THREADS=2",
        "-e", "MKL_NUM_THREADS=2",
        "-e", "NUMEXPR_NUM_THREADS=2",
        "-e", "VECLIB_MAXIMUM_THREADS=2",
        "-e", "UV_THREADPOOL_SIZE=2",
    ]


def test_resource_env_explicit_and_absent(allowed_two):
    explicit = Service(name="app", image="app:latest", tuxcomp=ServiceTuxComp(threads="3"))
    assert planner.resource_env(explicit)[1] == "OMP_NUM_THREADS=3"
    plain = Service(name="app", image="app:latest")
    assert planner.resource_env(plain) == []


def test_start_command_includes_resource_env(tmp_path, monkeypatch):
    monkeypatch.setattr(planner, "_allowed_cpus", lambda: 2)
    project = _compose(tmp_path, "auto")
    service = project.get("app")
    cmd = planner.service_start_command(project, service, "proj-app")
    assert "-e" in cmd
    assert "OMP_NUM_THREADS=2" in cmd
    assert "UV_THREADPOOL_SIZE=2" in cmd


def test_image_entrypoint_run_command_includes_resource_env(tmp_path, monkeypatch):
    monkeypatch.setattr(planner, "_allowed_cpus", lambda: 2)
    project = _compose(tmp_path, "auto")
    plan = planner.build_plan(project)
    start = next(step for step in plan.steps if step.kind == "start")
    assert "OMP_NUM_THREADS=2" in start.command


def test_resource_warnings_explicit_over_allowed(allowed_two):
    project = Project(
        name="p",
        services={
            "app": Service(
                name="app",
                image="app:latest",
                tuxcomp=ServiceTuxComp(threads="8"),
            )
        },
    )
    warnings = planner.resource_warnings(project)
    assert len(warnings) == 1
    assert "requests 8 threads" in warnings[0]
    assert "2 CPUs" in warnings[0]


def test_resource_warnings_auto_is_silent(allowed_two):
    project = Project(
        name="p",
        services={
            "app": Service(
                name="app",
                image="app:latest",
                tuxcomp=ServiceTuxComp(threads="auto"),
            )
        },
    )
    assert planner.resource_warnings(project) == []
