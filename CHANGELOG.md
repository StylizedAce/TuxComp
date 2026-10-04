# Changelog

All notable changes to TuxComp are recorded here. Entries marked
`UNPUSHED — awaiting confirmation` exist only on a local branch until the
change is reviewed and approved for `main`.

## v0.8.25 — 2026-10-05

- Add `tuxcomp setup --adb` (host-side): finds adb automatically (PATH,
  `ANDROID_HOME`/`ANDROID_SDK_ROOT`, Android Studio's SDK and common
  platform-tools folders - Android Studio is not required), resolves the
  target device (`ANDROID_SERIAL` aware), and plans/applies the Android
  background tweaks (`settings_enable_monitor_phantom_procs`,
  `max_phantom_processes`, `max_cached_processes`, `max_empty_time_millis`,
  device_config sync freeze). Dry-run by default; `--apply` to execute;
  `--restart-termux` force-stops Termux afterwards. Reads the phone's Android
  release and skips version-specific keys instead of reporting failures.
- Docs: measured impact of the tweaks plus a Termux restart on the P30
  (`top-app`, 8/8 cores, fresh vision scoring 84 s -> 9.8 s).

## v0.8.24 — 2026-10-05

- Docs: record the exhaustively tested Mali-G76 GPU results (native
  `ollama-backend-vulkan` detects the GPU but the driver lacks 16-bit storage;
  ggml-opencl rejects Mali; glibc proot cannot load the Bionic ICD). Documents
  the native-only GPU path for newer phones instead of adding device-specific
  commands to the CLI.

## v0.8.23 — 2026-10-05

- `tuxcomp deploy` now asks the target for a `doctor` summary and surfaces
  resource warnings (allowed CPUs, wake lock, process budget) before pushing.
- README: document the new `doctor`, `setup`, `wake` and `serve` commands.

## v0.8.22 — 2026-10-05

- Add `tuxcomp serve`: supervises registered containers, restarts dead
  sessions with exponential backoff (2s up to 5min), re-runs health checks,
  and can hold the wake lock while supervising (`--wake-lock`). `--once`
  runs a single sweep for cron/tests.

## v0.8.21 — 2026-10-05

- Add `x-tuxcomp.resources.threads: auto|N` per service: injects common
  thread-pool env vars (`OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`,
  `MKL_NUM_THREADS`, `NUMEXPR_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS`,
  `UV_THREADPOOL_SIZE`) resolved against the CPUs Android actually allows.
- Warn at `up` when an explicit thread request exceeds the allowed CPUs.

## v0.8.20 — 2026-10-05

- Add `tuxcomp setup`: pass/fail serving posture check (exit 1 until
  complete). Verifies the phantom-process setting (when the ROM exposes it),
  the tuxcomp wake lock, the process budget, and CPU posture. Only checks
  what the CLI can actually read; everything else is not reported.

## v0.8.19 — 2026-10-05

- Add `tuxcomp wake on|off|status`: manages the Termux wake lock with a marker
  file so `down`/`stop` release only locks TuxComp acquired.
- Add `x-tuxcomp.keep_awake: true`: acquire the wake lock after `up`, release
  it when the last registered container stops.

## v0.8.18 — 2026-10-05

- Add `tuxcomp doctor`: read-only status for phone containers — cpuset group,
  allowed vs total CPUs and locked clusters, governor, memory/swap, per-UID
  process count vs the Android 12+ phantom budget, wake-lock state, container
  counts, orphan proot sessions, and warnings. `--json` for scripting.

## v0.8.17 — 2026-10-05

- Add `docs/RESOURCE-MANAGEMENT.md`: how Android allocates CPU/memory to the
  Termux app, what TuxComp does about it, and the measured limits of a phone
  as a server (background cpuset, thread-count thrash, phantom-process budget).
- Add `docs/VERSIONING.md`: where the version lives, bump policy, branch
  workflow, and how to test a build on a phone via wheel + scp before pushing.
- Add this changelog.
- README: device dedication notice and a link to the resource guide.

## v0.8.16

- Drop dead tunnel helper and other unreferenced code.
