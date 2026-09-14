# JupyterLab over Tailscale

A single-user tool that runs JupyterLab and a small statistics dashboard on a Linux laptop and
makes both reachable from a tablet (or any other device) through Tailscale — and from nowhere
else.

Everything runs in Docker. The only host dependencies are **Docker Engine**, the **Docker Compose
v2 plugin** and **Tailscale**. Python, JupyterLab, FastAPI and every library live inside two
locally built images; nothing is installed on the host with `pip`, and no systemd user service is
used.

```text
 tablet ──(Tailscale, WireGuard)──► 100.x.y.z:8888  ──► container "jupyterlab"  (JupyterLab)
                                    100.x.y.z:8889  ──► container "stats"       (FastAPI dashboard)
                                        │                     │
                    published only on the Tailscale IPv4      ├─ ~/jupyter-workspace  (notebooks)
                    never on 0.0.0.0, loopback or the LAN     └─ /proc, /sys          (read-only)
```

## Contents

- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Opening it on the tablet](#opening-it-on-the-tablet)
- [Credentials and settings](#credentials-and-settings)
- [Graphical builder](#graphical-builder)
- [Commands](#commands)
- [Persistence](#persistence)
- [Statistics dashboard](#statistics-dashboard)
- [GPU support](#gpu-support)
- [Security model](#security-model)
- [Host settings and firewall behaviour](#host-settings-and-firewall-behaviour)
- [Updating](#updating)
- [Troubleshooting](#troubleshooting)
- [Uninstalling](#uninstalling)
- [Repository layout](#repository-layout)

## Architecture

`setup-jupyterlab-tailscale.sh` is the whole interface. The Docker files it deploys live next to
it in `stack/`; on `install` and `update` it copies them into an app directory and runs Docker
Compose there.

| Piece | Location | Notes |
| --- | --- | --- |
| Settings (password, ports, stats on/off) | `<repo>/.env` | Created with defaults on first install, mode `0600`, gitignored. |
| Stack sources | `<repo>/stack/` | `Dockerfile`, `compose.yaml`, `compose.gpu.yaml`, `jupyter/`, `stats/`. |
| Deployed stack (app dir) | `~/.local/share/jupyterlab-tailscale/` | A copy of `stack/` plus generated files; Compose project directory. |
| Runtime variables | `~/.local/share/jupyterlab-tailscale/.env` | Generated on every deploy: Tailscale IP, ports, profiles, uid/gid. **No secrets.** |
| Secrets | `~/.local/share/jupyterlab-tailscale/secrets/` | The password and its argon2 hash, mode `0600`, mounted as Compose secrets. |
| Notebooks | `~/jupyter-workspace` | Bind-mounted at `/workspace`. Never deleted by default. |

Compose project `jupyterlab-tailscale` runs two services on the default bridge network (so notebooks
and `pip` have outbound internet access):

| Service | Image (built locally) | Published on | Contents |
| --- | --- | --- | --- |
| `jupyterlab` | `jupyterlab-tailscale/jupyterlab:local` (~1 GB) | `<tailscale-ip>:8888` | JupyterLab 4.6.3, jupyter_server 2.21.0, ipykernel 7.3.0, ipywidgets 8.1.9, numpy 2.5.3, pandas 3.0.5, matplotlib 3.11.2, scipy 1.18.1 |
| `stats` | `jupyterlab-tailscale/stats:local` (~210 MB) | `<tailscale-ip>:8889` | FastAPI 0.141.1, uvicorn 0.53.0, nvidia-ml-py 13.610.43 |

Both images start from `python:3.13-slim-trixie` and install exactly the versions in
`stack/*/requirements.lock.txt` (every transitive package pinned, wheels only). Both containers run
as a non-root user whose uid/gid match yours, with all Linux capabilities dropped,
`no-new-privileges`, an init process, health checks, `restart: unless-stopped` and rotated JSON
logs (3 × 10 MB). The `stats` container additionally has a read-only root filesystem.

Inside its container each server listens on all of the *container's* interfaces — that is how
Docker forwards traffic to it. What decides who can connect is the host side of the port mapping,
which is always the Tailscale IPv4 address.

## Prerequisites

- A Linux machine with Docker Engine and the Compose v2 plugin, and your user in the `docker` group
  (log out and back in after adding it).
- Tailscale installed, logged in and connected on the laptop **and** on the tablet, both in the same
  tailnet.
- `sudo` rights for the one-time host step (see [below](#host-settings-and-firewall-behaviour)).
- About 2 GB of free disk space for images and build cache.
- Optional: an NVIDIA GPU with the NVIDIA Container Toolkit, for GPU statistics and GPU access in
  notebooks.

Check before installing:

```bash
docker compose version
docker info --format '{{.ServerVersion}}'
tailscale status
tailscale ip -4          # must print a 100.x.y.z address
```

If Tailscale is installed but disconnected: `sudo tailscale up`.

## Installation

Run the script as your normal user from the repository (not with `sudo`):

```bash
./setup-jupyterlab-tailscale.sh install
```

It is safe to run again at any time. `install`:

1. Checks Docker, Compose and the Tailscale IPv4 address (waits up to 30 s for it).
2. Creates `<repo>/.env` with the default settings if it does not exist, and validates it.
3. Creates `~/jupyter-workspace` if needed (an existing directory is left exactly as it is).
4. Removes leftovers of the previous, virtualenv-based version of this installer: its systemd user
   service, `~/.local/bin/jupyterlab-tailscale`, `~/.local/share/jupyterlab-tailscale/venv` and
   `~/.config/jupyterlab-tailscale`.
5. Refuses to continue if a port is already used by something else.
6. Copies `stack/` to the app dir, detects a usable NVIDIA GPU, writes the runtime `.env`.
7. Builds both images (cached layers make repeated runs fast and do not recreate containers).
8. Hashes the password with argon2 **inside the image** — only when it changed — and writes the
   secrets.
9. Starts the containers and waits until both report healthy.
10. Runs the [host step](#host-settings-and-firewall-behaviour) through `sudo` when it is needed.
11. Prints the URLs.

When the host step is needed and there is no terminal to ask for the `sudo` password, the script
prints the command to run instead, for example:

```text
ROOT_STEP_REQUIRED: host-setup 100.82.217.101 8888 8889
Run in a terminal: sudo /path/to/setup-jupyterlab-tailscale.sh host-setup 100.82.217.101 8888 8889
```

The containers are already running at that point; the host step only makes them survive reboots
reliably and restricts the ports when a firewall is active.

## Opening it on the tablet

With the tablet connected to the same tailnet, open:

| Service | URL | Login |
| --- | --- | --- |
| JupyterLab | `http://<tailscale-ip>:8888/lab` | password only |
| Statistics | `http://<tailscale-ip>:8889/` | username `jupyter` + the same password |

`./setup-jupyterlab-tailscale.sh status` prints both URLs with the real address. The addresses use
plain HTTP; the traffic between Tailscale devices is encrypted by Tailscale's WireGuard tunnel.

## Credentials and settings

The default password is:

```text
TailLab-7mK9-vQ2x-N4pR!
```

It is used for the JupyterLab login and, together with the fixed username `jupyter`, for the
statistics dashboard. `install` warns while the default is in use.

Settings live in `<repo>/.env`:

```bash
JUPYTER_PASSWORD='TailLab-7mK9-vQ2x-N4pR!'
JUPYTER_PORT='8888'
STATS_ENABLED='1'
STATS_PORT='8889'
STATS_USER='jupyter'
```

To change something, edit the file and apply it:

```bash
./setup-jupyterlab-tailscale.sh update
```

- **Password:** 8–128 characters; no single quote, backslash, control characters or leading/trailing
  spaces. A new password is re-hashed and both containers are recreated, which logs every browser
  out. An unchanged password keeps its hash, so sessions survive redeploys.
- **Ports:** 1024–65535 and different from each other. A changed port recreates only the affected
  container.
- **`STATS_ENABLED='0'`** stops and removes the `stats` container (and, with an active firewall,
  closes its port); `'1'` brings it back.

The script parses this file itself; it is never executed as shell code. JupyterLab only ever sees
the argon2 hash. The plain password reaches the `stats` container as a mounted secret file, never
through environment variables, `compose.yaml` or `docker inspect`. JupyterLab's token login is
disabled, and changing the password from the JupyterLab UI is turned off.

## Graphical builder

`builder.py` is an optional PyQt6 window over the same script and the same settings file. It is a
thin frontend: every action it takes is `setup-jupyterlab-tailscale.sh install | start | restart |
stop`, plus a read-only `docker compose ps` for the status badges. The command line keeps working
exactly as before, and both can be used side by side.

### Installing and launching

```bash
./run-builder.sh
```

- The first start creates `.venv` in the repository with `/usr/bin/python3` and installs the pinned
  wheels from `requirements-builder.txt` (PyQt6 6.11.0, PyQt6-Qt6 6.11.2, PyQt6-sip 13.12.0; about
  95 MB to download). Nothing is installed globally — `rm -rf .venv` removes every builder
  dependency.
- Later starts reuse the venv. It is rebuilt only when the pins or the Python version change.
  Another interpreter can be chosen with `PYTHON=/path/to/python3 ./run-builder.sh`.
- Ubuntu/Debian need `python3-venv`, and X11 sessions need `libxcb-cursor0` for Qt
  (`sudo apt install python3-venv libxcb-cursor0`); the launcher names whichever is missing.

The window follows the desktop's light or dark mode, using the same oya palette as the dashboard.

### Fields and buttons

| Control | Meaning |
| --- | --- |
| Password + Show/Hide | `JUPYTER_PASSWORD` — the JupyterLab password and the statistics password. |
| JupyterLab port | `JUPYTER_PORT`, default 8888. |
| Enable FastAPI statistics | `STATS_ENABLED`. Unticking it and deploying stops and removes the statistics container and closes its firewall port. |
| Statistics port | `STATS_PORT`, default 8889 (disabled while statistics are off). |
| Username: jupyter | The fixed statistics username (`STATS_USER`), shown read-only. |
| Services | A state dot and badge for JupyterLab and Statistics (Running, Starting, Unhealthy, Restarting, Stopped, Not deployed, Disabled), refreshed every 4 seconds, with the deployed URL and an **Open** button that starts the browser. |
| **Deploy / Update** | Validates, saves the settings, then runs `install`: builds the images and starts the containers. Changed passwords or ports recreate only the affected containers. |
| **Start / Restart** | `start` when nothing is running, otherwise `restart`. |
| **Stop** | `stop` (`docker compose stop`). |
| Output | Read-only log of every command and its output. |

The header shows the detected Tailscale IPv4 address (from `tailscale status --json`; Tailscale must be
connected) and whether Docker answers. Buttons are disabled while a command runs; the window stays
responsive throughout.

Deploy is refused, with the reasons listed under the form, when:

- the password breaks the [password rules](#credentials-and-settings);
- a port is not a whole number from 1024 to 65535, or both ports are equal;
- a port is already in use on the Tailscale address by another program (ports held by this stack's own
  containers are fine; to swap ports between the two services, press Stop first);
- Tailscale is not connected or Docker does not answer.

### The host step from the builder

When the script reports that the [host step](#host-settings-and-firewall-behaviour) is needed, the
builder asks through polkit (`pkexec /bin/bash setup-jupyterlab-tailscale.sh host-setup <ip>
<ports>`), which shows the desktop's own password dialog. If the dialog is dismissed, the services
still run; the builder shows the equivalent `sudo` command, and it asks again on the next Deploy,
Start or Restart until the step has been applied once.

### Configuration and security notes

- The builder edits `<repo>/.env` — the same file the script reads. It writes it atomically with mode
  `0600`, keeps comments and unknown lines, and honours `JLT_SETTINGS_FILE` and `JLT_APP_DIR`. It
  also writes `THEME='amazing'`, the name of the palette in `stack/theme/`.
- It reads the deployed `~/.local/share/jupyterlab-tailscale/.env` only to build the Open URLs, so
  they always point at what is actually running.
- Commands run through Qt's `QProcess` with argument lists; no shell is involved. The password never
  appears on a command line, in a child process environment, in `compose.yaml` or in the Output
  panel.
- Services stay published on the Tailscale address only; the builder has no way to choose another
  address.
- The builder has no uninstall button and never deletes `~/jupyter-workspace`.

### Equivalent CLI commands

| Builder | Command line |
| --- | --- |
| Edit fields + **Deploy / Update** | edit `.env`, then `./setup-jupyterlab-tailscale.sh install` (or `update` to also refresh the base image) |
| **Start / Restart** | `./setup-jupyterlab-tailscale.sh start` / `restart` |
| **Stop** | `./setup-jupyterlab-tailscale.sh stop` |
| Services badges | `./setup-jupyterlab-tailscale.sh status` |
| **Open** | the URLs printed by `status` |
| polkit dialog | `sudo ./setup-jupyterlab-tailscale.sh host-setup <tailscale-ip> <port> [<port>]` |

The builder's offscreen test suite runs with
`QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests`.

## Commands

| Command | What it does |
| --- | --- |
| `install` | Build and start everything; creates the settings file when missing. Idempotent. |
| `update` | Same as `install`, but also pulls a newer base image (`docker compose build --pull`). Use it after editing `.env` or pulling a new version of this repository. |
| `start` | Start the containers. If the Tailscale IPv4 changed, the ports follow the new address. |
| `stop` | Stop the containers (they stay stopped across reboots until `start`). |
| `restart` | `stop`, then `start`. |
| `status` | Settings, deployed ports, live vs. deployed Tailscale IP, container state and health, URLs, host step and firewall state. Exit code 3 when not installed. |
| `logs [--no-follow] [SERVICE...]` | Last 100 log lines of `jupyterlab` and/or `stats`, following unless `--no-follow`. |
| `uninstall [--yes] [--delete-workspace]` | See [Uninstalling](#uninstalling). |
| `host-setup <ip> <port> [<port>]`, `host-teardown` | Root-only helpers, normally run for you through `sudo`. |

Environment overrides: `JLT_SETTINGS_FILE` (settings file), `JLT_APP_DIR` (app dir),
`JLT_WORKSPACE_DIR` (notebook workspace), `JLT_GPU=auto|on|off`.

Plain Compose commands work too, from the app dir:

```bash
cd ~/.local/share/jupyterlab-tailscale
docker compose ps
docker compose config
docker compose logs -f jupyterlab
```

## Persistence

| Data | Where | Survives `update`/recreation | Removed by `uninstall` |
| --- | --- | --- | --- |
| Notebooks and files | `~/jupyter-workspace` (bind mount at `/workspace`) | yes | no (only with `--delete-workspace`) |
| JupyterLab settings, workspace layouts, IPython history, login cookie secret | Docker volume `jupyterlab-tailscale_jupyter_state` (`/state`) | yes | yes |
| Reserved for custom packages | Docker volume `jupyterlab-tailscale_custom_packages` (`/opt/custom`) | yes | yes |
| pip download cache | Docker volume `jupyterlab-tailscale_pip_cache` | yes | yes |
| Settings | `<repo>/.env` | yes | no |
| Anything else inside a container (e.g. `pip install --user` from a notebook) | container filesystem | **no** | yes |

Save everything you want to keep inside `/workspace` — that is the directory JupyterLab opens.

## Statistics dashboard

The `stats` service is a small FastAPI application styled after zenobia's *oya* design language
(theme "Amazing Moon", Orbitron headings, light and dark mode following the device, with a toggle
remembered by the browser). It works on phone, tablet and desktop widths and refreshes every
3 seconds.

| Route | Content |
| --- | --- |
| `/` | Dashboard: CPU (usage, per core, load, temperature), memory and swap, disk (the filesystem holding the workspace), uptime, network totals and rates (physical interfaces, with `tailscale0` shown separately), active Jupyter kernels (name, state, connections), NVIDIA GPU (utilisation, memory, temperature, power). |
| `/api/stats` | The same data as JSON. |
| `/health` | `{"status": "ok", "jupyter": "reachable"}` — used by the container health check. |

**Every** route, including `/health` and static files, requires HTTP Basic authentication (user
`jupyter`, the JupyterLab password). Host metrics come from the host's `/proc` and `/sys`, mounted
read-only; the Docker socket is not mounted. Kernel information comes from JupyterLab's REST API,
which the dashboard logs into with the same password (once, not on every refresh).

## GPU support

With `JLT_GPU=auto` (the default) the installer enables GPU access when `nvidia-smi -L` works on the
host and Docker has the NVIDIA runtime or a CDI specification, and it verifies that with a test
container. When enabled, `compose.gpu.yaml` gives both containers `gpus: all`:

- `jupyterlab` gets the `compute,utility` driver capabilities, so CUDA libraries you install into a
  notebook environment can use the GPU.
- `stats` gets `utility` only, which is enough for the GPU statistics.

Force it with `JLT_GPU=on ./setup-jupyterlab-tailscale.sh update` or disable it with `JLT_GPU=off`.

## Security model

- **Reachability.** Both ports are published only on the Tailscale IPv4 address. Compose refuses to
  start if that address is missing, so an empty value can never turn into "all interfaces". Nothing
  listens on loopback, the Wi-Fi/Ethernet address or `0.0.0.0`.
- **Transport.** Tailscale encrypts traffic between devices; the services themselves speak HTTP.
- **Authentication.** JupyterLab requires the password (argon2 hash, token login disabled, the server
  refuses to start without a valid hash). The dashboard requires HTTP Basic auth on every route.
- **Tailnet members.** Every device in your tailnet that can reach the laptop can reach the login
  pages. To restrict that, add a Tailscale ACL that allows TCP 8888/8889 on the laptop only from
  the tablet.
- **Containers.** Non-root user, all capabilities dropped, `no-new-privileges`, no Docker socket,
  host `/proc` and `/sys` read-only, secrets as `0600` files.
- **Notebooks run arbitrary code** as that container user, with internet access and write access to
  `~/jupyter-workspace` — which is the point of JupyterLab. Anyone with the password can do the same.
- **Residual LAN risk.** Docker publishes by destination address, not by interface. A device on the
  same local network that deliberately routes packets for the laptop's Tailscale address to it can
  reach the ports without going through Tailscale (it still hits the password prompts). With ufw
  active, the host step adds rules that drop such traffic; see the next section.
- Do not expose the ports through a router, a public reverse proxy or Tailscale Funnel.

## Host settings and firewall behaviour

The host step (`sudo ./setup-jupyterlab-tailscale.sh host-setup <ip> <ports>`) is the only part that
needs root. `install`, `update` and `start` run it when something is missing or out of date, and
`status` shows whether it is needed.

1. **Reboot reliability.** It writes `/etc/sysctl.d/60-jupyterlab-tailscale.conf` with
   `net.ipv4.ip_nonlocal_bind = 1`. Docker binds the published port on the Tailscale address itself;
   if Tailscale gets its address a few seconds after Docker restores containers at boot, that bind
   fails and Docker **never retries it**, leaving the containers stopped. With this setting the bind
   succeeds regardless of timing and the ports start working as soon as Tailscale is up. The
   previous value is recorded and restored by `host-teardown`.
2. **ufw (only when ufw is active).** Docker-published ports are forwarded before ufw's normal input
   rules are consulted, so `ufw allow` rules alone do not restrict them. The host step therefore adds
   both:
   - `ufw allow in on tailscale0 to any port <port> proto tcp` for each port (commented
     `jupyterlab-tailscale`), and
   - a marked block in `/etc/ufw/after.rules` with one `DOCKER-USER` rule per port that drops
     connections to `<tailscale-ip>:<port>` arriving on any interface other than `tailscale0`.
     This is the rule that actually enforces "tailnet only" for Docker ports. Note that `ufw reload`
     rebuilds the whole `DOCKER-USER` chain from that file.
3. **firewalld (only when running).** Opens the ports in the zone of `tailscale0` (creating a
   `jupyter-tailnet` zone if the interface has none). Docker's own firewalld zone accepts forwarded
   container traffic, so this does not add an interface restriction for Docker ports; the Tailscale
   binding remains the control.
4. **No active firewall.** No rules are created; `status` says so. The Tailscale-only binding is what
   keeps the services off other networks.

Ports that are no longer wanted (a changed port, statistics disabled) are removed from the rules on
the next run. The applied state is recorded in `/var/lib/jupyterlab-tailscale/state`.

## Updating

- **Settings changes:** edit `<repo>/.env`, then `./setup-jupyterlab-tailscale.sh update`.
- **New base image (security fixes in Debian/Python):** `./setup-jupyterlab-tailscale.sh update`.
- **New package versions:** edit `stack/jupyter/requirements.txt` or `stack/stats/requirements.txt`,
  regenerate the matching `requirements.lock.txt` with the command in that file's header, then run
  `update`.
- **New version of this tool:** update the repository (e.g. `git pull`), then `update`.

Containers are recreated only when their configuration or image actually changed. Recreating
JupyterLab stops running kernels; notebooks and settings are kept.

## Troubleshooting

**The tablet cannot connect.** Check `tailscale status` on both devices, `tailscale ping
<laptop>` from another device, and `./setup-jupyterlab-tailscale.sh status` on the laptop (both
containers should be `running`/`healthy`, and the live Tailscale IP should match the deployed one).

**Containers are stopped after a reboot.** `status` shows the container error (typically
`cannot assign requested address`). Run `./setup-jupyterlab-tailscale.sh start`, and make sure the
host step has been applied so it does not happen again.

**The Tailscale address changed.** Run `./setup-jupyterlab-tailscale.sh start` (or `restart`); the
ports move to the new address.

**A port is already in use.** `install` names the conflicting port. Pick another one in `.env`
(`JUPYTER_PORT`, `STATS_PORT`) and run `update`.

**`permission denied` talking to Docker.** Add yourself to the `docker` group
(`sudo usermod -aG docker "$USER"`) and log in again.

**A container is unhealthy or restarting.** `./setup-jupyterlab-tailscale.sh logs --no-follow
jupyterlab` (or `stats`). A JupyterLab container that exits immediately with a message about the
password hash means the secret is missing or damaged; run `update`.

**The dashboard shows "No NVIDIA GPU visible".** Check `nvidia-smi` on the host and
`docker info | grep -i runtime`; then `JLT_GPU=on ./setup-jupyterlab-tailscale.sh update` to see
why the GPU test fails.

**The build fails with "no space left on device".** Free disk space; `docker builder prune` removes
Docker's build cache (for all projects).

## Uninstalling

```bash
./setup-jupyterlab-tailscale.sh uninstall
```

You must type `uninstall` to confirm (or pass `--yes`; without a terminal, `--yes` is required). It
removes the containers, both images, the Docker volumes (JupyterLab settings and caches), the app
dir, and — through `sudo` — the host step (sysctl file, firewall rules, state).

It **keeps** `~/jupyter-workspace` and `<repo>/.env`. To delete the workspace as well, add
`--delete-workspace` and type `delete` when asked.

## Repository layout

```text
setup-jupyterlab-tailscale.sh      the CLI (install, start, stop, restart, status, logs, update, uninstall)
stack/Dockerfile                   base → jupyterlab and stats images
stack/compose.yaml                 services, ports, secrets, volumes, health checks
stack/compose.gpu.yaml             GPU override, used when a GPU is detected
stack/jupyter/                     JupyterLab config, requirements and lock file
stack/stats/                       FastAPI dashboard: app, templates, static assets, requirements and lock file
stack/theme/                       oya theme files shared with zenobia (palette of the builder window)
builder.py                         optional PyQt6 builder (thin frontend over the script)
run-builder.sh                     creates .venv with the pinned PyQt6 and starts the builder
requirements-builder.txt           PyQt6 pins for the builder venv
tests/test_builder.py              offscreen tests for the builder
plan.md                            implementation plan and progress
.env                               your settings (created by install or the builder, not committed)
.venv/                             builder virtualenv (created by run-builder.sh, not committed)
```
