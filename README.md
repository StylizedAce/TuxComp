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

Expose a container to the internet via a Cloudflare tunnel. TuxComp runs **one
shared, host-global cloudflared instance** on the device (Termux shell, outside
any container) that serves every exposed service — one tunnel connection, many
public hostnames → localhost ports. The binary is installed automatically if
missing; if a token is given it is stored at `~/.tuxcomp/tunnel-token` and used
with `--token`. Idempotent: if cloudflared is already running it is left alone
(never killed — other projects may rely on it), and `down` does NOT stop it.

**Token-based tunnel** (dashboard-created tunnel):

```yaml
x-tuxcomp:
  cloudflared:
    token: "${CLOUDFLARED_TOKEN}"
```

**Login-based tunnel** (registered via `cloudflared tunnel login` on the device):

```yaml
x-tuxcomp:
  cloudflared: {}
```

Optionally name the tunnel (from `cloudflared tunnel list`):

```yaml
x-tuxcomp:
  cloudflared:
    token: "${CLOUDFLARED_TOKEN}"
    tunnel: my-tunnel-name
```

Manually stop the tunnel with `pkill -f '[c]loudflared tunnel run'` on the device.

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