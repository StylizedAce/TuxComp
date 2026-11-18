"""PostgreSQL support for proot-based containers.

Android kernels do not implement System V shared memory. proot emulates it
with ``libandroid-shmem``, whose key table can deadlock when a key is reused
after its segment was removed (termux/libandroid-shmem#23). PostgreSQL does
exactly that in ``initdb``'s probes and whenever the server restarts, so a
plain ``apt install postgresql`` container hangs at
``selecting default "shared_buffers" ...``.

The shim below is installed into the container before the service starts:

* ``postgres --check`` only validates settings, so it returns immediately
  instead of probing shared memory.
* every real invocation clears stale ``ashv_key_*`` links first, so a fresh
  segment is created instead of connecting to a dead one.

It only touches PostgreSQL's own binary path and is idempotent across
re-deploys and package upgrades.
"""

from __future__ import annotations

from tuxcomp.model import Service
from tuxcomp.roles import image_base_name

POSTGRES_SHIM_SCRIPT = r"""
set -eu
PGBIN=$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1 || true)
if [ -z "$PGBIN" ] || [ ! -x "$PGBIN/postgres" ]; then
  echo "postgres binary not found; skipping proot shm shim"
  exit 0
fi
if ! head -c 2 "$PGBIN/postgres" | grep -q '^#!'; then
  mv -f "$PGBIN/postgres" "$PGBIN/postgres.real"
fi
cat > "$PGBIN/postgres" <<'EOS'
#!/bin/sh
[ "$1" = "--check" ] && exit 0
rm -f /data/data/com.termux/files/usr/tmp/ashv_key_* 2>/dev/null || true
exec "$(dirname "$0")/postgres.real" "$@"
EOS
chmod +x "$PGBIN/postgres"
echo "installed proot shm shim in $PGBIN"
""".strip()


def is_postgres_service(service: Service) -> bool:
    """Return True when this service installs PostgreSQL from its image/role."""
    if service.image and image_base_name(service.image) == "postgres":
        return True
    tux = service.tuxcomp
    return bool(tux and any(p.startswith("postgresql") for p in tux.packages))


def postgres_shim_command(container: str) -> list[str]:
    return [
        "proot-distro",
        "login",
        container,
        "--",
        "/bin/sh",
        "-c",
        POSTGRES_SHIM_SCRIPT,
    ]
