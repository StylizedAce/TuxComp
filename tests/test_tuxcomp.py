import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from tuxcomp.model import Project, Service
from tuxcomp.parser import ComposeError, parse_compose_file, interpolate, parse_duration
from tuxcomp.planner import build_plan, down_plan, PlanError


def _proot() -> str:
    return os.environ.get("TUXCOMP_PROOT", "proot-distro")


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def simple():
    return parse_compose_file(FIXTURES / "simple.yml")


def test_parse_basic(simple: Project):
    assert set(simple.services) == {"redis", "app"}
    redis = simple.get("redis")
    assert redis.image == "redis:7-alpine"
    assert redis.container() == "socialvibes-redis"
    assert redis.volumes[0].is_named and redis.volumes[0].source == "redis-data"
    app = simple.get("app")
    assert app.ports[0].host == "8000"
    assert app.environment["REDIS_URL"] == "redis://localhost:6379"
    assert app.depends_on["redis"]["condition"] == "service_healthy"
    assert app.restart == "unless-stopped"


def test_env_interpolation():
    env = {"MYSQL_ROOT_PASSWORD": "secret"}
    assert interpolate("${MYSQL_ROOT_PASSWORD}", env) == "secret"
    assert interpolate("${MYSQL_ROOT_PASSWORD:-default}", env) == "secret"
    assert interpolate("${NOPE:-fallback}", env) == "fallback"
    assert interpolate("${NOPE-default}", env) == "default"
    assert interpolate("$NOPE", env) == ""
    assert interpolate("$$NOPE", env) == "$NOPE"
    assert interpolate("${NOPE}", env) == ""
    with pytest.raises(ComposeError):
        interpolate("${REQ:?must be set}", {})
    assert interpolate("${REQ:?must be set}", {"REQ": "value"}) == "value"


