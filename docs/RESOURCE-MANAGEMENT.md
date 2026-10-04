# Android Resource Management for TuxComp

Why phone containers feel slow, what TuxComp does about it, and the measured
limits of a phone as a server. No root required for anything here.

---

## TL;DR

- TuxComp/proot cannot grant resources. Android decides which CPU cores an
  app may use via cgroups (`cpuset`), how much CPU share it gets, and when
  memory pressure kills it. A container is exactly as fast as the Termux app
  hosting it.
- Backgrounded Termux gets the little cores only. On the P30 we measured
  `Cpus_allowed_list: 2-3` (deep background) and `0-3` (background) out of 8
  cores. The big cores require `top-app`, which Android gives the *visible*
  app — so "Termux open on screen" is the only no-root full-speed mode.
- The worst slowdown is not the core limit itself: Ollama spawned 8 threads
  for 2-4 allowed cores and they spin-contended (0.14 tok/s vs 7 tok/s with
  the correct thread count). TuxComp injects correct thread counts and warns.
- Android 12+ additionally kills forked app processes beyond ~32 total
  ("phantom process killer"). `tuxcomp setup` checks the setting when the ROM
  allows reading it and points at the one Developer-options toggle.
- Measured result: vision scoring on the P30 went from 471 s to ~37 s fresh
  (screen off) and ~10-14 s warm. Sub-10 s fresh needs the screen-on
  `top-app` mode or newer hardware.

---

## How Android allocates resources

| App state | Example | cpuset group | Cores |
|---|---|---|---|
| Visible / focused | Termux open on screen | `top-app` | All, incl. prime |
| Foreground service | Termux wake lock + notification | `foreground` | Usually all |
| Cached / background | Screen off, other app visible | `background` | Little cluster only |

Measurements from the P30 (EMUI 10 / Android 10):

| When | `Cpus_allowed_list` | Notes |
|---|---|---|
| Heavy background state | `2-3` | 2 little cores |
| Later, same app instance | `0-3` | 4 little cores — cpuset is dynamic |
| Group | `cpuset:/background` | Big cores never allowed in background |

The background cgroup also carries a low CPU *share* (~5% vs ~95% for
foreground on EMUI-era kernels), which only matters under contention — but a
server is always under contention with itself.

