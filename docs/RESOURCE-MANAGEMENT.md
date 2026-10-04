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

### The phantom process killer (Android 12+)

Android 12 kills forked app processes when more than ~32 exist across all
apps ("excessive CPU" killing cannot be configured on Android 12 itself).
Every proot container runs bash + proot + the service, so a multi-service
TuxComp stack gets close to that budget. Symptoms are `signal 9` exits and
half-dead stacks.

`tuxcomp setup` checks `settings get global settings_enable_monitor_phantom_procs`
when the ROM allows reading it. On Android 12L+ it can be turned off once in
Developer options ("Disable child process restrictions"); `tuxcomp doctor`
also prints the live process count on Android 12+ so the budget is visible.

---

## What TuxComp does about it

| Command | Purpose |
|---|---|
| `tuxcomp doctor` | Full read-only status: cpuset group, allowed/total cores and clusters, memory/swap, process count, governor/frequency, containers, orphan proot sessions, `--json` |
| `tuxcomp setup` | Pass/fail posture gate (exit 1 = not serving-ready). Only checks what it can verify: phantom setting (when readable), TuxComp wake lock, process budget |
| `tuxcomp wake on\|off\|status` | Manages the Termux wake lock with a marker so `down` releases only locks TuxComp acquired |
| `x-tuxcomp.keep_awake: true` | Acquire the wake lock on `up`, release when the last registered service stops |
| `x-tuxcomp.resources.threads: auto\|N` | Injects `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS`, `NUMEXPR_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS`, `UV_THREADPOOL_SIZE` from the allowed CPU count |
| `tuxcomp serve` | Supervises registered containers: restarts dead sessions with backoff, re-runs health checks |

**Full-speed mode (no root):** keep the screen on with Termux visible while
charging. Android then classifies the app as `top-app` and all cores become
available — `tuxcomp doctor` shows `8/8 cores` when this is active. This is
the intended mode for a demo server on a charger; screen-off remains the
cool-running mode with little cores.

**Not in scope:** root/Magisk cpuset manipulation, OEM power-manager hacks,
and adb recipes. TuxComp stays a `pip install` deployment tool; anything the
phone must be physically configured for is left to the user.

---

## Measured limits (P30 Pro, Android 10, Ollama 0.35.1 in proot)

Vision scoring of a 1600x1200 photo with `qwen3.5:0.8b`, `think: false`,
image bounded to 512 px, model warm:

| Scenario | Total | Split |
|---|---|---|
| Fresh photo, background (little cores) | ~37 s | prefill ~23 s + answer ~13 s |
| Same photo retried (prompt cache) | ~10-14 s | answer only |
| Backgrounded with 8-thread thrash (before fixes) | 471 s | — |

Answer generation runs at ~7.5 tok/s and produces only the final JSON (43-103
tokens, zero reasoning tokens). Vision prefill is the dominant cost and scales
with image tokens, which is why uploads are downscaled before the encoder.

---

## AI service tuning notes

Tuning that mattered, in order of impact:

1. `num_thread = allowed CPUs` (per request; the cpuset can change).
2. `OLLAMA_KEEP_ALIVE=-1` + a startup warmup request: no cold-load cost.
3. Compact prompt (the JSON schema already enforces the response shape).
4. Downscale images (`OLLAMA_MAX_IMAGE_DIM=512`).
5. `"think": false` and a small output cap with a safe upper bound.

A modern phone with a current Vulkan driver (e.g. Mali-G610+) is worth
testing for GPU inference with a native Termux llama.cpp build; the P30's
Mali-G76 (Vulkan 1.1, 2018 driver) is not a candidate, and Ollama has no
Android GPU path at all.

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