def test_dotenv_autoload(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("CLOUDFLARED_TOKEN=from-dotenv\nRELAY_PASSCODE=abc\n", encoding="utf-8")
    (tmp_path / "compose.yml").write_text(
        'services:\n  api:\n    image: app:latest\n    environment:\n'
        '      TOKEN: "${CLOUDFLARED_TOKEN:?missing}"\n'
        '      PASS: "${RELAY_PASSCODE}"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("CLOUDFLARED_TOKEN", raising=False)
    monkeypatch.delenv("RELAY_PASSCODE", raising=False)
    project = parse_compose_file(tmp_path / "compose.yml")
    assert project.services["api"].environment["TOKEN"] == "from-dotenv"
    assert project.services["api"].environment["PASS"] == "abc"


def test_duration():
    assert parse_duration("10s") == 10
    assert parse_duration("1m") == 60
    assert parse_duration("500ms") == 1
    assert parse_duration("2h") == 7200
    assert parse_duration(15) == 15
    assert parse_duration(None, 5) == 5


def test_healthcheck_duration_strings():
    project = parse_compose_file(FIXTURES / "simple.yml")
    redis = project.get("redis")
    assert redis.healthcheck is not None
    assert redis.healthcheck.interval == 10
    assert redis.healthcheck.start_period == 30
    app = project.get("app")
    assert app.healthcheck is None


def test_missing_image_or_build():
    with pytest.raises(ComposeError):
        parse_compose_file(FIXTURES / "no-image.yml")


def test_build_spec():
    project = parse_compose_file(FIXTURES / "build.yml")
    svc = project.get("app")
    assert svc.build is not None
    assert svc.build.context == "."
    assert svc.build.dockerfile == "Dockerfile"


def test_plan_order_dependencies(simple: Project):
    plan = build_plan(simple)
    first = plan.for_service("redis")
    second = plan.for_service("app")
    assert first[0].kind == "install"
    assert first[0].command[:3] == ["proot-distro", "install", "debian"]
    assert second[0].kind == "install"
    assert "-n" in second[0].command and "socialvibes-redis" not in second[0].command
    assert plan.steps.index(first[0]) < plan.steps.index(second[0])
    assert any(s.kind == "health" for s in plan.steps)
    assert any(s.kind == "volume" for s in plan.steps)
    volume_steps = [s for s in plan.steps if s.kind == "volume"]
    assert len(volume_steps) == 1


def test_plan_role_mapping(simple: Project):
    plan = build_plan(simple)
    redis_steps = plan.for_service("redis")
    kinds = [s.kind for s in redis_steps]
    assert "install" in kinds and "provision" in kinds
    provision = next(s for s in redis_steps if s.kind == "provision")
    assert "redis-server" in provision.command[-1]
    install = next(s for s in redis_steps if s.kind == "install")
    assert install.command[2] == "debian"


def test_plan_raw_image_escape_hatch():
    project = parse_compose_file(FIXTURES / "v2-role.yml")
    plan = build_plan(project)
    raw = plan.for_service("redis")
    assert raw[0].kind == "install"
    assert raw[0].command[:3] == ["proot-distro", "install", "redis:7-alpine"]
    mapped = plan.for_service("db")
    assert mapped[0].command[2] == "ubuntu"
    assert any(s.kind == "provision" for s in mapped)


def test_plan_hosts_naming(simple: Project):
    plan = build_plan(simple)
    hosts = [s for s in plan.steps if s.kind == "hosts"]
    assert len(hosts) == 2
    assert "# tuxcomp" in hosts[0].command[-1]
    assert "redis" in hosts[0].command[-1] and "app" in hosts[0].command[-1]


def test_plan_reuse_container():
    project = parse_compose_file(FIXTURES / "v2-reuse.yml")
    plan = build_plan(project)
    db_steps = plan.for_service("db")
    assert any(s.kind == "reuse" for s in db_steps)
    assert "socialvibes-db" in db_steps[0].note
    assert not any(s.kind == "install" for s in db_steps)
    assert not any(s.kind == "hosts" for s in db_steps)
    start = [s for s in db_steps if s.kind == "start"]
    assert start[0].command[2] == "socialvibes-db"
    down = down_plan(project)
    assert any(s.kind == "note" for s in down.for_service("db"))


def test_plan_golden_clone():
    project = parse_compose_file(FIXTURES / "v2-golden.yml")
    plan = build_plan(project)
    api_steps = plan.for_service("api")
    clone = [s for s in api_steps if s.kind == "clone"]
    assert len(clone) == 1
    assert clone[0].command[:2] == ["cp", "-a"]
    assert clone[0].command[2].endswith("wttg-golden")


def test_plan_cloudflared(monkeypatch):
    monkeypatch.setenv("CLOUDFLARED_TOKEN", "test-token-123")
    project = parse_compose_file(FIXTURES / "v2-tunnel.yml")
    plan = build_plan(project)
    tunnel = [s for s in plan.steps if s.kind == "tunnel"]
    # ensure container (install), install binary, write token, start daemon
    assert len(tunnel) == 3
    assert any("cloudflared" in s.command[-1] for s in tunnel)
    assert any("tunnel-token" in s.command[-1] for s in tunnel)
    assert any("cloudflared tunnel run" in s.command[-1] for s in tunnel)
    # The shared container has the fixed default name and a health step exists.
    assert any("tuxcomp-cloudflared" in s.title for s in plan.steps)
    assert any(s.kind == "health" and "cloudflared" in s.title for s in plan.steps)
    # Per-project container runs via proot (DNS works inside proot on Android)
    assert any("proot-distro" in s.command for s in tunnel)
    down = down_plan(project)
    # down does NOT stop the shared tunnel container - it serves other projects.
    assert not any(s.kind == "tunnel" for s in down.steps)


def test_plan_cloudflared_named_container(monkeypatch):
    """cloudflared: { container: X, token: ... } uses the named container."""
    import tempfile

    monkeypatch.setenv("CLOUDFLARED_TOKEN", "test-token-123")
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "compose.yml").write_text(
            'services:\n'
            '  app:\n'
            '    image: app:latest\n'
            'x-tuxcomp:\n'
            '  cloudflared:\n'
            '    container: my-tunnel\n'
            '    token: "${CLOUDFLARED_TOKEN}"\n',
            encoding="utf-8",
        )
        project = parse_compose_file(Path(d) / "compose.yml")
        plan = build_plan(project)
        assert any("my-tunnel" in s.title for s in plan.steps)
        assert not any("tuxcomp-cloudflared" in s.title for s in plan.steps)


def test_plan_build_service():
    project = parse_compose_file(FIXTURES / "build.yml")
    plan = build_plan(project)
    build_steps = [s for s in plan.steps if s.kind == "build"]
    assert len(build_steps) == 1
    assert build_steps[0].command[0] == "proot-distro"
    assert "--install-as" in build_steps[0].command


def test_plan_build_context_relative_to_compose_dir(tmp_path):
    (tmp_path / "compose.yml").write_text(
        'services:\n  app:\n    build:\n      context: ./src\n      dockerfile: Dockerfile\n',
        encoding="utf-8",
    )
    project = parse_compose_file(tmp_path / "compose.yml")
    plan = build_plan(project)
    build_step = next(s for s in plan.steps if s.kind == "build")
    ctx = build_step.command[2]
    assert Path(ctx).is_absolute(), f"context must be absolute, got {ctx!r}"
    assert Path(ctx).parent == tmp_path, ctx
    dockerfile = build_step.command[4]
    assert Path(dockerfile).is_absolute(), f"dockerfile must be absolute, got {dockerfile!r}"
    assert Path(dockerfile).parent == Path(ctx), dockerfile


def test_plan_profiles():
    project = parse_compose_file(FIXTURES / "profiles.yml")
    base = build_plan(project)
    assert "demo" not in {s.service for s in base.steps}
    with_profile = build_plan(project, profiles=["demo"])
    assert "demo" in {s.service for s in with_profile.steps}


def test_plan_no_detach():
    project = parse_compose_file(FIXTURES / "simple.yml")
    plan = build_plan(project, detach=False)
    start = [s for s in plan.steps if s.kind == "start"]
    assert all("-d" not in s.command for s in start)


def test_circular_dependency():
    project = parse_compose_file(FIXTURES / "circular.yml")
    with pytest.raises(PlanError):
        build_plan(project)


def test_down_plan():
    project = parse_compose_file(FIXTURES / "simple.yml")
    plan = down_plan(project)
    assert all(s.kind == "stop" for s in plan.steps)
    assert len(plan.steps) == 2
    plan = down_plan(project, remove=True)
    assert any(s.kind == "remove" for s in plan.steps)


def test_env_file():
    project = parse_compose_file(FIXTURES / "envfile.yml")
    svc = project.get("app")
    assert svc.environment.get("FROM_ENV_FILE") == "envvalue"


def test_container_name_fallback():
    project = parse_compose_file(FIXTURES / "simple.yml")
    plan = build_plan(project)
    for step in plan.for_service("app"):
        assert step.command and "app" in step.command


def test_plan_log_capture_command_service(tmp_path):
    (tmp_path / "compose.yml").write_text(
        'services:\n'
        '  api:\n'
        '    image: app:latest\n'
        '    command: ["uvicorn", "app.main:app", "--port", "4040"]\n',
        encoding="utf-8",
    )
    project = parse_compose_file(tmp_path / "compose.yml")
    plan = build_plan(project)
    start = next(s for s in plan.steps if s.kind == "start")
    assert start.command[0] == "proot-distro"
    assert start.command[1] == "login"
    shell = start.command[-1]
    assert "mkdir -p /var/log/tuxcomp" in shell
    assert "/var/log/tuxcomp/api.log" in shell
    assert "uvicorn" in shell and "--port" in shell
    assert "exec" in shell


def test_plan_log_capture_note_for_image_entrypoint(simple: Project):
    plan = build_plan(simple)
    start = next(s for s in plan.steps if s.kind == "start" and s.service == "app")
    assert "image entrypoint" in start.title
    assert "no command: defined" in start.note


def test_cli_log_path(tmp_path, monkeypatch):
    monkeypatch.setenv("TUXCOMP_PD_DIR", str(tmp_path))
    from tuxcomp.cli import _log_path
    assert _log_path("wttg-backend") == str(
        tmp_path / "wttg-backend" / "rootfs" / "var" / "log" / "tuxcomp" / "wttg-backend.log"
    )


def test_project_name_fallback(tmp_path, monkeypatch):
    (tmp_path / "docker-compose.yml").write_text(
        'services:\n  app:\n    image: app:latest\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    project = parse_compose_file("docker-compose.yml")
    assert project.name == tmp_path.name


def test_registry_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from tuxcomp.cli import _load_registry, _save_registry, _delete_registry

    entry = {
        "container": "wttg-backend",
        "project": "wttg",
        "created_at": "2026-08-16T06:00:00",
        "ports": ["4040"],
        "start": ["proot-distro", "login", "wttg-backend", "-d", "--", "/bin/sh", "-c", "x"],
        "health": None,
        "tunnel": True,
        "volume_dirs": [str(tmp_path / "vol")],
    }
    _save_registry(entry)
    assert _load_registry("wttg-backend") == entry
    _delete_registry("wttg-backend")
    assert _load_registry("wttg-backend") is None


def test_start_missing_registry(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    from tuxcomp.cli import _cmd_start, _parse_args

    args = _parse_args(["start", "ghost"])
    assert _cmd_start(args) == 1
    assert "no saved config" in capsys.readouterr().err


def test_rmi_container(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    import subprocess as sp

    from tuxcomp.cli import _cmd_rmi, _load_registry, _parse_args, _save_registry

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sp, "run", fake_run)
    vol = tmp_path / "vol"
    vol.mkdir()
    _save_registry(
        {
            "container": "wttg-backend",
            "project": "wttg",
            "ports": [],
            "start": [],
            "volume_dirs": [str(vol)],
        }
    )
    args = _parse_args(["rmi", "wttg-backend", "--force"])
    assert _cmd_rmi(args) == 0
    assert [c[0] for c in calls] == [_proot(), _proot()]
    assert not vol.exists()
    assert _load_registry("wttg-backend") is None
    out = capsys.readouterr().out
    assert "removed wttg-backend" in out


def test_down_container_direct(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    import subprocess as sp

    from tuxcomp.cli import _cmd_down, _parse_args

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sp, "run", fake_run)
    args = _parse_args(["down", "wttg-backend"])
    assert _cmd_down(args) == 0
    assert calls == [[_proot(), "kill", "wttg-backend"]]
    assert "stopped wttg-backend" in capsys.readouterr().out


def test_up_container_direct_replays_registry(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    import subprocess as sp

    from tuxcomp.cli import _cmd_up, _parse_args, _save_registry

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sp, "run", fake_run)
    _save_registry(
        {
            "container": "wttg-backend",
            "project": "wttg",
            "ports": ["4040"],
            "start": ["proot-distro", "login", "wttg-backend", "-d", "--", "/bin/sh", "-c", "x"],
            "health": None,
            "tunnel": False,
            "volume_dirs": [],
        }
    )
    args = _parse_args(["up", "wttg-backend"])
    assert _cmd_up(args) == 0
    assert calls == [["proot-distro", "login", "wttg-backend", "-d", "--", "/bin/sh", "-c", "x"]]
    assert "start wttg-backend" in capsys.readouterr().out


def test_runner_aborts_on_failure(monkeypatch, capsys):
    import subprocess as sp

    from tuxcomp.model import Project, Service
    from tuxcomp.planner import Plan, Step
    from tuxcomp.runner import Runner

    plan = Plan(
        project_name="x",
        steps=[
            Step(kind="install", service="a", title="install a", command=["proot-distro", "install", "x"]),
            Step(kind="install", service="b", title="install b", command=["proot-distro", "install", "y"]),
        ],
    )

    def fake_run(cmd, **kwargs):
        if cmd[-1] == "x":
            return sp.CompletedProcess(cmd, 1, stdout="boom", stderr="")
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sp, "run", fake_run)
    runner = Runner()
    results = runner.run(plan)
    assert len(results) == 1
    assert not results[0].ok
    assert "aborting" in capsys.readouterr().err


def test_runner_skips_reuse_step(monkeypatch, capsys):
    """A reuse step has an empty command - the runner must not crash on it."""
    import subprocess as sp

    from tuxcomp.planner import Plan, Step
    from tuxcomp.runner import Runner

    plan = Plan(
        project_name="x",
        steps=[
            Step(kind="reuse", service="db", title="reuse existing container socialvibes-db", command=[]),
            Step(kind="health", service="db", title="wait for db health", command=["true"]),
        ],
    )

    def fake_run(cmd, **kwargs):
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sp, "run", fake_run)
    runner = Runner()
    results = runner.run(plan)
    assert len(results) == 2
    assert results[0].ok and results[0].skipped
    assert results[1].ok
    out = capsys.readouterr().out
    assert "reused" in out


def test_runner_tolerates_missing_container_on_remove(monkeypatch, capsys):
    """rmi/down on containers that were never provisioned must not fail."""
    import subprocess as sp

    from tuxcomp.planner import Plan, Step
    from tuxcomp.runner import Runner

    plan = Plan(
        project_name="x",
        steps=[
            Step(
                kind="remove",
                service="demo",
                title="remove container socialvibes-demo",
                command=["proot-distro", "remove", "socialvibes-demo"],
            ),
            Step(
                kind="remove",
                service="api",
                title="remove container socialvibes-api",
                command=["proot-distro", "remove", "socialvibes-api"],
            ),
        ],
    )

    def fake_run(cmd, **kwargs):
        is_demo = any("socialvibes-demo" in c for c in cmd)
        out = "Error: container 'socialvibes-demo' is not installed." if is_demo else ""
        return sp.CompletedProcess(cmd, 1 if is_demo else 0, stdout="", stderr=out)

    monkeypatch.setattr(sp, "run", fake_run)
    runner = Runner()
    results = runner.run(plan)
    assert len(results) == 2
    assert all(r.ok for r in results)
    assert results[0].skipped  # already gone -> skipped, not failed
    out = capsys.readouterr().out
    assert "already gone" in out


def test_parse_deploy_config(tmp_path):
    (tmp_path / "compose.yml").write_text(
        'services:\n'
        '  app:\n'
        '    image: app:latest\n'
        'x-tuxcomp:\n'
        '  deploy:\n'
        '    host: root@192.168.1.153\n'
        '    port: 8022\n'
        '    remote_dir: ~/upload-tool\n'
        '    build: "npm run build -- --configuration=docker"\n'
        '    sync:\n'
        '      - dist/upload-tool/browser\n'
        '      - nginx.conf\n',
        encoding="utf-8",
    )
    project = parse_compose_file(tmp_path / "compose.yml")
    deploy = project.tuxcomp.deploy
    assert deploy.host == "root@192.168.1.153"
    assert deploy.port == 8022
    assert deploy.remote_dir == "~/upload-tool"
    assert deploy.build == "npm run build -- --configuration=docker"
    assert deploy.sync == ["dist/upload-tool/browser", "nginx.conf"]


def test_prebuilt_guard_missing_source(tmp_path, monkeypatch, capsys):
    from tuxcomp.cli import _prebuilt_guard
    from tuxcomp.parser import parse_compose_file

    (tmp_path / "compose.yml").write_text(
        'services:\n'
        '  web:\n'
        '    image: nginx\n'
        '    volumes:\n'
        '      - ./dist/upload-tool/browser:/usr/share/nginx/html:ro\n',
        encoding="utf-8",
    )
    project = parse_compose_file(tmp_path / "compose.yml")
    assert _prebuilt_guard(project) == 1
    err = capsys.readouterr().err
    assert "not found" in err and "deploy" in err


def test_prebuilt_guard_passes_when_source_exists(tmp_path, monkeypatch, capsys):
    from tuxcomp.cli import _prebuilt_guard
    from tuxcomp.parser import parse_compose_file

    (tmp_path / "dist").mkdir()
    (tmp_path / "compose.yml").write_text(
        'services:\n'
        '  web:\n'
        '    image: nginx\n'
        '    volumes:\n'
        '      - ./dist:/usr/share/nginx/html:ro\n',
        encoding="utf-8",
    )
    project = parse_compose_file(tmp_path / "compose.yml")
    assert _prebuilt_guard(project) == 0
    assert "not found" not in capsys.readouterr().err


def test_rebuild_requires_registry(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    from tuxcomp.cli import _cmd_rebuild, _parse_args

    args = _parse_args(["rebuild", "ghost"])
    assert _cmd_rebuild(args) == 1
    assert "no saved config" in capsys.readouterr().err


def test_rebuild_refuses_reuse(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    from tuxcomp.cli import _cmd_rebuild, _parse_args, _save_registry

    _save_registry(
        {
            "container": "socialvibes-db",
            "project": "fastapi",
            "reuse": "socialvibes-db",
            "compose_file": str(tmp_path / "compose.yml"),
            "ports": [],
            "start": [],
        }
    )
    args = _parse_args(["rebuild", "socialvibes-db"])
    assert _cmd_rebuild(args) == 1
    assert "reuses a shared container" in capsys.readouterr().err


def test_rebuild_removes_and_ups(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    import subprocess as sp

    from tuxcomp.cli import _cmd_rebuild, _parse_args, _save_registry

    compose = tmp_path / "compose.yml"
    compose.write_text('services:\n  app:\n    image: app:latest\n', encoding="utf-8")
    run_calls: list[list[str]] = []
    call_calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        run_calls.append(cmd)
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    def fake_call(cmd, **kwargs):
        call_calls.append(cmd)
        return 0

    monkeypatch.setattr(sp, "run", fake_run)
    monkeypatch.setattr(sp, "call", fake_call)
    _save_registry(
        {
            "container": "wttg-backend",
            "project": "wttg",
            "compose_file": str(compose),
            "created_at": "2026-08-16T06:00:00",
            "ports": ["4040"],
            "start": ["proot-distro", "login", "wttg-backend", "-d", "--", "/bin/sh", "-c", "x"],
        }
    )
    args = _parse_args(["rebuild", "wttg-backend"])
    assert _cmd_rebuild(args) == 0
    # kill + remove
    assert run_calls[0][1] == "kill" and run_calls[0][2] == "wttg-backend"
    assert run_calls[1][1] == "remove" and run_calls[1][2] == "wttg-backend"
    # then re-runs up via subprocess.call
    assert "up" in call_calls[0] and "-f" in call_calls[0] and str(compose) in call_calls[0]


def test_deploy_no_config(tmp_path, monkeypatch, capsys):
    from tuxcomp.cli import _cmd_deploy, _parse_args

    (tmp_path / "compose.yml").write_text(
        'services:\n  app:\n    image: app:latest\n', encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    args = _parse_args(["deploy", "-f", "compose.yml"])
    assert _cmd_deploy(args) == 1
    assert "no x-tuxcomp.deploy" in capsys.readouterr().err


def test_deploy_sequence(tmp_path, monkeypatch, capsys):
    import subprocess as sp

    from tuxcomp import __version__
    from tuxcomp.cli import _cmd_deploy, _parse_args

    (tmp_path / "compose.yml").write_text(
        'services:\n'
        '  app:\n'
        '    image: app:latest\n'
        'x-tuxcomp:\n'
        '  deploy:\n'
        '    host: root@192.168.1.153\n'
        '    port: 8022\n'
        '    remote_dir: ~/upload-tool\n'
        '    sync:\n'
        '      - dist/browser\n',
        encoding="utf-8",
    )
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "browser").mkdir()

    calls: list[list[str]] = []

    def fake_call(cmd, **kwargs):
        calls.append(cmd)
        return 0

    def fake_ssh_out(host, port, cmd, timeout=30):
        # Simulate an already-up-to-date target so no upgrade SSH call happens.
        return f"tuxcomp {__version__}"

    monkeypatch.setattr(sp, "call", fake_call)
    monkeypatch.setattr("tuxcomp.cli._ssh_out", fake_ssh_out)
    monkeypatch.chdir(tmp_path)
    args = _parse_args(["deploy", "-f", "compose.yml"])
    assert _cmd_deploy(args) == 0
    # ssh mkdir, scp compose, ssh mkdir parent, scp sync, ssh up
    assert calls[0][0] == "ssh" and "mkdir -p" in calls[0][-1]
    assert calls[1][0] == "scp" and any("compose.yml" in c for c in calls[1])
    assert calls[2][0] == "ssh" and "mkdir -p" in calls[2][-1]
    assert calls[3][0] == "scp" and "-r" in calls[3]
    assert calls[4][0] == "ssh" and "tuxcomp up" in calls[4][-1]


def test_deploy_upgrades_outdated_tuxcomp(tmp_path, monkeypatch, capsys):
    """Deploy should pip-install the latest tuxcomp when the target is older."""
    import subprocess as sp

    from tuxcomp.cli import _cmd_deploy, _parse_args

    (tmp_path / "compose.yml").write_text(
        'services:\n'
        '  app:\n'
        '    image: app:latest\n'
        'x-tuxcomp:\n'
        '  deploy:\n'
        '    host: root@192.168.1.153\n'
        '    port: 8022\n'
        '    remote_dir: ~/upload-tool\n'
        '    sync:\n'
        '      - dist/browser\n',
        encoding="utf-8",
    )
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "browser").mkdir()

    calls: list[list[str]] = []

    def fake_call(cmd, **kwargs):
        calls.append(cmd)
        return 0

    def fake_ssh_out(host, port, cmd, timeout=30):
        # Simulate an outdated target: version is old, so upgrade runs.
        return "tuxcomp 0.1.0"

    monkeypatch.setattr(sp, "call", fake_call)
    monkeypatch.setattr("tuxcomp.cli._ssh_out", fake_ssh_out)
    monkeypatch.chdir(tmp_path)
    args = _parse_args(["deploy", "-f", "compose.yml"])
    assert _cmd_deploy(args) == 0
    # First SSH call is the pip upgrade, not mkdir.
    assert calls[0][0] == "ssh" and "pip install --upgrade" in calls[0][-1]
    assert calls[1][0] == "ssh" and "mkdir -p" in calls[1][-1]


def test_deploy_syncs_env_file(tmp_path, monkeypatch, capsys):
    """`.env` listed in sync: is pushed to <remote_dir>/.env."""
    import subprocess as sp

    from tuxcomp import __version__
    from tuxcomp.cli import _cmd_deploy, _parse_args

    (tmp_path / "compose.yml").write_text(
        'services:\n'
        '  app:\n'
        '    image: app:latest\n'
        'x-tuxcomp:\n'
        '  deploy:\n'
        '    host: root@192.168.1.153\n'
        '    port: 8022\n'
        '    remote_dir: ~/upload-tool\n'
        '    sync:\n'
        '      - .env\n'
        '      - dist/browser\n',
        encoding="utf-8",
    )
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "browser").mkdir()
    (tmp_path / ".env").write_text("SECRET=123\n", encoding="utf-8")

    calls: list[list[str]] = []

    def fake_call(cmd, **kwargs):
        calls.append(cmd)
        return 0

    def fake_ssh_out(host, port, cmd, timeout=30):
        return f"tuxcomp {__version__}"

    monkeypatch.setattr(sp, "call", fake_call)
    monkeypatch.setattr("tuxcomp.cli._ssh_out", fake_ssh_out)
    monkeypatch.chdir(tmp_path)
    args = _parse_args(["deploy", "-f", "compose.yml"])
    assert _cmd_deploy(args) == 0
    # One of the calls must be scp of .env -> remote/.env
    assert any(
        c[0] == "scp" and c[-1].endswith("/.env")
        for c in calls
    ), f"no .env sync found in calls: {calls}"


def test_remote_add_list_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    from tuxcomp.cli import _cmd_remote, _parse_args

    args = _parse_args(["remote", "add", "phone", "--host", "root@192.168.1.153", "--port", "8022", "--default"])
    assert _cmd_remote(args) == 0
    args = _parse_args(["remote", "add", "other", "--host", "root@10.0.0.5"])
    assert _cmd_remote(args) == 0

    args = _parse_args(["remote", "list"])
    assert _cmd_remote(args) == 0
    out = capsys.readouterr().out
    assert "* phone" in out and "root@192.168.1.153:8022" in out
    assert "other" in out


def test_remote_remove_and_set_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    from tuxcomp.cli import _cmd_remote, _parse_args

    _cmd_remote(_parse_args(["remote", "add", "phone", "--host", "root@1.1.1.1", "--default"]))
    _cmd_remote(_parse_args(["remote", "add", "other", "--host", "root@2.2.2.2"]))

    args = _parse_args(["remote", "set-default", "other"])
    assert _cmd_remote(args) == 0
    args = _parse_args(["remote", "remove", "phone"])
    assert _cmd_remote(args) == 0
    args = _parse_args(["remote", "remove", "ghost"])
    assert _cmd_remote(args) == 1
    assert "no remote 'ghost'" in capsys.readouterr().err


def test_down_multiple_containers(tmp_path, monkeypatch, capsys):
    """tuxcomp down a b c stops each registered container."""
    monkeypatch.setenv("HOME", str(tmp_path))
    import subprocess as sp

    from tuxcomp.cli import _cmd_down, _parse_args

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return type("P", (), {"returncode": 0})()

    monkeypatch.setattr(sp, "run", fake_run)
    registry = Path(tmp_path) / ".tuxcomp" / "registry"
    registry.mkdir(parents=True)
    (registry / "lt-api.json").write_text('{"container": "lt-api"}', encoding="utf-8")
    (registry / "lt-redis.json").write_text('{"container": "lt-redis"}', encoding="utf-8")

    args = _parse_args(["down", "lt-api", "lt-redis"])
    assert _cmd_down(args) == 0
    kills = [c for c in calls if c[0] == "proot-distro" and c[1] == "kill"]
    assert len(kills) == 2


def test_down_all(tmp_path, monkeypatch, capsys):
    """tuxcomp down --all stops every registered container."""
    monkeypatch.setenv("HOME", str(tmp_path))
    import subprocess as sp

    from tuxcomp.cli import _cmd_down, _parse_args

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return type("P", (), {"returncode": 0})()

    monkeypatch.setattr(sp, "run", fake_run)
    registry = Path(tmp_path) / ".tuxcomp" / "registry"
    registry.mkdir(parents=True)
    for name in ("lt-api", "lt-redis", "tuxcomp-cloudflared"):
        (registry / f"{name}.json").write_text(f'{{"container": "{name}"}}', encoding="utf-8")

    args = _parse_args(["down", "--all"])
    assert _cmd_down(args) == 0
    kills = [c for c in calls if c[0] == "proot-distro" and c[1] == "kill"]
    assert len(kills) == 3


def test_completion_output(tmp_path, monkeypatch, capsys):
    """tuxcomp completion prints a script containing subcommands."""
    from tuxcomp.cli import _cmd_completion, _parse_args

    args = _parse_args(["completion"])
    assert _cmd_completion(args) == 0
    out = capsys.readouterr().out
    assert "tuxcomp" in out
    assert "complete" in out


def test_deploy_uses_default_remote(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    import subprocess as sp

    from tuxcomp.cli import _cmd_deploy, _cmd_remote, _parse_args

    _cmd_remote(_parse_args(["remote", "add", "phone", "--host", "root@192.168.1.153", "--port", "8022", "--default"]))
    (tmp_path / "compose.yml").write_text(
        'services:\n'
        '  app:\n'
        '    image: app:latest\n'
        'x-tuxcomp:\n'
        '  deploy:\n'
        '    sync:\n'
        '      - dist/browser\n',
        encoding="utf-8",
    )
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "browser").mkdir()

    calls: list[list[str]] = []

    def fake_call(cmd, **kwargs):
        calls.append(cmd)
        return 0

    monkeypatch.setattr(sp, "call", fake_call)
    monkeypatch.chdir(tmp_path)
    args = _parse_args(["deploy", "-f", "compose.yml"])
    assert _cmd_deploy(args) == 0
    out = capsys.readouterr().out
    assert "root@192.168.1.153:8022" in out
    assert any("8022" in c and "root@192.168.1.153" in c for c in calls)


def test_deploy_remote_flag_overrides(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    import subprocess as sp

    from tuxcomp.cli import _cmd_deploy, _cmd_remote, _parse_args

    _cmd_remote(_parse_args(["remote", "add", "a", "--host", "root@10.0.0.1", "--default"]))
    _cmd_remote(_parse_args(["remote", "add", "b", "--host", "root@10.0.0.2"]))
    (tmp_path / "compose.yml").write_text(
        'services:\n'
        '  app:\n'
        '    image: app:latest\n'
        'x-tuxcomp:\n'
        '  deploy:\n'
        '    sync:\n'
        '      - dist/browser\n',
        encoding="utf-8",
    )
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "browser").mkdir()
    calls: list[list[str]] = []

    def fake_call(cmd, **kwargs):
        calls.append(cmd)
        return 0

    monkeypatch.setattr(sp, "call", fake_call)
    monkeypatch.chdir(tmp_path)
    args = _parse_args(["deploy", "-f", "compose.yml", "--remote", "b"])
    assert _cmd_deploy(args) == 0
    assert any(any("10.0.0.2" in part for part in c) for c in calls)


def test_dockerfile_fix_workdir_copy(tmp_path):
    from tuxcomp.dockerfile_fix import preprocess_dockerfile

    text = (
        "FROM python:3.13-slim\n"
        "WORKDIR /app\n"
        "COPY requirements.txt .\n"
        "RUN pip install -r requirements.txt\n"
        "COPY . .\n"
        "CMD [\"gunicorn\", \"--bind\", \"0.0.0.0:8000\", \"main:app\"]\n"
    )
    new, notes = preprocess_dockerfile(text)
    assert "WORKDIR" not in new
    assert "COPY requirements.txt /app/requirements.txt" in new
    assert "RUN mkdir -p /app && cd /app && pip install" in new
    assert "COPY . /app" in new
    assert '"mkdir -p /app && cd /app && exec \\"$0\\" \\"$@\\""' in new
    assert len(notes) >= 4


def test_dockerfile_fix_no_workdir_unchanged(tmp_path):
    from tuxcomp.dockerfile_fix import preprocess_dockerfile

    text = "FROM nginx\nCOPY nginx.conf /etc/nginx/conf.d/default.conf\nCMD [\"nginx\"]\n"
    new, notes = preprocess_dockerfile(text)
    assert new.strip() == text.strip()
    assert notes == []


def test_dockerfile_fix_workdir_relative_and_multiline(tmp_path):
    from tuxcomp.dockerfile_fix import preprocess_dockerfile

    text = (
        "FROM debian\n"
        "WORKDIR /app\n"
        "WORKDIR sub\n"
        "RUN apt-get update && \\\n"
        "    apt-get install -y curl\n"
        "ADD . .\n"
        "USER node\n"
        "ENTRYPOINT [\"/entry.sh\"]\n"
    )
    new, notes = preprocess_dockerfile(text)
    assert "WORKDIR" not in new
    assert "ADD . /app/sub" in new
    assert "RUN mkdir -p /app/sub && cd /app/sub && apt-get update" in new
    assert "USER node" not in new
    assert any("USER" in n for n in notes)
    assert "ENTRYPOINT" in new and '"/entry.sh"' in new


def test_dockerfile_fix_single_file_into_existing_dir(tmp_path):
    from tuxcomp.dockerfile_fix import preprocess_dockerfile

    # RUN creates the workdir; a later single-file COPY must target the file
    # path explicitly (proot "renames" the file to the dest NAME otherwise)
    text = (
        "FROM python:3.13-slim\n"
        "WORKDIR /app\n"
        "RUN pip install --upgrade pip\n"
        "COPY requirements.txt .\n"
        "COPY config.json subdir/\n"
    )
    new, notes = preprocess_dockerfile(text)
    assert "COPY requirements.txt /app/requirements.txt" in new
    assert "COPY config.json /app/subdir/config.json" in new


def test_dockerfile_fix_multi_stage_copy_from(tmp_path):
    from tuxcomp.dockerfile_fix import preprocess_dockerfile

    text = (
        "FROM node:22-alpine AS build\n"
        "WORKDIR /app\n"
        "COPY package.json .\n"
        "RUN npm ci\n"
        "FROM nginx:alpine\n"
        "COPY --from=build /app/dist /usr/share/nginx/html\n"
    )
    new, notes = preprocess_dockerfile(text)
    # second stage has no WORKDIR -> COPY --from= stays untouched
    assert "COPY --from=build /app/dist /usr/share/nginx/html" in new
    assert "COPY package.json /app/package.json" in new


def test_dockerfile_fix_merges_consecutive_runs(tmp_path):
    """Fewer RUN steps = fewer proot rootfs snapshots = much faster builds."""
    from tuxcomp.dockerfile_fix import preprocess_dockerfile

    text = (
        "FROM python:3.13-slim\n"
        "WORKDIR /app\n"
        "RUN apt-get update && apt-get install -y gcc\n"
        "RUN pip install -r requirements.txt\n"
        "RUN pip install gunicorn\n"
        "COPY . .\n"
        "RUN echo done\n"
    )
    new, notes = preprocess_dockerfile(text)
    run_blocks = [b for b in new.splitlines() if b.startswith("RUN ")]
    # the three leading RUNs fuse into one; the post-COPY RUN stays separate
    assert len(run_blocks) == 2
    assert "apt-get update && apt-get install -y gcc && pip install -r requirements.txt && pip install gunicorn" in run_blocks[0]
    assert "echo done" in run_blocks[1]
    # each merged block gets exactly one mkdir+cd prefix
    assert run_blocks[0].count("mkdir -p /app && cd /app") == 1


def test_dockerfile_fix_does_not_merge_exec_form(tmp_path):
    from tuxcomp.dockerfile_fix import preprocess_dockerfile

    text = (
        "FROM python:3.13-slim\n"
        "WORKDIR /app\n"
        'RUN ["python", "setup.py", "build"]\n'
        'RUN ["python", "setup.py", "install"]\n'
    )
    new, notes = preprocess_dockerfile(text)
    # exec-form RUNs cannot be joined with && - both stay separate
    assert new.count("RUN /bin/sh -c") == 2


def test_dockerfile_fix_multiline_apt_continuation(tmp_path):
    """Merged multiline RUNs must not leak a trailing backslash (\\ gcc bug)."""
    from tuxcomp.dockerfile_fix import preprocess_dockerfile

    text = (
        "FROM python:3.13-slim\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
        "    gcc \\\n"
        "    libmariadb-dev \\\n"
        "    pkg-config \\\n"
        "    && rm -rf /var/lib/apt/lists/*\n"
        "RUN pip install -r requirements.txt\n"
    )
    new, notes = preprocess_dockerfile(text)
    assert "\\ gcc" not in new
    assert "\\\n    libmariadb" not in new
    assert "gcc" in new and "libmariadb-dev" in new and "pkg-config" in new
    # merged into one RUN (apt + pip), no stray backslashes
    assert new.count("RUN ") == 1
    assert "&& pip install" in new


def test_port_conflict_same_project_duplicate(tmp_path, monkeypatch, capsys):
    from tuxcomp.cli import _check_port_conflicts
    from tuxcomp.parser import parse_compose_file

    (tmp_path / "compose.yml").write_text(
        'services:\n'
        '  a:\n'
        '    image: app:latest\n'
        '    ports:\n'
        '      - "8000:8000"\n'
        '  b:\n'
        '    image: app:latest\n'
        '    ports:\n'
        '      - "8000:8000"\n',
        encoding="utf-8",
    )
    project = parse_compose_file(tmp_path / "compose.yml")
    assert _check_port_conflicts(project) == 1
    assert "both 'a' and 'b'" in capsys.readouterr().err


def test_port_conflict_with_running_container(tmp_path, monkeypatch, capsys):
    import subprocess as sp

    from tuxcomp.cli import _check_port_conflicts, _load_registry, _save_registry
    from tuxcomp.parser import parse_compose_file

    monkeypatch.setenv("HOME", str(tmp_path))
    _save_registry(
        {
            "container": "other-backend",
            "project": "other",
            "ports": ["8000"],
            "start": [],
        }
    )
    monkeypatch.setattr(
        "tuxcomp.cli._running_containers",
        lambda: {"other-backend"},
    )
    (tmp_path / "compose.yml").write_text(
        'services:\n'
        '  a:\n'
        '    image: app:latest\n'
        '    ports:\n'
        '      - "8000:8000"\n',
        encoding="utf-8",
    )
    project = parse_compose_file(tmp_path / "compose.yml")
    assert _check_port_conflicts(project) == 1
    assert "owned by running container 'other-backend'" in capsys.readouterr().err


def test_port_conflict_from_dockerfile(tmp_path, monkeypatch, capsys):
    from tuxcomp.cli import _check_port_conflicts, _save_registry
    from tuxcomp.parser import parse_compose_file

    monkeypatch.setenv("HOME", str(tmp_path))
    _save_registry(
        {
            "container": "sv-backend",
            "project": "other",
            "ports": ["8000"],
            "start": [],
        }
    )
    monkeypatch.setattr(
        "tuxcomp.cli._running_containers",
        lambda: {"sv-backend"},
    )
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.13-slim\nEXPOSE 8000\nCMD [\"uvicorn\", \"main:app\", \"--port\", \"8000\"]\n",
        encoding="utf-8",
    )
    (tmp_path / "compose.yml").write_text(
        'services:\n'
        '  a:\n'
        '    build:\n'
        '      context: .\n'
        '      dockerfile: Dockerfile\n'
        '    ports:\n'
        '      - "8080:8080"\n',
        encoding="utf-8",
    )
    project = parse_compose_file(tmp_path / "compose.yml")
    assert _check_port_conflicts(project) == 1
    assert "owned by running container 'sv-backend'" in capsys.readouterr().err


def test_port_conflict_force_passes(tmp_path, monkeypatch, capsys):
    from tuxcomp.cli import _check_port_conflicts, _save_registry
    from tuxcomp.parser import parse_compose_file

    monkeypatch.setenv("HOME", str(tmp_path))
    _save_registry(
        {"container": "other-backend", "project": "other", "ports": ["8000"], "start": []}
    )
    monkeypatch.setattr("tuxcomp.cli._running_containers", lambda: {"other-backend"})
    (tmp_path / "compose.yml").write_text(
        'services:\n  a:\n    image: app:latest\n    ports:\n      - "8000:8000"\n',
        encoding="utf-8",
    )
    project = parse_compose_file(tmp_path / "compose.yml")
    assert _check_port_conflicts(project, force=True) == 0


def test_parse_pip_wheels_config(tmp_path):
    (tmp_path / "compose.yml").write_text(
        'services:\n'
        '  app:\n'
        '    image: app:latest\n'
        'x-tuxcomp:\n'
        '  deploy:\n'
        '    host: root@192.168.1.153\n'
        '    pip_wheels: true\n'
        '    requirements: requirements-dev.txt\n',
        encoding="utf-8",
    )
    project = parse_compose_file(tmp_path / "compose.yml")
    deploy = project.tuxcomp.deploy
    assert deploy.pip_wheels is True
    assert deploy.requirements == "requirements-dev.txt"


def test_prefetch_wheels_downloads_and_syncs(tmp_path, monkeypatch, capsys):
    import subprocess as sp

    from tuxcomp.cli import _prefetch_wheels
    from tuxcomp.parser import parse_compose_file

    (tmp_path / "requirements.txt").write_text("fastapi\nuvicorn\n", encoding="utf-8")
    (tmp_path / "compose.yml").write_text(
        'services:\n  app:\n    image: app:latest\n'
        'x-tuxcomp:\n  deploy:\n    host: root@x\n',
        encoding="utf-8",
    )
    project = parse_compose_file(tmp_path / "compose.yml")
    deploy = project.tuxcomp.deploy

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sp, "run", fake_run)
    assert _prefetch_wheels(project, deploy) == 0
    assert calls[0][0].endswith("python") or "python" in calls[0][0]
    assert "download" in calls[0]
    assert "--platform" in calls[0] and "manylinux2014_aarch64" in calls[0]
    assert "--only-binary=:all:" in calls[0]
    assert calls[0][-2] == "-r" and calls[0][-1].endswith("requirements.txt")
    assert "wheels" in deploy.sync


def test_prefetch_wheels_missing_requirements(tmp_path, monkeypatch, capsys):
    import subprocess as sp

    from tuxcomp.cli import _prefetch_wheels
    from tuxcomp.parser import parse_compose_file

    (tmp_path / "compose.yml").write_text(
        'services:\n  app:\n    image: app:latest\n'
        'x-tuxcomp:\n  deploy:\n    host: root@x\n    pip_wheels: true\n',
        encoding="utf-8",
    )
    project = parse_compose_file(tmp_path / "compose.yml")
    deploy = project.tuxcomp.deploy

    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess([], 0))
    assert _prefetch_wheels(project, deploy) == 0
    assert "not found - skipping" in capsys.readouterr().err