This behavior is universal: Google/Samsung users report the same (Termux
issue #5086): Pixel background = 7 cores, Samsung One UI 8 = 4 mid cores,
both full cores only in `top-app`.

### CPU thread counts, not just cores

Ollama (and most threaded runtimes) read the *total* CPU count, not the
allowed cpuset. On the P30 that meant 8 threads fighting over 2 cores:

| Setting | Generation speed |
|---|---|
| default (8 threads, 2 allowed cores) | 0.14 tok/s |
| `num_thread = allowed CPUs` | 7 tok/s |

TuxComp handles this two ways: `tuxcomp doctor` reports allowed vs total
cores, and `x-tuxcomp.resources.threads: auto` injects thread-count env vars
for common runtimes.

### The phantom process killer and background eviction

Android 12 kills forked app processes when more than ~32 exist across all
apps ("excessive CPU" killing cannot be configured on Android 12 itself).
Every proot container runs bash + proot + the service, so a multi-service
TuxComp stack gets close to that budget. Symptoms are `signal 9` exits and
half-dead stacks. Even without the killer, Android evicts cached apps
aggressively, which demotes or kills the Termux process tree.

The phone cannot apply these settings to itself, so `tuxcomp setup --adb`
(host-side, dry-run by default) applies the community-standard tweaks through
adb:

```bash
adb shell settings put global settings_enable_monitor_phantom_procs false
adb shell device_config put activity_manager max_phantom_processes 2147483647
adb shell device_config put activity_manager max_cached_processes 256
adb shell device_config put activity_manager max_empty_time_millis 43200000
adb shell device_config set_sync_disabled_for_tests persistent
```

- `--apply` executes them; `--restart-termux` additionally force-stops the
  Termux app so everything takes effect (this stops every container — reopen
  Termux and run `tuxcomp up --all`).
- adb is found automatically: PATH, `ANDROID_HOME`/`ANDROID_SDK_ROOT`, Android
  Studio's SDK and the common platform-tools folders. **Android Studio is not
  required**; any platform-tools install works (`scoop/choco install adb`,
  `brew install android-platform-tools`, `apt install adb`, ...). Use
  `ANDROID_SERIAL` when several devices are connected.
- On Android 14+, Developer options has a "Disable child process
  restrictions" toggle that covers the phantom side; the cached-process keys
  still need the commands.

**Measured impact (P30 Pro, Android 10):** after the settings plus a full
Termux restart, the app came up in the `top-app` cpuset with `8 of 8` CPUs
instead of `2-3`, and a fresh vision scoring dropped from **84 s to 9.8 s**
(MiniCPM-V 4.6, 384 px, warm model). The elevation was sticky across wake
lock release; verify with `tuxcomp doctor` (it shows the group and core list)
and restart Termux if the app demotes again.

---

## What TuxComp does about it

| Command | Purpose |
|---|---|
| `tuxcomp doctor` | Full read-only status: cpuset group, allowed/total cores and clusters, memory/swap, process count, governor/frequency, containers, orphan proot sessions, `--json` |
| `tuxcomp setup` | Pass/fail posture gate (exit 1 = not serving-ready). Only checks what it can verify: phantom setting (when readable), TuxComp wake lock, process budget |
| `tuxcomp setup --adb` | Host-side: detect adb and plan/execute the Android background tweaks (dry-run unless `--apply`; optional `--restart-termux`) |
| `tuxcomp wake on\|off\|status` | Manages the Termux wake lock with a marker so `down` releases only locks TuxComp acquired |
| `x-tuxcomp.keep_awake: true` | Acquire the wake lock on `up`, release when the last registered service stops |
| `x-tuxcomp.resources.threads: auto\|N` | Injects `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS`, `NUMEXPR_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS`, `UV_THREADPOOL_SIZE` from the allowed CPU count |
| `tuxcomp serve` | Supervises registered containers: restarts dead sessions with backoff, re-runs health checks |

**Full-speed mode (no root):** get the app into the `top-app` cpuset, which on
the P30 needed the `--adb` settings above plus a Termux restart. `tuxcomp
doctor` shows the current group and core list (`8/8` when elevated). If it
demotes, restart Termux. Screen-off background mode remains the cool-running
mode with 2-4 little cores.

---

## Measured limits (P30 Pro, Android 10, Ollama 0.35.1 in proot)

Vision scoring of a fresh ~1600 px photo (downscaled to 384 px) with
MiniCPM-V 4.6 (instruct, no reasoning mode), warm model:

| State | Total | Split |
|---|---|---|
| `top-app`, 8/8 cores (after `setup --adb` + Termux restart) | **9.3-9.8 s** | prefill 313 tok @ 46 tok/s + answer 46 tok @ 18 tok/s |
| Background, 2/8 cores (deep background) | 84 s | prefill 70 s + answer 13 s |
| Background, 8-thread thrash (initial state) | 471 s | — |

The phone state matters more than anything else in this table: same model,
same image, ~9x difference between `top-app` and a 2-core background cpuset.
Answer generation produces only the final JSON (35-46 tokens, zero reasoning
tokens). Vision prefill is the dominant cost and scales with image tokens,
which is why uploads are downscaled before the encoder.

---

## AI service tuning notes

Tuning that mattered, in order of impact:

1. cpuset state: `top-app` (8 cores incl. 2.6 GHz prime cores) vs background
   (2-4 little cores) is a ~9x swing. `tuxcomp setup --adb` + Termux restart
   got the P30 there.
2. `num_thread = allowed CPUs` (per request; the cpuset can change).
3. `OLLAMA_KEEP_ALIVE=-1` + a startup warmup request: no cold-load cost.
4. Model architecture: MiniCPM-V 4.6 (instruct) has no reasoning mode to
   disable and was faster state-for-state than qwen3.5's hybrid thinking
   family, whose upstream tag also drifted to an MTP build that is ~5x slower
   per token on this CPU.
5. Compact prompt (the JSON schema already enforces the response shape).
6. Downscale images (`OLLAMA_MAX_IMAGE_DIM`).
7. A small output cap with a safe upper bound.

A modern phone with a current Vulkan driver is worth testing for GPU
inference, but the P30's Mali-G76 (Vulkan 1.1, 2018 driver) was tested
exhaustively and cannot work:

- Native Termux has real GPU packages now: `ollama-backend-vulkan`,
  `llama-cpp-backend-vulkan`, `llama-cpp-backend-opencl`.
- Native Ollama with the Vulkan backend **detects the Mali-G76**
  (`library=Vulkan name=Vulkan0`), then fails at model load:
  `device Vulkan0 does not support 16-bit storage` → `Unsupported device`.
  `GGML_VK_DISABLE_F16=1` does not bypass it.
- Native llama.cpp Vulkan fails the same way. Registering the Mali blob as an
  OpenCL ICD makes the platform enumerate, but ggml-opencl rejects it:
  `unsupported GPU 'Mali-G76'`.
- A glibc proot can never use this GPU at all: the Android ICD depends on
  Bionic (`libion.so`), which a Linux process cannot load. Mesa panfrost is
  not an option either (Android has no `/dev/dri`; it uses the ARM kbase
  driver).

Conclusion for TuxComp: GPU is not a proot/container feature on this class of
hardware — no compose key or rootfs file can add a Vulkan feature the driver
lacks. On newer GPUs (Mali Valhall G57+/G610+, Adreno) the tested path is a
**native** Termux service (`ollama-backend-vulkan` or llama.cpp with
`-ngl 99`), outside proot; that is documented here rather than implemented as
a TuxComp command so the CLI stays free of device-specific machinery.

---

## Measurement cookbook

```bash
# cgroup + allowed CPUs
cat /proc/self/cgroup
grep Cpus_allowed_list /proc/self/status

# CPU clusters / governor (readable on the P30 without root)
cat /sys/devices/system/cpu/online
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor

# memory / swap
grep MemAvailable /proc/meminfo
grep SwapFree /proc/meminfo

# process budget (Android 12+ phantom limit is ~32 total)
ps -A -o pid | wc -l

# text benchmark: read eval_count / eval_duration from the API reply
curl -s http://127.0.0.1:11434/api/generate -d '{
  "model":"qwen3.5:0.8b","prompt":"Say hi.","stream":false,"think":false,
  "options":{"num_predict":8,"num_ctx":1024}
}'
```

Record every benchmark together with the `Cpus_allowed_list` it ran under,
otherwise the number is meaningless.

---

## Sources

- Termux issue #5086 — Samsung vs Pixel background cpuset/core masks
- Termux issue #4657 — TermuxService stops without notification
- agnostic-apollo Android Docs — phantom/cached/empty processes (Android 12+)
- Termux proot issue #91 — proot ptrace overhead
- llama.cpp discussions #23193 / #23057 — Vulkan on modern Mali, driver bugs
- Our measurements: P30 Pro, Android 10, Ollama 0.35.1 in proot, Oct 2026
