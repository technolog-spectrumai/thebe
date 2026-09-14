# JupyterLab over Tailscale — Docker rewrite plan

Status: in progress, 2026-09-14. Stages are delivered in order; each ends with the
validation listed in §7 and a commit.

## Progress

A box is ticked only after the item has been validated (in a container or on the host), not
merely written.

**Groundwork**
- [x] Research: zenobia builder framework, oya design tokens, Docker/Compose/Jupyter behaviour, package pins
- [x] Decisions recorded (§1, §8)

**Stage 1 — Docker stack**
- [x] `stack/Dockerfile` (base → `jupyterlab`, `stats`), exact pins + lock files, non-root uid 1000
- [x] `stack/compose.yaml`, `stack/compose.gpu.yaml` (`docker compose config` incl. TS_IP / WORKSPACE_DIR guards)
- [x] `stack/jupyter/jupyter_server_config.py`: hashed password, token off, fail-fast on a bad secret; login, kernels, GPU, internet, uid-1000 files verified in a container
- [x] Image hooks for Stage 3: kernelspec `PYTHONPATH`/`PATH`, `/opt/constraints.txt`, mount points
- [x] `stack/stats/`: FastAPI dashboard (Basic auth on every route, `/`, `/api/stats`, `/health`, oya UI; screenshots at 390/768/1280 px, light and dark)
- [x] `setup-jupyterlab-tailscale.sh` rewrite (install/start/stop/restart/status/logs/update/uninstall, root helpers) + `.gitignore` — shellcheck clean, 107 unit + 45 root-helper checks (ufw in a container)
- [x] Cross-file integration check + loopback sandbox run of the real compose project (fixed: rebuilds no longer change image IDs / recreate containers)
- [x] Real install on the Tailscale IP (legacy venv and config dir removed; both containers healthy on 100.82.217.101 only)
- [ ] Root step applied on this laptop (`sudo ./setup-jupyterlab-tailscale.sh host-setup 100.82.217.101 8888 8889` — needs your terminal)
- [x] Validation: `bash -n`, `docker compose config`, `docker compose build`, curl/security checks, idempotent re-install, 49-check lifecycle run (stop/start/restart/logs, password and port change, invalid settings, stats off/on, update, uninstall + reinstall)
- [x] README (Stage 1 sections)
- [x] Commit

**Stage 2 — PyQt6 builder**
- [ ] `builder.py`, `run-builder.sh`, `requirements-builder.txt`, `tests/test_builder.py`
- [ ] Adversarial review against the spec + fixes, offscreen tests green
- [ ] Real Deploy → Stop → Start round trip against the stack
- [ ] README (builder section)
- [ ] Commit

**Stage 3 — Dependencies page**
- [ ] `/dependencies` page + API: pip into the shared volume with constraints, single job, live log
- [ ] Notebook imports a custom package after kernel restart; `torch.cuda.is_available()` on this laptop
- [ ] README + commit

**Stage 4 — Ops links**
- [ ] Builder "Pages" card with Open buttons; dashboard navigation for every page
- [ ] README + commit

**Stage 5 — Harmonization with zenobia (no imports)**
- [x] zenobia theme files copied to `stack/theme/{amazing,bitter,market,spectre}.json`
- [ ] `THEME` setting validated by the script and the builder
- [ ] Stats app generates `theme.css` from the theme JSON
- [ ] Builder QSS generated from the same JSON
- [ ] README + commit

## 0. Goal

Turn `setup-jupyterlab-tailscale.sh` from "host venv + systemd user service" into a
self-contained Docker Compose stack: JupyterLab plus a small FastAPI dashboard, both
published **only on the laptop's Tailscale IPv4**, with the oya look of zenobia. Host
dependencies: Docker Engine, Docker Compose v2, Tailscale. Nothing is installed on the
host with pip. A later PyQt6 builder (Stage 2) is a thin GUI over the same script.

Non-goals: HTTPS (Tailscale encrypts the tunnel), multi-user, exposing anything on
`0.0.0.0`, reusing the zenobia/Django stack (see §6).

## 1. Decisions