def test_dockerfile_fix_wheels_find_links(tmp_path):
    from tuxcomp.dockerfile_fix import fix_dockerfile

    (tmp_path / "wheels").mkdir()
    (tmp_path / "wheels" / "fastapi.whl").write_bytes(b"x")
    df = tmp_path / "Dockerfile"
    df.write_text(
        "FROM python:3.13-slim\n"
        "WORKDIR /app\n"
        "COPY requirements.txt .\n"
        "RUN pip install --no-cache-dir -r requirements.txt\n"
        "COPY . .\n",
        encoding="utf-8",
    )
    out = tmp_path / ".tuxcomp-Dockerfile"
    changed, notes = fix_dockerfile(df, out)
    assert changed
    text = out.read_text(encoding="utf-8")
    assert "--find-links /app/wheels" in text
    # full-context COPY exists -> no extra COPY of wheels needed
    assert "COPY wheels" not in text


def test_dockerfile_fix_wheels_adds_copy(tmp_path):
    from tuxcomp.dockerfile_fix import fix_dockerfile

    (tmp_path / "wheels").mkdir()
    (tmp_path / "wheels" / "fastapi.whl").write_bytes(b"x")
    df = tmp_path / "Dockerfile"
    # no full-context COPY -> wheels must be copied explicitly
    df.write_text(
        "FROM python:3.13-slim\n"
        "WORKDIR /app\n"
        "COPY requirements.txt .\n"
        "RUN pip install --no-cache-dir -r requirements.txt\n"
        "COPY main.py .\n",
        encoding="utf-8",
    )
    out = tmp_path / ".tuxcomp-Dockerfile"
    changed, notes = fix_dockerfile(df, out)
    assert changed
    text = out.read_text(encoding="utf-8")
    assert "--find-links /app/wheels" in text
    assert "COPY wheels /app/wheels" in text
    assert text.index("COPY wheels /app/wheels") < text.index("pip install")


