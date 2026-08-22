# TuxComp

Docker Compose orchestration for Termux servers — parses `docker-compose.yml` and runs the
equivalent services on phones/Pis that can't run a native Docker engine, via
[proot-distro](https://github.com/termux/proot-distro).

## Install

```bash
pip install git+https://github.com/StylizedAce/TuxComp.git
```

## Quick start

Write a compose file with an `x-tuxcomp.deploy` block:

```yaml
services:
  redis:
    image: redis:7-alpine
    container_name: app-redis
    restart: unless-stopped

x-tuxcomp:
  deploy:
    remote_dir: ~/my-app
    sync:
      - app/
    pip_wheels: true
    requirements: requirements.txt
```

Deploy to your phone:

```bash
tuxcomp remote add phone --host root@192.168.1.153 --port 8022 --default
tuxcomp deploy -f docker-compose.yml
```

`tuxcomp deploy` upgrades tuxcomp on the target, pushes your files, and starts the stack.

## Commands

| Command | Description |
|---|---|
| `tuxcomp up -f compose.yml` | Provision + start services |
| `tuxcomp down -f compose.yml` | Stop services (keeps state) |
| `tuxcomp start <container>` | Restart a container from its saved config |
| `tuxcomp stop <container>` | Stop a container |
| `tuxcomp ps -f compose.yml` | List running services |
| `tuxcomp logs <service>` | Tail container logs |
| `tuxcomp exec <container> <cmd>` | Run a command inside a container |
| `tuxcomp rebuild <container>` | Stop + remove + re-up with new code |
| `tuxcomp deploy -f compose.yml` | Upgrade tuxcomp on target, push files, start stack |
| `tuxcomp plan -f compose.yml` | Print what would run, without running it |

## Cloudflared

Expose a container to the internet via a Cloudflare tunnel. Two modes:

**Host mode (recommended)** — reuses the device's globally-installed cloudflared
(registered once via `cloudflared tunnel login`) and its `~/.cloudflared` config.
One instance serves every exposed service; no token in the compose file:

```yaml
x-tuxcomp:
  cloudflared: {}
```

Optionally name the tunnel (from `cloudflared tunnel list`):

```yaml
x-tuxcomp:
  cloudflared:
    tunnel: my-tunnel-name
```

**Token mode** — installs cloudflared inside a container and starts a named
token tunnel (useful for fresh phones without a host setup):

```yaml
x-tuxcomp:
  cloudflared:
    token: "${CLOUDFLARED_TOKEN}"
    tunnel: my-app.example.com
```

In both modes the tunnel lifecycle follows the container: `up`/`start`/`deploy`
bring cloudflared up, `down` stops it.

## Env sync

Deploy can push a local `.env` to the target so compose's `${VAR:?...}` references
resolve with secrets that never enter git:

```yaml
x-tuxcomp:
  deploy:
    remote_dir: ~/my-app
    env_sync: .env        # pushed to ~/my-app/.env on every deploy
    sync:
      - app/
```

Keep the local `.env` gitignored — only the compose's variable references are
committed.

## Development

```bash
pip install -e .[dev]
python -m pytest
```