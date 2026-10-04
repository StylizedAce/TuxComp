# Changelog

All notable changes to TuxComp are recorded here. Entries marked
`UNPUSHED — awaiting confirmation` exist only on a local branch until the
change is reviewed and approved for `main`.

## v0.8.22 — 2026-10-05

Status: UNPUSHED — awaiting confirmation

- Add `tuxcomp serve`: supervises registered containers, restarts dead
  sessions with exponential backoff (2s up to 5min), re-runs health checks,
  and can hold the wake lock while supervising (`--wake-lock`). `--once`
  runs a single sweep for cron/tests.

## v0.8.21 — 2026-10-05

Status: UNPUSHED — awaiting confirmation

- Add `x-tuxcomp.resources.threads: auto|N` per service: injects common
  thread-pool env vars (`OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`,
  `MKL_NUM_THREADS`, `NUMEXPR_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS`,
  `UV_THREADPOOL_SIZE`) resolved against the CPUs Android actually allows.
- Warn at `up` when an explicit thread request exceeds the allowed CPUs.

## v0.8.20 — 2026-10-05

Status: UNPUSHED — awaiting confirmation

- Add `tuxcomp setup`: pass/fail serving posture check (exit 1 until
  complete). Verifies the phantom-process setting (when the ROM exposes it),
  the tuxcomp wake lock, the process budget, and CPU posture. Only checks
  what the CLI can actually read; everything else is not reported.

## v0.8.19 — 2026-10-05

Status: UNPUSHED — awaiting confirmation

- Add `tuxcomp wake on|off|status`: manages the Termux wake lock with a marker
  file so `down`/`stop` release only locks TuxComp acquired.
- Add `x-tuxcomp.keep_awake: true`: acquire the wake lock after `up`, release
  it when the last registered container stops.

## v0.8.18 — 2026-10-05

Status: UNPUSHED — awaiting confirmation

- Add `tuxcomp doctor`: read-only status for phone containers — cpuset group,
  allowed vs total CPUs and locked clusters, governor, memory/swap, per-UID
  process count vs the Android 12+ phantom budget, wake-lock state, container
  counts, orphan proot sessions, and warnings. `--json` for scripting.

## v0.8.17 — 2026-10-05

Status: UNPUSHED — awaiting confirmation

- Add `docs/RESOURCE-MANAGEMENT.md`: how Android allocates CPU/memory to the
  Termux app, what TuxComp does about it, and the measured limits of a phone
  as a server (background cpuset, thread-count thrash, phantom-process budget).
- Add `docs/VERSIONING.md`: where the version lives, bump policy, branch
  workflow, and how to test a build on a phone via wheel + scp before pushing.
- Add this changelog.
- README: device dedication notice and a link to the resource guide.

## v0.8.16

- Drop dead tunnel helper and other unreferenced code.
