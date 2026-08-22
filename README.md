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
One **shared** instance serves every exposed service on the device (one tunnel
connection, many public hostnames → localhost ports). No token in the compose:

```yaml
x-tuxcomp:
  cloudflared: {}
```

Host mode is idempotent: if cloudflared is already running it is left alone
(never killed — other projects may use the same instance), and `down` does NOT
stop it. Manually stop with `tuxcomp stop cloudflared` or `pkill cloudflared`.

Optionally name the tunnel (from `cloudflared tunnel list`):

```yaml
x-tuxcomp:
  cloudflared:
    tunnel: my-tunnel-name
```

**Token mode** — installs cloudflared inside a container and starts a token
tunnel owned by that project (useful for a fresh phone with no host setup, or
a dedicated tunnel). This one DOES stop on `down`:

```yaml
x-tuxcomp:
  cloudflared:
    token: "${CLOUDFLARED_TOKEN}"
    tunnel: my-app.example.com
```

## Env sync

Add `.env` to the deploy `sync:` list to push local secrets to the target.
The compose resolves `${VAR:?...}` from the `.env` next to it, and the local
file is gitignored:

```yaml
x-tuxcomp:
  deploy:
    remote_dir: ~/my-app
    sync:
      - .env
      - app/
```

## Development

```bash
pip install -e .[dev]
python -m pytest
```