def test_dockerfile_fix_no_wheels_no_change(tmp_path):
    from tuxcomp.dockerfile_fix import fix_dockerfile

    df = tmp_path / "Dockerfile"
    df.write_text(
        "FROM python:3.13-slim\n"
        "WORKDIR /app\n"
        "COPY requirements.txt .\n"
        "RUN pip install --no-cache-dir -r requirements.txt\n"
        "COPY . .\n",
        encoding="utf-8",
    )
    out = tmp_path / ".tuxcomp-Dockerfile"
    changed, notes = fix_dockerfile(df, out)
    assert changed  # WORKDIR/COPY rewrites still happen
    text = out.read_text(encoding="utf-8")
    assert "--find-links" not in text
def test_rmi_compose_skips_uninstalled(tmp_path, monkeypatch, capsys):
    import subprocess as sp

    from tuxcomp.cli import (
        _cmd_rmi,
        _load_registry,
        _parse_args,
        _save_registry,
    )

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "tuxcomp.cli._installed_containers",
        lambda: ["socialvibes-api", "socialvibes-db"],
    )
    (tmp_path / "compose.yml").write_text(
        'services:\n'
        '  db:\n'
        '    image: mysql:8.0\n'
        '    container_name: socialvibes-db\n'
        '  api:\n'
        '    image: app:latest\n'
        '    container_name: socialvibes-api\n'
        '  demo:\n'
        '    profiles: ["demo"]\n'
        '    image: app:latest\n'
        '    container_name: socialvibes-demo\n',
        encoding="utf-8",
    )
    _save_registry(
        {"container": "socialvibes-api", "project": "x", "ports": ["8000"], "start": []}
    )

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return sp.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sp, "run", fake_run)
    monkeypatch.chdir(tmp_path)
    args = _parse_args(["rmi", "-f", "compose.yml", "--force"])
    assert _cmd_rmi(args) == 0
    out = capsys.readouterr().out
    # demo was never installed -> its destructive remove is skipped (kill may still run, harmlessly)
    assert "socialvibes-demo (skip: not installed)" in out
    assert not any(c[1] == "remove" and "socialvibes-demo" in str(c) for c in calls)
    assert _load_registry("socialvibes-api") is None
    assert "removed project services" in out
