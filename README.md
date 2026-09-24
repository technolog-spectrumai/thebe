# JupyterLab over Tailscale

A single-user tool that runs JupyterLab, a statistics dashboard and a package manager page on a
Linux laptop and makes them reachable from a tablet (or any other device) through Tailscale — and
from nowhere else.

Everything runs in Docker. The only host dependencies are **Docker Engine**, the **Docker Compose
v2 plugin** and **Tailscale**. Python, JupyterLab, FastAPI and every library live inside locally
built images; nothing is installed on the host with `pip`, and no systemd user service is used. An
optional PyQt6 builder window and a headless runner (`./run.sh`, for servers without a display)
share one `config.yaml` and keep their own dependencies in a project-local `.venv`.

```text
 tablet ──(Tailscale, WireGuard)──► 100.x.y.z:8888  ──► container "jupyterlab"  (JupyterLab)
                                    100.x.y.z:8889  ──► container "stats"       (dashboard + Dependencies page)
                                        │                     │         │
                    published only on the Tailscale IPv4      │         └─► container "deps" (pip runner,
                    never on 0.0.0.0, loopback or the LAN     │             internal only, no published port)
                                                              ├─ ~/jupyter-workspace  (notebooks)
                                                              └─ /proc, /sys          (read-only, stats only)
```

## Contents

- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Opening it on the tablet](#opening-it-on-the-tablet)
- [Connecting by name over HTTPS](#connecting-by-name-over-https)
- [Credentials and settings](#credentials-and-settings)
- [Graphical builder](#graphical-builder)
- [Headless runner and config.yaml](#headless-runner-and-configyaml)
- [Imported directories](#imported-directories)
- [Commands](#commands)
- [Persistence](#persistence)
- [Statistics dashboard](#statistics-dashboard)
- [Dependencies page](#dependencies-page)
- [Python packages: three layers](#python-packages-three-layers)
- [Themes](#themes)
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
| Configuration of the builder and `run.sh` | `<repo>/config.yaml` | What you edit when you use the builder or `run.sh`; they write `.env` from it. Mode `0600`, gitignored. See [config.yaml](#headless-runner-and-configyaml). |
| Settings (password, ports, stats on/off) | `<repo>/.env` | The installer's input. Created with defaults on first install, mode `0600`, gitignored. |
| Stack sources | `<repo>/stack/` | `Dockerfile`, `compose.yaml`, `compose.gpu.yaml`, `compose.tls.yaml`, `jupyter/`, `stats/`, `theme/`. |
| Deployed stack (app dir) | `~/.local/share/jupyterlab-tailscale/` | A copy of `stack/` plus generated files; Compose project directory. |
| Runtime variables | `~/.local/share/jupyterlab-tailscale/.env` | Generated on every deploy: Tailscale IP and name, URL scheme, TLS state, ports, profiles, uid/gid. **No secrets.** |
| Secrets | `~/.local/share/jupyterlab-tailscale/secrets/` | The password, its argon2 hash and a random package-runner token, each mode `0600`, mounted as Compose secrets. |
| HTTPS certificate | `/var/lib/jupyterlab-tailscale/tls/` | `<name>.crt` and `<name>.key` from `tailscale cert`, owned by root and readable by your group; mounted read-only through `compose.tls.yaml` when valid. |
| Notebooks | `~/jupyter-workspace` | Bind-mounted at `/workspace`. Never deleted by default. |

Compose project `jupyterlab-tailscale` runs three services on the default bridge network (so notebooks
and `pip` have outbound internet access):

| Service | Image (built locally) | Published on | Contents |
| --- | --- | --- | --- |
| `jupyterlab` | `jupyterlab-tailscale/jupyterlab:local` (~1 GB) | `<tailscale-ip>:8888` | JupyterLab 4.6.3, jupyter_server 2.21.0, ipykernel 7.3.0, ipywidgets 8.1.9, numpy 2.5.3, pandas 3.0.5, matplotlib 3.11.2, scipy 1.18.1 |
| `stats` | `jupyterlab-tailscale/stats:local` (~210 MB) | `<tailscale-ip>:8889` | FastAPI 0.141.1, uvicorn 0.53.0, nvidia-ml-py 13.610.43: the dashboard and the Dependencies page |
| `deps` | reuses `jupyterlab-tailscale/jupyterlab:local` | nothing (internal) | A small pip runner used by the Dependencies page |

`stats` and `deps` belong to the `stats` profile and are only created while statistics are enabled.

Both images start from `python:3.13-slim-trixie` and install exactly the versions in
`stack/*/requirements.lock.txt` (every transitive package pinned, wheels only). All containers run
as a non-root user whose uid/gid match yours, with all Linux capabilities dropped,
`no-new-privileges`, an init process, health checks, `restart: unless-stopped` and rotated JSON
logs (3 × 10 MB). `stats` and `deps` additionally have a read-only root filesystem.

Inside its container each server listens on all of the *container's* interfaces — that is how
Docker forwards traffic to it. What decides who can connect is the host side of the port mapping,
which is always the Tailscale IPv4 address.

## Prerequisites

- A Linux machine with Docker Engine and the Compose v2 plugin, and your user in the `docker` group
  (log out and back in after adding it).
- Tailscale installed, logged in and connected on the laptop **and** on the tablet, both in the same
  tailnet.
- `sudo` rights for the one-time host step (see [below](#host-settings-and-firewall-behaviour)).
- About 2 GB of free disk space for images and build cache, plus whatever the packages you add on the
  Dependencies page need (PyTorch with CUDA: several GB).
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
6. Copies `stack/` to the app dir and detects a usable NVIDIA GPU.
7. Builds the images (cached layers make repeated runs fast and do not recreate containers).
8. Reads the Tailscale name and checks the HTTPS certificate
   ([details](#connecting-by-name-over-https)), then writes the runtime `.env`.
9. Runs the [host step](#host-settings-and-firewall-behaviour) through `sudo` when it is needed — in a
   terminal before the containers start, so a new certificate is used right away.
10. Hashes the password with argon2 **inside the image** — only when it changed — writes the secrets
    and creates the package-runner token once.
11. Starts the containers and waits until all of them report healthy.
12. Installs the packages of the project's `requirements.txt`, if there is one
    ([three layers](#python-packages-three-layers)); unchanged, it is skipped.
13. Prints the URLs.

When the host step is needed and there is no terminal to ask for the `sudo` password, the script
prints the command to run instead, for example:

```text
ROOT_STEP_REQUIRED: host-setup 100.82.217.101 8888 8889 --cert basilisk-systems.lyrebird-hen.ts.net 1000
Run in a terminal: sudo /path/to/setup-jupyterlab-tailscale.sh host-setup 100.82.217.101 8888 8889 --cert basilisk-systems.lyrebird-hen.ts.net 1000
```

The containers are already running at that point, over HTTP. The host step makes them survive reboots
reliably, restricts the ports when a firewall is active and issues the HTTPS certificate; run
`./setup-jupyterlab-tailscale.sh start` afterwards to switch to HTTPS (the builder does that by
itself).

## Opening it on the tablet

With the tablet connected to the same tailnet, open the laptop by its Tailscale name (MagicDNS),
for example `basilisk-systems.lyrebird-hen.ts.net`:

| Page | URL | Login |
| --- | --- | --- |
| JupyterLab | `https://<machine>.<tailnet>.ts.net:8888/lab` | password only |
| Statistics | `https://<machine>.<tailnet>.ts.net:8889/` | username `jupyter` + the same password |
| Dependencies | `https://<machine>.<tailnet>.ts.net:8889/dependencies` | as Statistics |

`./setup-jupyterlab-tailscale.sh status` prints the exact URLs. They use HTTPS with a real certificate
when the tailnet allows it (see [Connecting by name over HTTPS](#connecting-by-name-over-https));
otherwise they fall back to `http://` by name, or to `http://<tailscale-ip>:8888/lab` when MagicDNS is
off. The dashboard's navigation bar links Statistics, Dependencies and JupyterLab, so only one address
has to be typed (or none, with the builder's Open buttons). Traffic between Tailscale devices is always
encrypted by the WireGuard tunnel; HTTPS adds a certificate the browser can verify.

## Connecting by name over HTTPS

Tailscale gives every device a MagicDNS name such as `basilisk-systems.lyrebird-hen.ts.net`, and it
can issue a real certificate for that name. The script uses both, the way zenobia does:

- **Detection.** With `HTTPS='auto'` (the default) every `install`, `update` and `start` reads
  `tailscale status --json` (parsed inside the JupyterLab image, so the host needs no Python or
  `jq`). When MagicDNS is on, all URLs use the name. When the tailnet can also issue a certificate
  for it, JupyterLab and the dashboard serve **HTTPS on the same ports**.
- **Certificate.** It comes from `tailscale cert`, which needs root, so it is part of the
  [host step](#host-settings-and-firewall-behaviour):
  `host-setup <tailscale-ip> <port> [<port>] --cert <name> <gid>`. The files are written to
  `/var/lib/jupyterlab-tailscale/tls/<name>.crt` and `.key`, owned by root and readable by your group,
  so the non-root containers can read them. In a terminal the step runs before the containers start,
  so the first install already uses HTTPS. From the builder, polkit asks for the password and the
  builder then deploys once more to switch to HTTPS.
- **Renewal.** Certificates last 90 days. When fewer than 21 days remain, the next `install`,
  `update` or `start` (or the builder's Deploy/Start) fetches a fresh one through the host step and
  recreates JupyterLab and the dashboard so they serve it. `status` and the dashboard footer show the
  expiry date, highlighted during the last 21 days.
- **Fallbacks.** With `HTTPS='off'`, with MagicDNS or HTTPS certificates disabled for the tailnet,
  or before the host step has run, the services use HTTP: by name when MagicDNS works, by IP otherwise.
  Nothing fails because of it. A stack deployed before this feature keeps HTTP until the next
  `update`.
- **Addresses.** The Tailscale IP keeps working, but under HTTPS the browser warns about the
  certificate, because it names the MagicDNS name and not the address.

Both prerequisites are switched on in the Tailscale admin console under **DNS**: *MagicDNS* and
*HTTPS Certificates*.

Inside the stack, `compose.tls.yaml` mounts the certificate read-only into `jupyterlab` and `stats`
whenever a valid one exists. The dashboard's own call to JupyterLab also uses HTTPS; it does not verify
the certificate, because the call stays on the private Compose network and the certificate cannot name
the service `jupyterlab`. The package runner stays internal plain HTTP. No HSTS header is sent, so the
HTTP fallback keeps working in browsers.

## Credentials and settings

The default password is:

```text
TailLab-7mK9-vQ2x-N4pR!
```

It is used for the JupyterLab login and, together with the fixed username `jupyter`, for the
statistics dashboard and the Dependencies page. `install` warns while the default is in use.

Settings live in `<repo>/.env`:

```bash
JUPYTER_PASSWORD='TailLab-7mK9-vQ2x-N4pR!'
JUPYTER_PORT='8888'
STATS_ENABLED='1'
STATS_PORT='8889'
STATS_USER='jupyter'
THEME='amazing'
HTTPS='auto'
NVIDIA='auto'
```

To change something, edit the file (or use the [builder](#graphical-builder)) and apply it:

```bash
./setup-jupyterlab-tailscale.sh update
```

- **Password:** 8–128 characters; no single quote, backslash, control characters or leading/trailing
  spaces. A new password is re-hashed and the containers are recreated, which logs every browser
  out. An unchanged password keeps its hash, so sessions survive redeploys.
- **Ports:** 1024–65535 and different from each other. A changed port recreates only the affected
  container.
- **`STATS_ENABLED='0'`** stops and removes the `stats` and `deps` containers (and, with an active
  firewall, closes the statistics port); `'1'` brings them back. Packages installed from the
  Dependencies page stay installed and keep working in notebooks.
- **`THEME`:** one of the files in `stack/theme/` without `.json` (default `amazing`); see
  [Themes](#themes). Changing it recreates only the `stats` container.
- **`HTTPS`:** `auto` (default) serves HTTPS by the Tailscale name when the tailnet allows it, `off`
  keeps plain HTTP; see [Connecting by name over HTTPS](#connecting-by-name-over-https).
- **`NVIDIA`:** `auto` (default) uses an NVIDIA GPU when Docker can hand it to the containers, `1`
  expects one (install and start stop when it is not usable), `0` never uses one; see
  [GPU support](#gpu-support).

The script parses this file itself; it is never executed as shell code. JupyterLab only ever sees
the argon2 hash. The plain password reaches the `stats` container as a mounted secret file, never
through environment variables, `compose.yaml` or `docker inspect`. The `deps` container never gets
the password at all — it accepts only the random token in `secrets/deps_token`, which the dashboard
uses to talk to it. JupyterLab's token login is disabled, and changing the password from the
JupyterLab UI is turned off.

## Graphical builder

`builder.py` is an optional PyQt6 window over the same script. It is a thin frontend: it edits
[`config.yaml`](#headless-runner-and-configyaml) (shared with `./run.sh`), writes the installer's
`.env` from it, and every action it takes is `setup-jupyterlab-tailscale.sh install | start | restart
| stop`, plus a read-only `docker compose ps` for the status badges. The command line keeps working
exactly as before, and all of them can be used side by side.

### Installing and launching

```bash
./run-builder.sh
```

- The first start creates `.venv` in the repository with `/usr/bin/python3` and installs the pinned
  wheels from `requirements-builder.txt` (PyQt6 6.11.0, PyQt6-Qt6 6.11.2, PyQt6-sip 13.12.0, and
  PyYAML 6.0.3 from `requirements-run.txt`; about 95 MB to download). Nothing is installed globally —
  `rm -rf .venv` removes every builder dependency. `run.sh` uses the same venv.
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
| Enable FastAPI statistics | `STATS_ENABLED`. Unticking it and deploying stops and removes the statistics and package-runner containers and closes the statistics firewall port. |
| Statistics port | `STATS_PORT`, default 8889 (disabled while statistics are off). |
| Username: jupyter | The fixed statistics username (`STATS_USER`), shown read-only. |
| NVIDIA GPU: Expect an NVIDIA GPU | `NVIDIA` ([GPU support](#gpu-support)). Ticked: the notebooks get the GPU, and Deploy and Start stop with the reason when it is not usable. Unticked: never use a GPU. Until it is clicked, `auto` is kept (the box shows whether `nvidia-smi` exists). An exported `JLT_GPU` is ignored here. |
| Services | A state dot and badge for JupyterLab and Statistics (Running, Starting, Unhealthy, Restarting, Stopped, Not deployed, Disabled), refreshed every 4 seconds, with the deployed URL and an **Open** button that starts the browser. |
| Page links | Under Statistics: **Dependencies**, **Stats API** and **Health** open those pages directly, so no address has to be typed. Like Open, they are enabled while the container runs and always use the deployed address and port. |
| Imported directories | Up to 7 host directories (path, **Browse…**, optional name) copied into the workspace on Deploy; see [Imported directories](#imported-directories). Only the filled rows and one empty row are shown; an empty row is an unused slot. The card lists what goes where and any problem (missing directory, same name twice, the workspace itself). |
| **Deploy / Update** | Validates, saves `config.yaml` and the installer's `.env`, copies the imported directories (`run.py import`, when any are set), then runs `install`: builds the images and starts the containers. Changed passwords or ports recreate only the affected containers. |
| **Start / Restart** | `start` when nothing is running, otherwise `restart`. |
| **Stop** | `stop` (`docker compose stop`). |
| Output | Read-only log of every command and its output. |

The header shows the detected Tailscale IPv4 address and MagicDNS name (from `tailscale status
--json`; Tailscale must be connected) and whether Docker answers. Buttons are disabled while a command runs; the window stays
responsive throughout. The package runner has no row of its own; its state is shown on the
Dependencies page.

Deploy is refused, with the reasons listed under the form, when:

- the password breaks the [password rules](#credentials-and-settings);
- a port is not a whole number from 1024 to 65535, or both ports are equal;
- a port is already in use on the Tailscale address by another program (ports held by this stack's own
  containers are fine; to swap ports between the two services, press Stop first);
- Tailscale is not connected or Docker does not answer.

### The host step from the builder

When the script reports that the [host step](#host-settings-and-firewall-behaviour) is needed, the
builder asks through polkit (`pkexec /bin/bash setup-jupyterlab-tailscale.sh host-setup <ip>
<ports> [--cert <name> <gid>]`), which shows the desktop's own password dialog. If the dialog is
dismissed, the services still run; the builder shows the equivalent `sudo` command, and it asks again
on the next Deploy, Start or Restart until the step has been applied once.

When the step included `--cert` and succeeded, the builder deploys once more by itself, so JupyterLab
and the dashboard switch to HTTPS and the Open buttons follow. It never loops: if a step is still
reported as needed afterwards, it shows a warning instead of asking again. If only the certificate
could not be issued, it says so; the services keep using HTTP by name.

### Configuration and security notes

- The builder reads `config.yaml` (until it exists: `<repo>/.env`) and on Deploy writes both, each
  atomically with mode `0600`. In `.env` it keeps comments and unknown lines. It honours
  `JLT_CONFIG_FILE`, `JLT_SETTINGS_FILE` and `JLT_APP_DIR`, and passes the configured `workspace` to
  the script. Settings the form does not show (`theme`, `https`, `stats.user`, `workspace`) are kept
  as they are in `config.yaml`; the window uses the theme's colours and heading font.
- A `config.yaml` that cannot be read as a whole (not YAML, not UTF-8) is never rewritten: the form
  shows the defaults and Deploy is refused until the file is fixed or deleted. A single wrong
  setting is reported and shown with its default.
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
| Edit fields + **Deploy / Update** | edit `config.yaml`, then `./run.sh install` (or `update`); without `config.yaml`: edit `.env`, then `./setup-jupyterlab-tailscale.sh install` |
| **Start / Restart** | `./run.sh start` / `restart` (or the script's `start` / `restart`) |
| **Stop** | `./run.sh stop` |
| Services badges | `./run.sh status` |
| **Open** and page links | the URLs printed by `status` |
| polkit dialog | `sudo ./setup-jupyterlab-tailscale.sh host-setup <tailscale-ip> <port> [<port>]` |

The test suite (the builder offscreen, the config and the headless runner without Qt) runs with
`QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests`.

## Headless runner and config.yaml

`./run.sh` deploys and runs the stack from `config.yaml` without a window — on a server, over SSH, or
from a script. It is the builder without the GUI: both use the Qt-free `thebe` package to load and
check `config.yaml` and to write the installer's `.env`, and both hand the real work to
`setup-jupyterlab-tailscale.sh`.

```bash
./run.sh                 # install: check config.yaml, write .env, build and start (the default)
./run.sh check           # check config.yaml and show what would be deployed; changes nothing
./run.sh start           # also: restart, stop, status, update, logs [...], uninstall [...]
./run.sh init            # create config.yaml (from .env, else the defaults) if it is missing
./run.sh import          # copy the import_dirs into the workspace now (install and update do it too)
./run.sh --config /path/other.yaml install
```

- The first start creates `.venv` (shared with the builder, [lib/venv.sh](lib/venv.sh)) and installs
  PyYAML from `requirements-run.txt`. No display, Qt or X11 library is needed.
- When `config.yaml` is missing, `install`, `update`, `start` and `restart` create it from the current
  `.env` (or the defaults) and carry on. `install` is idempotent, like the script's.
- `install`, `update`, `start` and `restart` refuse an invalid configuration with a list of problems
  (exit code 2) before anything is written. Otherwise they write `.env` and run the script's command
  in the terminal, so `sudo` can ask for the host step; the script's exit code is passed on.
- `stop`, `status`, `logs` and `uninstall` pass through unchanged, with the configured workspace.

`config.yaml` (template: [config.example.yaml](config.example.yaml)) holds the deployment settings:

```yaml
jupyter:
  password: "TailLab-7mK9-vQ2x-N4pR!"   # JUPYTER_PASSWORD
  port: 8888                            # JUPYTER_PORT
stats:
  enabled: true                         # STATS_ENABLED
  port: 8889                            # STATS_PORT
  user: "jupyter"                       # STATS_USER
theme: "amazing"                        # THEME
https: "auto"                           # HTTPS: auto | off
nvidia: auto                            # NVIDIA: true | false | auto
# workspace: "~/jupyter-workspace"      # JLT_WORKSPACE_DIR; relative paths start at config.yaml's directory
import_dirs:                            # copied into <workspace>/imported/ on install and update
  - "~/tools"
  - path: "/opt/lab/tools"
    name: "lab-tools"
```

- A missing setting uses its default. The rules are the script's ([Credentials and
  settings](#credentials-and-settings)); unknown keys are reported, so a typo does not go unnoticed.
- Text values are best quoted: unquoted YAML turns `12345678` into a number and `off` into false (the
  runner accepts `https: off`, but refuses a password that is not text).
- The file holds the password, so it is kept at mode `0600` (a wider mode is narrowed with a note).
- Location: `JLT_CONFIG_FILE`, else next to the settings file (`<repo>/config.yaml`). An exported
  `JLT_WORKSPACE_DIR` wins over `workspace`, as it does for the script.
- `config.yaml` is the source; `.env` is generated from it. If you use only the script, keep editing
  `.env` — but the builder and `run.sh` overwrite it from `config.yaml` on their next run.

## Imported directories

Up to seven host directories — scripts, tools, small datasets — can be **copied** into the notebook
workspace, so notebooks can use them at `/workspace/imported/<name>/`:

```yaml
import_dirs:
  - "~/tools/scripts"          # -> ~/jupyter-workspace/imported/scripts/
  - path: "/opt/lab/tools"     # -> ~/jupyter-workspace/imported/lab-tools/
    name: "lab-tools"
```

In the builder: the **Imported directories** card (path or **Browse…**, optional name). Headless:
`import_dirs` in `config.yaml`. Both use the same code (`thebe/imports.py`, run as `run.py import`).

- **When:** on `./run.sh install` / `update` and on Deploy in the builder, before the installer
  (re)starts JupyterLab. `./run.sh import` copies without deploying. `start` does not copy; the
  script used on its own (`setup-jupyterlab-tailscale.sh install`) does not read `config.yaml` and
  does not copy either.
- **A plain copy, nothing more.** No bind mount, no symbolic link, no extra path visible to any
  container: JupyterLab keeps seeing only `/workspace`. Changing the source later does not change the
  copy until the next Deploy.
- **Symbolic links are never followed or created.** Links inside a source (to files or directories)
  are skipped and listed in the deployment log; so are sockets, pipes and devices, and files that are
  not readable.
- **Checked first, all or nothing.** Every source must exist, be a real directory (not itself a
  symbolic link — select the directory it points to), be readable, and neither be, lie inside nor
  contain the workspace. Two sources with the same directory name need a `name` for one of them. Any
  problem refuses the whole deploy before anything is copied or written.
- **Redeploying updates the same copy.** Each copy records its source in `imported/<name>/.thebe-import`.
  Files that differ from the source (size or modification time) are replaced, each atomically;
  identical files are left alone, and nothing is deleted: files you add in JupyterLab stay, and files deleted on the host stay in the copy (delete
  them in JupyterLab). A copy is never duplicated as `<name> (1)`. A destination that belongs to
  another source, or that Thebe did not create, is refused rather than merged into.
- **Failures are contained.** A failed copy (disk full, I/O error) names the source and the file, stops
  the deploy before the installer runs, and touches nothing outside that copy; every file copied so
  far is complete, and the next Deploy continues where it stopped.
- Removing a directory from the list leaves its copy in the workspace.

A copied file edited in JupyterLab no longer matches its source, so the next Deploy puts the host's
version back. Keep work you want to keep outside `imported/` (new files inside it are kept).

## Commands

| Command | What it does |
| --- | --- |
| `install` | Build and start everything; creates the settings file when missing. Idempotent. |
| `update` | Same as `install`, but also pulls a newer base image (`docker compose build --pull`). Use it after editing `.env` or pulling a new version of this repository. |
| `start` | Start the containers. If the Tailscale IPv4, the MagicDNS name or the certificate changed, the services follow; a certificate due for renewal triggers the host step. |
| `stop` | Stop the containers (they stay stopped across reboots until `start`). A running package install is interrupted. |
| `restart` | `stop`, then `start`. |
| `status` | Settings, deployed ports, live vs. deployed Tailscale IP, the MagicDNS name and HTTPS state (certificate valid until / days left / renewal due / why HTTPS is not used), state and health of `jupyterlab`, `stats` and `deps (package runner)`, URLs, host step and firewall state. Exit code 3 when not installed. |
| `logs [--no-follow] [SERVICE...]` | Last 100 log lines of `jupyterlab`, `stats` and/or `deps`, following unless `--no-follow`. |
| `uninstall [--yes] [--delete-workspace]` | See [Uninstalling](#uninstalling). |
| `host-setup <ip> <port> [<port>] [--cert <name> <gid>]`, `host-teardown` | Root-only helpers, normally run for you through `sudo` (or polkit from the builder). |

Environment overrides: `JLT_SETTINGS_FILE` (settings file), `JLT_APP_DIR` (app dir),
`JLT_WORKSPACE_DIR` (notebook workspace), `JLT_GPU=auto|on|off` (overrides `NVIDIA` for one run).

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
| Copies of [imported directories](#imported-directories) | `~/jupyter-workspace/imported/<name>/` | yes (updated on Deploy) | no (only with `--delete-workspace`) |
| JupyterLab settings, workspace layouts, IPython history, login cookie secret | Docker volume `jupyterlab-tailscale_jupyter_state` (`/state`) | yes | yes |
| Custom packages: the venv, the page's requirements, the deployed copy of `requirements.txt` with the record of its last install, and the last job log | Docker volume `jupyterlab-tailscale_custom_packages` (`/opt/custom`, read-only in `jupyterlab`) | yes | yes |
| pip download cache of the package runner | Docker volume `jupyterlab-tailscale_pip_cache` | yes | yes |
| Settings | `<repo>/.env` | yes | no |
| Anything else inside a container (e.g. `%pip install` from a notebook) | container filesystem | **no** | yes |

Save everything you want to keep inside `/workspace` — that is the directory JupyterLab opens — and
add packages through the Dependencies page rather than with `pip` inside a notebook.

## Statistics dashboard

The `stats` service is a small FastAPI application styled after zenobia's *oya* design language
(theme "Amazing Moon", Orbitron headings, light and dark mode following the device, with a toggle
remembered by the browser). It works on phone, tablet and desktop widths and refreshes every
3 seconds. Its navigation bar links Statistics, Dependencies and JupyterLab.

| Route | Content |
| --- | --- |
| `/` | Dashboard: CPU (usage, per core, load, temperature), memory and swap, disk (the filesystem holding the workspace), uptime, network totals and rates (physical interfaces, with `tailscale0` shown separately), active Jupyter kernels (name, state, connections), NVIDIA GPU (utilisation, memory, temperature, power). |
| `/api/stats` | The same data as JSON. |
| `/health` | `{"status": "ok", "jupyter": "reachable"}` — used by the container health check. |
| `/dependencies` | The [Dependencies page](#dependencies-page). |
| `/theme.css` | The colour variables of the selected [theme](#themes). |

**Every** route, including `/health` and static files, requires HTTP Basic authentication (user
`jupyter`, the JupyterLab password). Host metrics come from the host's `/proc` and `/sys`, mounted
read-only; the Docker socket is not mounted. Kernel information comes from JupyterLab's REST API,
which the dashboard logs into with the same password (once, not on every refresh).

The page footer shows how the dashboard is reached: `HTTPS · certificate for <name> valid until <date>`
(highlighted during the last 21 days, a warning once expired) or `HTTP inside the Tailscale tunnel`.

## Dependencies page

`https://<machine>.<tailnet>.ts.net:8889/dependencies` (or the address `status` prints) adds Python packages to the notebook kernels — for example
PyTorch — without rebuilding images.

| Part of the page | What it does |
| --- | --- |
| Requirements | An editor for a pip requirements file (one requirement per line, `#` comments). **Save** stores it. |
| **Install / update** | Saves unsaved edits, creates the package environment on first use and runs `pip install` for the file. Packages already installed stay; changed version pins are applied. |
| **Reset & reinstall** | Deletes the package environment and installs the file from scratch — use it after removing lines. Asks for confirmation. |
| **Cancel** | Stops a running job. The environment keeps what was installed before. |
| **Clear download cache** | Frees the space used by pip's download cache. |
| Job, log, installed packages | Status of the last job (Succeeded, Failed, Cancelled, Interrupted), its live pip output, and the packages installed on top of the image. |
| Storage | Size of the installed packages, of the download cache, and the free disk space (with a warning when it gets low). |

**Restart the kernel** (*Kernel → Restart Kernel*) to use newly installed packages.

How it works:

- Installs run in the internal `deps` container, which uses the same image as JupyterLab. The packages
  go into a virtual environment in the `custom_packages` volume that sees the image's own packages.
- The image's packages (numpy, pandas, matplotlib, scipy, ipywidgets, jupyter…) keep their pinned
  versions: pip runs with the image's `pip freeze` as constraints, so they are not installed twice and
  cannot be replaced. A requirement that needs a different version of one of them fails with a
  resolution error instead.
- Kernels add the environment after the image's packages; the JupyterLab server itself never loads
  them, so a broken package cannot take JupyterLab down.
- One job runs at a time. A job cut off by a restart is shown as Interrupted.

Examples:

```text
# PyTorch for CPU only (about 200 MB). The index line applies to the whole file.
--index-url https://download.pytorch.org/whl/cpu
torch
```

```text
# PyTorch for CPU mixed with packages from PyPI: pin the +cpu build.
--extra-index-url https://download.pytorch.org/whl/cpu
torch==2.14.0+cpu
rich
```

```text
# PyTorch with CUDA from PyPI (about 3 GB). With GPU support enabled,
# torch.cuda.is_available() is True in notebooks.
torch
```

Lines are refused, with the line number and reason shown under the editor, when pip would misread
them or when they would reach outside the package environment:

- options that pip silently ignores next to a package, such as `torch --index-url …` (put the option
  on a line of its own);
- `-r`/`--requirement`, `-c`/`--constraint`, `-e`/`--editable`, `--target`, `--prefix`, `--root`,
  `--user`, `--src`;
- `${VARIABLES}`, encoding declarations, and control or bidirectional-text characters.

The page and the runner exist only while statistics are enabled. Installed packages stay in their
volume when statistics are switched off and notebooks keep using them.

The page also shows the project's `requirements.txt` read-only (**Project requirements**, with whether
it is installed); see [the next section](#python-packages-three-layers).

## Python packages: three layers

| Layer | Where it is declared | Installed | Changing it |
| --- | --- | --- | --- |
| **1. Base image** | `stack/jupyter/requirements.lock.txt` (JupyterLab, numpy, pandas, matplotlib, scipy, ipywidgets, …) | into the image, at build time | edit the lock and `update` (rebuilds the image) |
| **2. Project `requirements.txt`** | `<repo>/requirements.txt` (optional; `JLT_REQUIREMENTS_FILE` elsewhere) | into the custom packages environment on every `install` / `update`, Deploy in the builder and `./run.sh install` / `update` | edit the file and deploy again — no image rebuild |
| **3. Dependencies page** | the page's editor (stored in the `custom_packages` volume) | into the same environment, when you press **Install / update** | on the page |

Layers 2 and 3 are one environment — the virtualenv in the `custom_packages` volume that the
Dependencies page manages — installed by one pip job in the `deps` runner, with the same rules:

```text
# <repo>/requirements.txt: a normal pip requirements file (validated like the page's editor)
opencv-python
pytesseract
requests
--extra-index-url https://download.pytorch.org/whl/cpu
torch==2.14.0+cpu
```

- **The file is the source of truth.** It is not copied into `config.yaml` or `.env`; each deploy hands
  it to the runner, which keeps the last deployed copy next to the environment (shown on the page).
  No file, or one without packages, installs nothing extra.
- **Checked first.** `./run.sh` and the builder refuse a file the page would refuse (with line numbers),
  and the script checks it again with the image's runner right after the build, before any container
  is recreated. `-r`, `-c`, `-e`, `--target`, `--prefix`, `--root`, `--user`, `--src`, `${VARS}` and the
  other refused lines are listed [above](#dependencies-page).
- **Installed before "ready".** After the containers are healthy and before the script prints its
  summary, the runner installs the file together with the page's list, in one `pip install`, so the two
  can never disagree. With statistics on, the running runner does it and the page shows the job live;
  with statistics off, a one-off runner container does.
- **Option lines apply to both lists.** Because they are one `pip install`, an `--index-url`,
  `--extra-index-url`, `--no-index` or `--find-links` line in either file applies to every package in
  both; prefer `--extra-index-url` (as in the torch example) over replacing the index.
- **Skipped when nothing changed.** The runner records what each job installed. A deploy with the same
  file, the same image and a finished last install checks with pip (offline, dry run) that nothing is
  missing and then skips the install. A changed file, a rebuilt image with other pins, a failed or
  interrupted install, or packages missing from the volume install again.
- **The image's packages stay.** pip runs with the image's `pip freeze` as constraints, so a line that
  needs another version of JupyterLab, numpy, pandas or any other image package fails (the deploy
  names the conflicting requirement) instead of replacing it.
- **Failures keep the working environment.** pip resolves, downloads and builds everything before it
  changes the environment, so an unknown package, a conflict or a build error leaves the packages
  installed before, and the deploy stops with pip's error lines. Nothing is ever deleted
  automatically. The full log stays on the Dependencies page.
- **Downloads are reused.** Every job uses pip's cache in the `pip_cache` volume; large wheels such as
  torch are downloaded once. The deploy prints the size of the environment, of the cache and the free
  disk.
- **Persistent.** The environment, the deployed copy of the file and the record of the last install live
  in volumes and survive `update` and container recreation; `uninstall` removes them.
- **Removing lines does not uninstall.** Packages dropped from the file (or the page) stay until **Reset &
  reinstall** on the page, which rebuilds the environment from both lists.
- Kernels see new packages after a restart (*Kernel → Restart Kernel*).

## Themes

The dashboard, the Dependencies page and the builder window share one palette, taken from
zenobia's theme files. They are copied verbatim into `stack/theme/` (same format, same colour keys);
no zenobia or toto code or package is imported.

| `THEME` | Theme | Heading font |
| --- | --- | --- |
| `amazing` (default) | Amazing Moon | Orbitron |
| `bitter` | Bitter Orange | Orbitron |
| `market` | Market Appetite | Nunito Sans |
| `spectre` | Elegant Spectrum | Orbitron |

To switch, set `THEME='market'` (for example) in `<repo>/.env` and run
`./setup-jupyterlab-tailscale.sh update`. Only the `stats` container is recreated; nothing is
rebuilt and running kernels are not affected. Restart the builder to recolour its window.

How it works:

- At start-up the stats app reads `theme/<THEME>.json` and serves `/theme.css`: the light and dark
  colour variables (behind the same Basic auth as every other route). `oya.css` only uses those
  variables, so every page follows the theme; light/dark still follows the device or the toggle.
- The builder maps the same colours to its Qt palette with the same rules: cards bordered with
  `accent-2` in light mode and `accent-1` in dark mode, and a fallback for themes without `caution`
  colours.
- In light mode, small accent text and the text on accent buttons use whichever of two theme
  colours reads better (accent or link, background or text). With the orange `bitter` and `market`
  themes they therefore differ slightly from zenobia's raw accent.
- Orbitron ships with the dashboard (SIL Open Font Licence). Any other heading font is used only
  when it is installed on the viewing device; nothing is downloaded.

Adding a theme: put a zenobia theme file at `stack/theme/<name>.json` (a regular UTF-8 file without
a byte-order mark; the name made of letters, digits, `_` or `-`; every colour `#rgb` or `#rrggbb`;
all colour keys present, `caution-*` optional), set `THEME='<name>'` and run `update`, which
rebuilds the stats image to include it. The script refuses an unknown name and lists the available
ones. A file that cannot be used never stops anything: the dashboard and the builder fall back to
Amazing Moon, and the dashboard logs one warning (`./setup-jupyterlab-tailscale.sh logs --no-follow
stats`).

## GPU support

The `NVIDIA` setting (`nvidia:` in `config.yaml`, the **NVIDIA GPU** checkbox in the builder) says
whether to expect an NVIDIA GPU:

| Value | install / update | start |
| --- | --- | --- |
| `auto` (default) | GPU access when `nvidia-smi -L` works on the host and Docker has the NVIDIA runtime or a CDI specification, verified with a test container; otherwise (or when the test fails) without GPU, with a warning. | Checks the GPU with a test container first; when that fails, starts without the GPU this time (warning), and checks again on the next start. |
| `1` / `true` / ticked | Expects the GPU: stops with the reason when the test container cannot use it. | Stops with the reason when the test container cannot use it. |
| `0` / `false` / unticked | Never uses a GPU and never checks — for machines without NVIDIA, or while the driver or container toolkit is broken. | Same. |

When enabled, `compose.gpu.yaml` gives `jupyterlab` and `stats` `gpus: all`:

- `jupyterlab` gets the `compute,utility` driver capabilities, so CUDA libraries installed from the
  Dependencies page (e.g. PyTorch with CUDA) can use the GPU.
- `stats` gets `utility` only, which is enough for the GPU statistics.
- `deps` gets no GPU; installing packages does not need one.

For one run of the script, `JLT_GPU=on|off|auto ./setup-jupyterlab-tailscale.sh update` overrides the
setting (the builder and `run.sh` ignore an exported `JLT_GPU`: their setting decides). `status`
shows the deployed mode.

### Verifying PyTorch on the GPU

`verify_cuda.sh` checks the whole path a notebook uses, from inside the running stack:

```bash
./verify_cuda.sh                        # GPU on the host and in the container, libcuda from a real kernel,
                                        # and PyTorch on the GPU if it is already installed
./verify_cuda.sh --install              # also install PyTorch with CUDA (about 3 GB) from the Dependencies page
./verify_cuda.sh --install --cleanup    # install, verify, then restore the previous package list
```

It starts a kernel through the same `python3` kernelspec as notebooks, prints
`torch.cuda.is_available()`, the device name and a GPU matrix multiply compared with the CPU, and
exits with 0 when PyTorch uses the GPU, 1 when a check fails, 3 when the stack is not running, and 4
when the GPU works but PyTorch is not installed. Installing asks for confirmation (or `--yes`),
refuses with less than 8 GB free (`MIN_FREE_GB`), and cancels the install on Ctrl+C. The requirement
lines can be changed with `TORCH_REQUIREMENTS`. Calls to the Dependencies page run inside the stats
container, so the password is never handled by the script.

## Security model

- **Reachability.** JupyterLab and the dashboard are published only on the Tailscale IPv4 address;
  the package runner is not published at all. Compose refuses to start if that address is missing, so
  an empty value can never turn into "all interfaces". Nothing listens on loopback, the
  Wi-Fi/Ethernet address or `0.0.0.0`.
- **Transport.** Tailscale encrypts traffic between devices (WireGuard). When the tailnet allows it,
  JupyterLab and the dashboard also serve HTTPS with a publicly trusted certificate for the MagicDNS
  name, so browsers can verify the server; otherwise they speak HTTP inside the tunnel. The
  certificate's private key is readable only by root and your primary group. The dashboard's call to
  JupyterLab on the internal Compose network does not verify the certificate (it names the public
  host), and no HSTS header is sent, so the HTTP fallback keeps working.
- **Authentication.** JupyterLab requires the password (argon2 hash, token login disabled, the server
  refuses to start without a valid hash). The dashboard requires HTTP Basic auth on every route. The
  package runner accepts only a random token shared with the dashboard.
- **Cross-site requests.** Browsers resend Basic credentials automatically, so every request that
  changes something on the Dependencies page must be a same-origin JSON request carrying an
  `X-Requested-With` header; other sites cannot trigger installs. (A consequence: an HTTPS proxy put in
  front of the dashboard under a different address would have its write requests refused.)
- **Tailnet members.** Every device in your tailnet that can reach the laptop can reach the login
  pages. To restrict that, add a Tailscale ACL that allows TCP 8888/8889 on the laptop only from
  the tablet.
- **Containers.** Non-root user, all capabilities dropped, `no-new-privileges`, no Docker socket,
  host `/proc` and `/sys` read-only and only in `stats`, secrets as `0600` files.
- **Packages.** pip, and the build scripts of the packages it installs, run in `deps`: no host mounts,
  no workspace, no published port, read-only root filesystem, and no access to the password.
  Installed packages later run inside notebook kernels with the kernels' rights, so install only
  packages you trust.
- **Notebooks run arbitrary code** as the container user, with internet access and write access to
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
5. **HTTPS certificate (with `--cert <name> <gid>`).** Runs
   `tailscale cert --cert-file … --key-file … --min-validity 528h <name>` (always the full MagicDNS name,
   never writing into the current directory), then installs the pair as
   `/var/lib/jupyterlab-tailscale/tls/<name>.crt` (0644) and `<name>.key` (0640), both `root:<gid>`, in a
   `0750` directory, and removes certificates for other names. The group is your primary group, so the
   non-root containers can read the key; `host-setup` warns if that group is shared with other accounts.
   If `tailscale cert` fails (for example because HTTPS certificates are disabled for the tailnet), the
   sysctl and firewall parts are still applied, the previous certificate is kept, and the helper exits
   with an error saying that no certificate was issued.

Ports that are no longer wanted (a changed port, statistics disabled) are removed from the rules on
the next run. The applied state is recorded in `/var/lib/jupyterlab-tailscale/state`.

## Updating

- **Settings changes:** edit `<repo>/.env`, then `./setup-jupyterlab-tailscale.sh update`.
- **New base image (security fixes in Debian/Python):** `./setup-jupyterlab-tailscale.sh update`.
- **New package versions in the images:** edit `stack/jupyter/requirements.txt` or
  `stack/stats/requirements.txt`, regenerate the matching `requirements.lock.txt` with the command in
  that file's header, then run `update`. Afterwards run **Install / update** on the Dependencies page,
  so the added packages are resolved against the new image.
- **New version of this tool:** update the repository (e.g. `git pull`), then `update`.

Containers are recreated only when their configuration or image actually changed. Recreating
JupyterLab stops running kernels; notebooks, settings and installed packages are kept.

## Troubleshooting

**The tablet cannot connect.** Check `tailscale status` on both devices, `tailscale ping
<laptop>` from another device, and `./setup-jupyterlab-tailscale.sh status` on the laptop (all
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
jupyterlab` (or `stats`, `deps`). A JupyterLab container that exits immediately with a message about
the password hash means the secret is missing or damaged; run `update`.

**The Dependencies page says the package runner is unavailable.** Check `status` (the
`deps (package runner)` row) and `logs --no-follow deps`; `restart` usually fixes it. If statistics
are disabled, the page does not exist.

**An installed package cannot be imported in a notebook.** Restart the kernel. If the job failed,
the log on the page shows why — a `ResolutionImpossible` error means the package needs a different
version of something the image already pins.

**The browser warns about the certificate.** Open the pages by name
(`https://<machine>.<tailnet>.ts.net:8888/lab`), not by IP: the certificate names the MagicDNS name.

**HTTPS is not used although `HTTPS='auto'`.** The `Name:` and `HTTPS:` lines of `status` say why:
MagicDNS or HTTPS Certificates are disabled in the Tailscale admin console (DNS page), the host step has
not been applied yet (`Root step needed: yes -> sudo …`), or the stack was deployed before this feature
(run `update` once). If `host-setup` ends by saying that no certificate was issued, `tailscale cert`
failed; check the admin console and `tailscale status`.

**The certificate is about to expire.** Run `start` or `update` (or Deploy/Start in the builder): with
fewer than 21 days left the host step renews it, and JupyterLab and the dashboard are recreated with
the new one.

**The dashboard shows "No NVIDIA GPU visible".** Check `nvidia-smi` on the host and
`docker info | grep -i runtime`; then `JLT_GPU=on ./setup-jupyterlab-tailscale.sh update` to see
why the GPU test fails.

**Start fails with `failed to fulfil mount request: open /run/nvidia-persistenced/socket: no such file or
directory`.** The NVIDIA container toolkit (its CDI specification) mounts the socket of
`nvidia-persistenced`, and that daemon is not running — typically after a reboot or a driver update.
Start it and keep it on at boot: `sudo systemctl enable --now nvidia-persistenced`. Or regenerate the
CDI specification without it: `sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml` (the
script names the file it found). With `NVIDIA='auto'`, `start` notices this with its test container
and starts without the GPU meanwhile; to run without NVIDIA for good, untick **NVIDIA GPU** in the
builder, set `nvidia: false` in `config.yaml` or `NVIDIA='0'` in `.env`, and deploy again.

**A deploy stops with "The packages from …/requirements.txt were not installed".** pip's error lines
above name the requirement: a package or version that does not exist, a conflict with a package the
image pins (e.g. `numpy==1.26` while the image has 2.5.3), or a build that failed. JupyterLab keeps
running with the packages installed before. Fix the file and run `update` (or Deploy) again; the
Dependencies page shows the full log.

**"No space left on device".** Use **Clear download cache** on the Dependencies page, or **Reset &
reinstall** with fewer packages; `docker builder prune` removes Docker's build cache (for all
projects).

## Uninstalling

```bash
./setup-jupyterlab-tailscale.sh uninstall
```

You must type `uninstall` to confirm (or pass `--yes`; without a terminal, `--yes` is required). It
removes the containers, both images, the Docker volumes (JupyterLab settings, installed packages and
caches), the app dir, and — through `sudo` — the host step (sysctl file, firewall rules, HTTPS certificate, state).

It **keeps** `~/jupyter-workspace` and `<repo>/.env`. To delete the workspace as well, add
`--delete-workspace` and type `delete` when asked.

## Repository layout

```text
setup-jupyterlab-tailscale.sh      the CLI (install, start, stop, restart, status, logs, update, uninstall)
stack/Dockerfile                   base → jupyterlab and stats images
stack/compose.yaml                 services, ports, secrets, volumes, health checks
stack/compose.gpu.yaml             GPU override, used when a GPU is detected
stack/compose.tls.yaml             HTTPS override, used when a valid certificate exists
stack/jupyter/                     JupyterLab config and health check, kernel launcher, package runner, requirements and lock file
stack/stats/                       FastAPI dashboard and Dependencies page: app, TLS-aware launcher (serve.py), theme loader, templates, static assets, requirements and lock file
stack/theme/                       zenobia's oya theme files (palette of the dashboard and the builder)
builder.py                         optional PyQt6 builder (thin frontend over the script)
run-builder.sh                     creates .venv with the pinned PyQt6 and starts the builder
run.py, run.sh                     headless runner: deploy and run from config.yaml without the GUI
thebe/                             Qt-free core shared by both: settings and validation, config.yaml,
                                   stack state (Tailscale, compose ps), theme tokens, the runner's CLI,
                                   imports.py (copying host directories into the workspace),
                                   packages.py (checking requirements.txt with the package runner's rules)
requirements.txt                   optional: your Jupyter packages, installed on install/update (not in the repository)
lib/venv.sh                        the project-local .venv bootstrap used by run.sh and run-builder.sh
requirements-run.txt               PyYAML pin (run.sh and the builder)
requirements-builder.txt           PyQt6 pins for the builder venv (includes requirements-run.txt)
config.example.yaml                the default config.yaml, with explanations
tests/                             offscreen builder tests, config and headless runner tests
verify_cuda.sh                     end-to-end check that notebooks can use the GPU with PyTorch
plan.md                            implementation plan and progress
config.yaml                        your configuration (created by run.sh or the builder, not committed)
.env                               the installer's settings (written from config.yaml, not committed)
.venv/                             virtualenv of run.sh and the builder (not committed)
```
