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
| `tuxcomp up a b c` / `tuxcomp up --all` | Start registered containers from saved config |
| `tuxcomp down -f compose.yml` | Stop services (keeps state) |
| `tuxcomp down a b c` / `tuxcomp down --all` | Stop registered containers (--all includes tunnels) |
| `tuxcomp start <container>` | Restart a container from its saved config |
| `tuxcomp stop <container>` | Stop a container |
| `tuxcomp ps -f compose.yml` | List running services |
| `tuxcomp logs <service>` | Tail container logs |
| `tuxcomp exec <container> <cmd>` | Run a command inside a container |
| `tuxcomp rebuild <container>` | Stop + remove + re-up with new code |
| `tuxcomp deploy -f compose.yml` | Upgrade tuxcomp on target, push files, start stack |
| `tuxcomp plan -f compose.yml` | Print what would run, without running it |

## Cloudflared

Expose containers to the internet via Cloudflare tunnels. Each tunnel runs in
**one shared, named proot container** that every project on the device can use.
(Proot is required on Android: `/etc/resolv.conf` is read-only on the host, so
cloudflared's Go DNS resolver fails there — inside proot it works.)

**Owning a tunnel** (provides the token; creates/ensures the container):

```yaml
x-tuxcomp:
  cloudflared:
    container: tuxcomp-cloudflared     # optional; default tuxcomp-cloudflared
    token: "${CLOUDFLARED_TOKEN}"
```

The token is always overwritten on deploy (last owner wins). You can run
**multiple tunnels** (different accounts) side by side with different names.

**Using a tunnel without owning it** (auto-ensure it's running, no token):

```yaml
x-tuxcomp:
  cloudflared:
    container: tuxcomp-cloudflared     # ensure this tunnel is up
```

**Plain port exposure** — most projects need nothing at all: expose a port and
add a route in the Cloudflare dashboard (`lt.your-domain.com → localhost:8090`):

```yaml
services:
  api:
    ports: ["8090:8090"]
```

`down` never stops tunnel containers (they're shared); stop one explicitly:
`tuxcomp down tuxcomp-cloudflared`. `tuxcomp up <name>` reconnects it in
seconds. Replace a tunnel entirely: `tuxcomp rmi tuxcomp-cloudflared` then
re-deploy with the new token.

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