Confirmed with the user:

| Topic | Decision |
|---|---|
| Settings file | `.env` **next to the script in the repo** (gitignored, mode 0600). Read by the script and the builder. |
| Notebook image | jupyterlab + ipykernel + numpy, pandas, matplotlib, scipy, ipywidgets — all pinned. |
| GPU | When an NVIDIA GPU + container toolkit are detected, **both** containers get `gpus: all`. |
| `/health` | Requires Basic auth like every other route; the Docker healthcheck sends the header. |
| Stats auth | HTTP Basic, fixed username `jupyter`, the same password as JupyterLab. The builder shows the username read-only. |
| Design | Dashboard and builder use zenobia's oya design language (theme "Amazing Moon", font Orbitron). |
| Internet | Containers keep default bridge networking → outbound internet for notebooks and pip. Verified in validation, nothing to configure. |

Proposed (see §8 for the questions still open):

| Topic | Proposal | Why |
|---|---|---|
| Base image | `python:3.13-slim-trixie` for both images, one multi-stage Dockerfile, targets `jupyterlab` and `stats`. | Every pinned package has cp313 manylinux wheels → no compiler in the image. |
| Boot race | `install` writes `/etc/sysctl.d/60-jupyterlab-tailscale.conf` → `net.ipv4.ip_nonlocal_bind = 1` (one `sudo`). | Docker binds `<ts-ip>:8888` in the daemon; if tailscaled is late at boot the bind fails with `cannot assign requested address` and **Docker never retries** (`daemon/daemon.go:665`; docs: restart policies only apply after a successful start). A `docker.service After=tailscaled` drop-in does not help (tailscaled reports ready before the IP exists, tailscale#11504). The sysctl makes the bind succeed regardless of timing. |
| Firewall | Rules are applied only when a firewall is **active**. ufw: `allow in on tailscale0 to any port <p> proto tcp comment 'jupyterlab-tailscale'` per port **plus** a `DOCKER-USER` block in `/etc/ufw/after.rules` (between markers) that drops traffic to `<ts-ip>:<port>` arriving on any interface other than `tailscale0`. firewalld: port in the zone of `tailscale0` (as today; untestable here). | Docker-published ports take the DNAT→FORWARD path and bypass ufw INPUT rules entirely (docs.docker.com/engine/network/packet-filtering-firewalls). The `DOCKER-USER` rule is the only one that actually restricts the port to the tailnet interface. On this laptop ufw is installed but **disabled** (`/etc/ufw/ufw.conf: ENABLED=no`), so today nothing is configured and `status` says so explicitly instead of "no rule needed". |
| Secrets | Password → `$APP_DIR/secrets/jupyter_password` (0600); argon2 hash → `$APP_DIR/secrets/jupyter_hashed_password` (0600). Both are Compose `secrets:` (file) mounts. | The hash contains `$…$`; Compose interpolation would mangle it. Secrets never appear in `compose.yaml`, in `docker inspect`, or in the environment. |
| Hash generation | Inside the built image: `docker run --rm -i <image> python -c '…passwd(sys.stdin.read())'`. Re-hash **only when the password changed** (`passwd_check` against the stored hash). | No host Python. Re-hashing the same password changes Jupyter's cookie secret and logs every browser out. |
| Container user | Both images create user `jupyter` with the host's uid/gid (build args). | The workspace bind mount must stay writable by the desktop user. |
| Stage 3 packages | pip installs run in the **stats** container into a shared named volume (`custom_packages` → `/opt/custom/site-packages`), constrained by a `constraints.txt` frozen from the JupyterLab image; kernels see it through `PYTHONPATH`/`PATH` in the `python3` kernelspec. | No Docker socket, no image rebuild per package, the Jupyter server itself is never shadowed by a user package. |

## 2. Stage 1 — Docker stack

### 2.1 Repository layout

```
setup-jupyterlab-tailscale.sh     CLI: install|start|stop|restart|status|logs|update|uninstall (+ root helpers)
stack/Dockerfile                  multi-stage: base → jupyterlab, stats
stack/compose.yaml                two services, secrets, volumes
stack/compose.gpu.yaml            override adding `gpus: all` (used only when a GPU is detected)
stack/jupyter/requirements.txt    pinned notebook stack
stack/jupyter/jupyter_server_config.py
stack/stats/requirements.txt      pinned dashboard stack
stack/stats/app.py                FastAPI app
stack/stats/templates/*.html      oya-styled pages (inline CSS/JS, no CDN)
.env                              settings (gitignored, 0600) — created on first install
.gitignore                        .env, .venv/, __pycache__/
README.md, plan.md
```

`install`/`update` copy `stack/` into `~/.local/share/jupyterlab-tailscale/` (the app dir),
so the running stack is a snapshot; editing the repo changes nothing until `update`.

### 2.2 App dir (`~/.local/share/jupyterlab-tailscale/`)

```
.env               generated runtime variables, no secrets (COMPOSE_PROJECT_NAME, COMPOSE_FILE,
                   COMPOSE_PROFILES, TS_IP, JUPYTER_PORT, STATS_PORT, STATS_USER, WORKSPACE_DIR, JLT_UID, JLT_GID)
compose.yaml, compose.gpu.yaml, Dockerfile, jupyter/, stats/     copied from stack/
secrets/jupyter_password, secrets/jupyter_hashed_password        0600
```

`docker compose config` / `build` / `ps` work from this directory without arguments.
The old venv at `~/.local/share/jupyterlab-tailscale/venv` and the empty
`~/.config/jupyterlab-tailscale/` are removed by `install` (leftovers of the previous installer).

### 2.3 Settings `.env` (repo)

```
JUPYTER_PASSWORD='TailLab-7mK9-vQ2x-N4pR!'
JUPYTER_PORT=8888
STATS_ENABLED=1
STATS_PORT=8889
STATS_USER=jupyter
```

Written by the script (defaults on first install) and by the builder. Values are
single-quoted literals; the password may not contain `'`, `\` or line breaks. The script
parses `KEY=VALUE` lines itself (never `source`).

### 2.4 Containers

| | `jupyterlab` | `stats` |
|---|---|---|
| Image | `jupyterlab-tailscale/jupyterlab:local`, `pull_policy: never` | `jupyterlab-tailscale/stats:local`, `pull_policy: never` |
| Publish | `${TS_IP:?}:${JUPYTER_PORT:?}:8888` | `${TS_IP:?}:${STATS_PORT:?}:8889`, `profiles: [stats]` |
| Volumes | `~/jupyter-workspace:/workspace` (bind, `create_host_path: false`), `jupyter_state:/state`, `custom_packages:/opt/custom` | `/proc:/host/proc:ro`, `/sys:/host/sys:ro`, workspace `:ro` (disk usage), `custom_packages:/opt/custom`, `pip_cache:/var/cache/pip` |
| Secrets | `jupyter_hashed_password` | `jupyter_password` |
| Healthcheck | `python -c` GET `http://127.0.0.1:8888/api` (unauthenticated by design in jupyter_server) | `python -c` GET `/health` with Basic auth read from the secret |
| Hardening | `init: true`, `security_opt: no-new-privileges`, `cap_drop: [ALL]` | same + `read_only: true`, `tmpfs /tmp` |
| Other | `restart: unless-stopped`, json-file logs 10m×3, `stop_grace_period: 30s`, GPU via override | `depends_on: jupyterlab`, no Docker socket |

Pins (all latest on PyPI today, all with cp313 wheels): jupyterlab 4.6.3, jupyter_server
2.21.0, jupyterlab-server 2.28.0, ipykernel 7.3.0, ipywidgets 8.1.9, numpy 2.5.3, pandas
3.0.5, matplotlib 3.11.2, scipy 1.18.1, argon2-cffi 25.1.0; fastapi 0.141.1, starlette
1.6.0, uvicorn 0.53.0, pydantic 2.13.5, nvidia-ml-py 13.610.43. A full `pip freeze` lock
is generated once during implementation and installed with `--only-binary=:all:`.

### 2.5 JupyterLab configuration (`jupyter_server_config.py`)

`ip=0.0.0.0` inside the container (the Tailscale restriction is the port publish),
`port_retries=0`, `allow_remote_access=True`, `root_dir=/workspace`,
`PasswordIdentityProvider.hashed_password` read from `/run/secrets/jupyter_hashed_password`,
`password_required=True` (a missing secret crash-loops instead of running open),
`allow_password_change=False`, `IdentityProvider.token=""`, `cookie_secret_file=/state/jupyter_cookie_secret`
(survives recreation), `terminals_enabled=True`, `LabApp.news_url=None`,
`check_for_updates_class=NeverCheckForUpdate`, `extension_manager="readonly"`.
Environment pins `HOME`, `JUPYTER_CONFIG_DIR`, `JUPYTER_DATA_DIR`, `JUPYTERLAB_SETTINGS_DIR`,
`JUPYTERLAB_WORKSPACES_DIR`, `IPYTHONDIR` under `/state` so lab settings persist.

### 2.6 Stats app (FastAPI, `stats/app.py`)

Routes (all behind Basic auth, `secrets.compare_digest`, realm `jupyterlab-tailscale`):

| Route | Content |
|---|---|
| `GET /` | Statistics page: responsive oya layout, tiles refreshed every 3 s from `/api/stats`, dark/light toggle (`localStorage.darkMode`, same key as oya), links to JupyterLab and the other pages. |
| `GET /api/stats` | JSON: `cpu` (% from two `/host/proc/stat` samples, cores, load), `memory` and `swap` (`/host/proc/meminfo`), `disk` (statvfs of the workspace filesystem), `uptime` (`/host/proc/uptime`), `network` (totals from `/host/proc/1/net/dev`, per-interface, tailscale0 highlighted), `kernels` (count + list from Jupyter `/api/kernels`, via a cookie login that is cached and renewed on 403), `gpu` (pynvml: name, utilisation, memory, temperature, power; `null` with a reason when NVML is unavailable), `cpu_temperature` (hwmon under `/host/sys`, optional). |
| `GET /health` | `{"status":"ok","jupyter":"reachable|unreachable"}`. |

Host `/proc` is mounted at `/host/proc` and parsed with the standard library (psutil is
not used: its `/sys` paths are hard-coded and `/proc/net/dev` is per-namespace).

Design: tokens verbatim from `zenobia/data/themes/amazing.json` as CSS custom
properties (light + dark), cards `rounded-2xl` on `bubble-bg` with `accent-2/accent-1`
borders, eyebrow labels `text-xs semibold uppercase tracking-widest` in accent, stat tiles,
meter bars, status pills, primary button `bg-accent text-primary-bg`. Orbitron (OFL) is
embedded as a ~16 KB base64 woff2 for headings and numerals; body text uses the system
sans stack. No Tailwind runtime, no Alpine, no icon font.

### 2.7 Script commands

| Command | Behaviour |
|---|---|
| `install` | Refuses root. Checks docker, `docker compose` v2, tailscaled running, IPv4 in 100.64.0.0/10. Creates `.env` with defaults if missing. Creates `~/jupyter-workspace` as the user. Syncs `stack/` → app dir, writes runtime `.env`, detects GPU (`nvidia-smi -L` + docker `nvidia` runtime or CDI spec) and sets `COMPOSE_FILE` accordingly. `docker compose build`. Writes/keeps secrets (hash only when the password changed). `docker compose up -d --remove-orphans` (+ `--force-recreate jupyterlab stats` when the hash changed). If stats is disabled: `docker compose rm -s -f stats`. Waits for health. Runs the root step (§2.8) via `sudo`. Prints URLs, password, workspace, next commands. Removes the old venv/config leftovers. |
| `start` | Waits up to 60 s for a Tailscale IPv4; if it differs from the stored `TS_IP`, rewrites the runtime `.env` (ports re-bind → Compose recreates). `docker compose up -d --remove-orphans`. |
| `stop` | `docker compose stop`. |
| `restart` | `stop` then `start` (so an IP change is picked up; a plain `compose restart` would not). |
| `status` | Live vs configured Tailscale IP, `docker compose ps -a` (state + health per service), URLs, GPU state, `ip_nonlocal_bind` value, which firewall is active and whether rules are present, and a hint when a container failed with a bind error. |
| `logs` | `docker compose logs -f --tail=100 [service]`. |
| `update` | Same as `install` minus first-time steps, with `--pull` on build (base image refresh). Applies password/port/stats changes from `.env`; prints the root command when firewall ports changed. |
| `uninstall` | Requires typing `uninstall` (or `--yes`; without a TTY `--yes` is mandatory). `docker compose --profile stats down --remove-orphans --volumes --rmi all` (`--rmi local` would keep the custom-tagged images), root teardown (firewall rules, sysctl file, state), deletes the app dir. **Keeps `~/jupyter-workspace`** (removed only with `--delete-workspace`) and the settings `.env`. |
| `host-setup <ts-ip> <port>…` / `host-teardown` | Root-only helpers called by `install`/`uninstall` through `sudo` and by the builder through `pkexec`. Take everything as arguments (no reliance on the caller's environment). Idempotent; a state file under `/var/lib/jupyterlab-tailscale/` records applied ports so stale rules are removed. |

Idempotency: every step is re-runnable; `.env` is never overwritten once it exists; Compose
recreates only what changed; ufw `allow`/`delete` and firewalld `--add/--remove-port` are
exit-0 when already in the desired state.

### 2.8 Root step (`host-setup`)

1. `net.ipv4.ip_nonlocal_bind=1` via `/etc/sysctl.d/60-jupyterlab-tailscale.conf` + `sysctl -p`.
2. If ufw is enabled: per-port `ufw allow in on tailscale0 …` rules and the `DOCKER-USER`
   block in `/etc/ufw/after.rules`; `ufw reload`. Ports no longer wanted are deleted.
3. If firewalld is active: port in the zone of `tailscale0` (create `jupyter-tailnet` zone if none).
4. Otherwise: report "no active firewall; services are reachable only on `<ts-ip>`".

The unprivileged commands skip the root step when nothing would change (sysctl already 1,
no active firewall — the common case on this laptop after the first install).

### 2.9 Security model (README wording)

Services bind only to the tailnet IP; the tablet reaches them through the encrypted
Tailscale tunnel; both services require the password. Documented residual risk: a device on
the same LAN that routes to the laptop can address `<ts-ip>:8888` directly (Docker publishes
by destination address, not interface); the `DOCKER-USER` rule closes this when a firewall
is active, and a Tailscale ACL limits which tailnet devices may connect. Do not expose the
ports through a router, Funnel or a public proxy.

## 3. Stage 2 — PyQt6 builder (`builder.py`, `run-builder.sh`)

- `run-builder.sh`: creates/reuses `./.venv` with `/usr/bin/python3` (not the conda
  `python3` on PATH — a launcher would otherwise build a different venv than a terminal),
  installs `requirements-builder.txt` (`PyQt6==6.11.0`, `PyQt6-Qt6==6.11.2`,
  `PyQt6-sip==13.12.0`, `--only-binary=:all:`), stamps the venv with the requirements hash,
  execs `builder.py`. Checks `libxcb-cursor0` (Qt ≥ 6.5 needs it; present here).
- `builder.py` (single file, ~700 lines), Fusion style, QSS built from the oya tokens
  (`window_bg←primary-bg`, `card_bg←bubble-bg`, `card_border←accent-2/accent-1`,
  `input_bg←sunken`, `accent←accent`, `success/caution/warn`), light/dark from the OS
  with a header toggle stored in QSettings — the objectName conventions of
  `portal/tools/common/theme.py` (`#card`, `#primaryButton`, `#log`, badges) are kept.
- Layout: header (title, Tailscale IP / docker versions, theme toggle); **Settings** card:
  password field with Show/Hide, JupyterLab port spin (1024–65535, default 8888), "Enable
  FastAPI statistics" checkbox, stats port spin (default 8889, disabled when unchecked),
  read-only stats username, inline error label; **Services** card: one row per container
  with a state dot (running/starting/stopped/error), health badge, URL, Open button;
  **Output** read-only monospace log (4000 lines, ANSI stripped); bottom row: Stop,
  Start/Restart, Deploy/Update (primary).
- Processes: one `Runner` around `QProcess` (argv lists only, merged channels, stdin closed,
  new session, SIGTERM→SIGKILL cancel, "failed to start" vs exit code vs crash). Buttons are
  disabled while a job runs; `closeEvent` cancels and waits. Status polling every 4 s with
  `docker compose -p jupyterlab-tailscale ps -a --format json` (JSON Lines) while idle.
- Deploy = validate → write `.env` (atomic, `mkstemp` + `os.replace`, 0600) → `bash
  setup-jupyterlab-tailscale.sh update` → if a firewall is active or the sysctl is missing,
  `pkexec setup-jupyterlab-tailscale.sh host-setup <ip> <ports>` (exit 126 = dismissed,
  127 = not authorised). Stop → `… stop`; Start/Restart → `… start` / `… restart`.
  Disabling stats runs through the same `update`, which removes the container and the
  firewall port.
- Validation before Deploy: password non-empty, no `'`/`\`/newline; ports integers,
  ≥ 1024, ≤ 65535, different; occupancy by `socket.bind((ts_ip, port))` — ports held by this
  project's own containers are allowed. Tailscale IP from `tailscale status --json`
  (`BackendState == Running`, first IPv4 in `Self.TailscaleIPs`, inside 100.64.0.0/10).
- The password never appears in argv, environment, `compose.yaml`, or the log pane.
- `tests/test_builder.py` (unittest, `QT_QPA_PLATFORM=offscreen`, temp HOME): themes apply,
  password masked by default, port validation, `.env` mode 0600, Deploy/Stop argv.

## 4. Stage 3 — Dependencies page (`/dependencies`)

- Page + API in the stats container: a textarea holding `requirements.custom.txt` (one
  requirement per line, e.g. `torch --index-url https://download.pytorch.org/whl/cu128`),
  buttons **Install / update** (`pip install --target /opt/custom/site-packages --upgrade
  --constraint /opt/constraints.txt -r …`) and **Reset & reinstall** (wipe the target, then
  install), a live log (polled), a status pill, the installed package table (parsed from
  `*.dist-info/METADATA`), the Python version check, and the note "restart the kernel to
  pick up new packages".
- One job at a time (`asyncio.create_subprocess_exec`, no shell), log persisted to the
  volume, pip cache on the `pip_cache` volume so a failed 900 MB torch download is not
  repeated.
- JupyterLab side: the `python3` kernelspec gets `env: {PYTHONPATH: /opt/custom/site-packages,
  PATH: /opt/custom/site-packages/bin:…}` at image build; `constraints.txt` is
  `pip freeze` of the JupyterLab image copied into the stats image, so shared dependencies
  (numpy…) resolve to the exact versions the notebooks already have.
- GPU: torch's CUDA wheels bundle the runtime; with `gpus: all` `torch.cuda.is_available()`
  is true in notebooks. Same trust level as JupyterLab itself (arbitrary code), behind the
  same password — stated in the README.

## 5. Stage 4 — Ops links in the builder

A **Pages** card in the builder lists every page the stack serves — JupyterLab (`/lab`),
Statistics (`/`), Dependencies (`/dependencies`), Stats API (`/api/stats`), Health
(`/health`) — each with an Open button that launches the browser with the current
Tailscale IP and ports (`QDesktopServices.openUrl`). Buttons are disabled while the
corresponding container is not running. The list is a single table in `builder.py`,
mirrored by the navigation bar in the dashboard templates.

## 6. Stage 5 — Should thebe import toto-libs (subtree) or reuse the zenobia stack?

Recommendation: **no to both**; reuse the *design tokens and conventions*, not the code.

- The zenobia stack is Django + Postgres + Redis + Celery + nginx + Gitea + ten toto
  packages, driven by a 4 000-line `deploy.py`. thebe needs two containers and must keep
  "Docker, Compose, Tailscale" as its only dependencies. Reusing the stack would make the
  tool a zenobia deployment, not a standalone tool.
- oya is not a library: it is Tailwind Play CDN + Alpine configured at request time from a
  ~30-token theme JSON. The dashboard reproduces it with ~150 lines of plain CSS using the
  exact hex values and Tailwind defaults (radii, shadows, tracking, breakpoints). A subtree
  of `toto_libs` (hundreds of MB including `build/` trees) would bring Django templates and
  a 400 KB Tailwind runtime for one JSON file.
- The toto Qt builder framework (`portal/tools/common`, ~6 000 lines) is built around
  YAML config models and `deploy.py`; the borrowed parts (theme QSS structure, runner
  semantics, log pane, badges — ~200 lines) are copied into `builder.py` the way
  `delta_builder` already vendors them. Its own Qt palette is *not* oya-derived; thebe's
  mapping to the oya tokens is the closer match.
- **Decision (2026-09-14): harmonize, don't import.** Implemented as:
  - `stack/theme/{amazing,bitter,market,spectre}.json` — zenobia's theme files copied
    verbatim (same format, same token names).
  - `THEME='amazing'` in the settings `.env` selects one; the script and the builder validate it.
  - The stats app generates `/static/theme.css` (the `:root` light/dark custom properties)
    from that JSON at startup; `oya.css` only consumes the variables.
  - `builder.py` builds its QSS from the same JSON with the same oya → Qt token mapping, so
    the GUI and the dashboard always show one palette.
  - A few hundred lines in total; no toto or zenobia code or packages are imported.

## 7. Validation

Stage 1: `bash -n setup-jupyterlab-tailscale.sh`; `shellcheck` if available; `cd
~/.local/share/jupyterlab-tailscale && docker compose config && docker compose build`;
`./setup-jupyterlab-tailscale.sh install` (root step needs a terminal for `sudo`);
`status`; `ss -ltn` shows only `<ts-ip>:8888/8889`, nothing on `0.0.0.0`; `curl` from the
tailnet IP: JupyterLab login page, `/api/stats` 401 without and 200 with credentials,
`/health`; outbound internet from a kernel (`urllib.request.urlopen`) and `pip download`;
`stop`/`start`/`restart`/`logs`; re-run `install` (idempotent, no rebuild churn, hash
unchanged); change the password in `.env` → `update` recreates both containers, old
sessions are logged out; `uninstall --yes` leaves `~/jupyter-workspace` intact.

Stage 2: `python -m py_compile builder.py`; `bash -n run-builder.sh`; `run-builder.sh`
creates `.venv` and starts; offscreen unit tests; a real Deploy → Stop → Start round trip
from the GUI; the log never shows the password.

Stage 3: install `requests` (small) and `torch` (large, cu128) from the page; a notebook
imports both after a kernel restart; `torch.cuda.is_available()` on this laptop; Reset &
reinstall; the Jupyter server survives a deliberately broken package in the target.

Stage 4: each Open button reaches its page. Stage 5: decision only (§6).

## 8. Resolved questions

1. `install` applies `net.ipv4.ip_nonlocal_bind=1` (one `sudo`); `uninstall` removes it and
   restores the previous value.
2. All stages are implemented in order, each validated and committed before the next.
3. Stage 5 harmonizes with zenobia's theme files and conventions; nothing from toto or
   zenobia is imported.

## 9. Files per stage

- Stage 1: rewrite `setup-jupyterlab-tailscale.sh`, `README.md`; add `stack/…`, `.gitignore`,
  `plan.md`; `.env` is created at runtime (not committed).
- Stage 2: add `builder.py`, `run-builder.sh`, `requirements-builder.txt`,
  `tests/test_builder.py`; extend `README.md`.
- Stage 3: extend `stack/stats/app.py`, templates, `stack/Dockerfile`, `stack/compose.yaml`;
  `README.md`.
- Stage 4: extend `builder.py`, templates; `README.md`.
- Stage 5: `stack/theme/*.json`, theme loader in `stack/stats/app.py`, `stack/stats/static/oya.css`,
  `builder.py`, installer (`THEME` key); `README.md`.
