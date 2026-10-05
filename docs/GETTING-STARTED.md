# Getting started: from a fresh phone to a serving phone

This is the complete walkthrough for turning an Android phone into a TuxComp
server: install Termux, open SSH, plant a key, harden it, deploy your first
project (which installs TuxComp on the phone automatically), get full CPU
cores, and survive reboots.

Commands are shown for a Windows PC (PowerShell) plus the Unix/macOS
equivalent where it differs. Examples use `root@192.168.1.153` and SSH port
`8022`. Replace the IP with your phone's.

> **About the username:** Termux's SSH server accepts any username and always
> runs you as the Termux app user, so `root@` works fine (and is what the
> examples use). `whoami` in Termux prints the "real" name (`u0_aNNN`); using
> it is equally valid.

---

## 0. Phone: install Termux from F-Droid

The Play Store build of Termux is deprecated and won't receive package
updates — use F-Droid for everything.

1. Open the phone browser, go to [f-droid.org](https://f-droid.org), download
   the F-Droid APK and install it (allow "install unknown apps" for the
   browser when prompted).
2. Open F-Droid, search for **Termux**, install it.
3. Optional but recommended for later steps:
   - **Termux:Boot** (start services automatically after a reboot)
   - **Termux:API** (extra Android tooling; not required)

Open Termux once to let it unpack.

## 1. Phone: prepare the server inside Termux

Type these in Termux (the on-screen terminal):

```bash
pkg update
pkg install -y openssh python git proot-distro
passwd
sshd
whoami
```

What each piece is for:

| Command | Why |
|---|---|
| `openssh` | the SSH server (`sshd`) — Termux does **not** run one by default |
| `python` | runs TuxComp on the phone (brings `pip`) |
| `git` | lets `pip install git+https://...` work during the first deploy |
| `proot-distro` | the container engine TuxComp drives |
| `passwd` | sets the password for the first SSH login; you'll throw it away later |
| `sshd` | starts the SSH server on port **8022** |
| `whoami` | your username (e.g. `u0_a212`) |

Keep the phone awake while it serves:

```bash
termux-wake-lock
```

And in Android: **Settings → Apps → Termux → Battery → Unrestricted**
(exact wording varies; on Huawei/EMUI also allow Termux under "App launch").

Find the phone's Wi-Fi IP: **Settings → Wi-Fi → (network) → IP address**.

> `sshd` only runs while Termux is alive. After a phone reboot or a force-stop,
> run `sshd` again — or use [section 7](#7-survive-a-reboot-termuxboot) to make
> it automatic.

## 2. PC: first SSH contact + plant your key

You connect once with the password, then install your key and never use the
password again.

```powershell
# 1. create a key if you don't have one (accept defaults)
ssh-keygen -t ed25519

# 2. first login — accept the host fingerprint, type the passwd password
ssh -p 8022 root@192.168.1.153

# 3. back on the PC: append your public key to the phone's authorized_keys
Get-Content $env:USERPROFILE\.ssh\id_ed25519.pub |
  ssh -p 8022 root@192.168.1.153 'mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys'

# 4. verify key login (should NOT ask for a password)
ssh -p 8022 root@192.168.1.153 'echo key-ok'
```

On Linux/macOS step 3 is just `ssh-copy-id -p 8022 root@192.168.1.153`.

> The single quotes around the remote command matter: PowerShell strips inner
> double quotes, so use single quotes (or the `tuxcomp remote bootstrap`
> command below, which does all of this for you).

## 3. Harden: key-only SSH

Keep an SSH session open while doing this, so you can't lock yourself out.
In Termux (or over the open session):

```bash
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' $PREFIX/etc/ssh/sshd_config
pkill sshd; sshd
```

Verify from a **new** PC terminal — it must log in with no password prompt:

```powershell
ssh -p 8022 root@192.168.1.153 'echo hardened-ok'
```

Notes:

- SSH on 8022 is for your LAN. Do not port-forward it from the router; remote
  access is done with Cloudflare tunnels (see the README).
- If a key stops working, reopen Termux locally and set
  `PasswordAuthentication yes` again with the same `sed` pattern.

## 4. PC: install TuxComp and register the phone

```powershell
pip install git+https://github.com/StylizedAce/TuxComp.git

tuxcomp remote add phone --host root@192.168.1.153 --port 8022 --default
tuxcomp remote test phone
```

`remote test` checks non-interactive (key) SSH, the phone's TuxComp version
and the device model. Once set, `tuxcomp deploy` uses this target by default.

If key auth isn't set up yet, `tuxcomp remote bootstrap phone` can create the
key, plant it over an interactive password session, and verify it:

```powershell
tuxcomp remote bootstrap phone
```

## 5. First deploy

Write a compose file with the deployment block. Minimal working example:

```yaml
services:
  web:
    image: nginx:alpine
    container_name: demo-web
    ports: ["8088:80"]
    restart: unless-stopped

x-tuxcomp:
  keep_awake: true                # wake lock while the stack runs
  deploy:
    remote_dir: ~/demo-web
    sync:
      - html/                      # pushed after the local build (if any)
```

Then:

```powershell
tuxcomp plan -f docker-compose.yml      # optional: show what would run
tuxcomp deploy -f docker-compose.yml
```

The first deploy does everything on the phone:

1. checks that SSH works non-interactively;
2. installs any missing Termux packages (`python`, `git`, `proot-distro`);
3. installs/upgrades TuxComp on the phone from GitHub;
4. prints any `doctor` warnings from the target;
5. runs your local `build:` command, if configured;
6. copies the compose file (and `sync:` paths) into `remote_dir`;
7. runs `tuxcomp up -f` on the phone, which creates the proot containers.

Check it: `tuxcomp ps`, `tuxcomp logs demo-web`, or open
`http://192.168.1.153:8088`.

## 6. Full-speed posture (host PC + USB cable)

Android pins backgrounded apps to a few little cores. One-time, from the PC
with USB debugging enabled on the phone and the cable connected:

```powershell
tuxcomp setup --adb                    # dry-run: shows exactly what changes
tuxcomp setup --adb --apply            # applies the Android background tweaks
tuxcomp setup --adb --apply --restart-termux   # + force-stops Termux
```

The restart is what makes the settings fully apply. TuxComp prints what to do:

> Reopen Termux on the phone, then run: `tuxcomp up --all`

**Do exactly that** — reopening Termux and running `up --all` brings every
container back (and re-acquires the wake lock when your compose has
`keep_awake: true`). Verify on the phone:

```bash
tuxcomp doctor     # cpuset group: top-app, CPUs: 8 of 8 allowed
tuxcomp setup      # setup: COMPLETE
```

`setup --adb` also exempts Termux from battery optimization
(`dumpsys deviceidle whitelist`). On Android 14+ there is an extra Developer
options toggle ("Disable child process restrictions"); the `--adb` command
covers the config keys either way.

If `doctor` still shows `2-4` CPUs, restart Termux once more — the promotion
to `top-app` happens when Termux is (re)started after the settings are in
place.

## 7. Survive a reboot (Termux:Boot)

Phones reboot. Without help, `sshd` and every container stay down until you
open Termux and run commands by hand.

1. Install **Termux:Boot** from F-Droid and open it once.
2. On the phone (over SSH or in Termux):

```bash
tuxcomp boot install     # writes ~/.termux/boot/00-tuxcomp
tuxcomp boot status
```

The boot script starts the wake lock, `sshd`, `tuxcomp up --all`, and a
background `tuxcomp serve --wake-lock` supervisor so containers that die get
restarted. Verify by rebooting the phone, waiting a minute, then from the PC:

```powershell
tuxcomp remote test phone
ssh -p 8022 root@192.168.1.153 'tuxcomp ps'
```

Remove it with `tuxcomp boot remove`.

## 8. Day-2 operations

Run these on the phone (Termux or SSH), or from the PC where noted.

| Task | Command |
|---|---|
| Status | `tuxcomp ps`, `tuxcomp doctor` |
| Full-speed / posture check | `tuxcomp setup` (phone), `tuxcomp setup --adb` (PC) |
| Keep alive | `tuxcomp wake on` / `off` / `status` |
| Supervise containers | `tuxcomp serve --detach` (start), `serve --status`, `serve --stop` |
| Logs | `tuxcomp logs <container>` |
| Shell into a container | `tuxcomp exec <container>` |
| Restart one container | `tuxcomp start <container>` / `tuxcomp stop <container>` |
| Rebuild with new code/config | `tuxcomp rebuild <container>` |
| Stop the stack (keep state) | `tuxcomp down <container>...` or `down --all` |
| Start it again | `tuxcomp up --all` |
| Remove permanently | `tuxcomp rmi <container>` |
| Push updated code from PC | `tuxcomp deploy -f docker-compose.yml` |

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Connection refused` on port 8022 | `sshd` isn't running — open Termux and run `sshd` (or install the boot script) |
| Password prompt every time | key not planted — repeat section 2, or `tuxcomp remote bootstrap phone` |
| `deploy: cannot reach ... over ssh` | wrong IP, wrong port, `sshd` not running, or swapped Wi-Fi network |
| `proot-distro: command not found` / `could not install tuxcomp` | missing Termux packages — `pkg install -y openssh python git proot-distro` |
| `setup: NOT COMPLETED` (phantom-process killing) | run `tuxcomp setup --adb --apply --restart-termux`, or the Android 14+ Developer-options toggle |
| `setup: NOT COMPLETED` (wake lock) | `tuxcomp wake on`, or `x-tuxcomp.keep_awake: true` + re-deploy |
| Only 2-4 CPUs allowed | `tuxcomp setup --adb --apply --restart-termux`, reopen Termux, `tuxcomp up --all` |
| Containers die with `signal 9` | phantom-process settings not applied — same as above |
| Warnings about orphan proot sessions | `tuxcomp doctor`, then `tuxcomp down --all` before removing/rebuilding |

See [`VERSIONING.md`](VERSIONING.md) for how releases work and
[`RESOURCE-MANAGEMENT.md`](RESOURCE-MANAGEMENT.md) for the full story on
Android CPU/memory behavior and every measured number.
