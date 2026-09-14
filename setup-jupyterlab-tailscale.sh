#!/usr/bin/env bash
set -Eeuo pipefail
shopt -s inherit_errexit

# JupyterLab over Tailscale: JupyterLab and a small statistics dashboard in Docker Compose,
# published only on this machine's Tailscale IPv4 address.
#
# Run the everyday commands as your normal desktop user (member of the docker group).
# Only host-setup / host-teardown need root; the other commands call them through sudo,
# and the Stage 2 builder calls them through pkexec.

# The root helpers run under sudo/pkexec: never trust the caller's PATH for them.
if [[ "$EUID" -eq 0 ]]; then
  PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
  export PATH
fi
IFS=$' \t\n'

readonly PROJECT='jupyterlab-tailscale'
readonly DEFAULT_PASSWORD='TailLab-7mK9-vQ2x-N4pR!'
readonly DEFAULT_JUPYTER_PORT='8888'
readonly DEFAULT_STATS_PORT='8889'
readonly DEFAULT_STATS_ENABLED='1'
readonly DEFAULT_STATS_USER='jupyter'
readonly DEFAULT_THEME='amazing'
readonly -a SETTINGS_KEYS=(JUPYTER_PASSWORD JUPYTER_PORT STATS_ENABLED STATS_PORT STATS_USER THEME)
readonly JUPYTER_IMAGE="${PROJECT}/jupyterlab:local"
readonly STATS_IMAGE="${PROJECT}/stats:local"
readonly SYSCTL_FILE='/etc/sysctl.d/60-jupyterlab-tailscale.conf'
readonly NONLOCAL_BIND_PROC='/proc/sys/net/ipv4/ip_nonlocal_bind'
readonly ROOT_STATE_DIR='/var/lib/jupyterlab-tailscale'
readonly ROOT_STATE_FILE="${ROOT_STATE_DIR}/state"
readonly UFW_CONF='/etc/ufw/ufw.conf'
readonly UFW_AFTER_RULES='/etc/ufw/after.rules'
readonly UFW_BLOCK_BEGIN="# BEGIN ${PROJECT}"
readonly UFW_BLOCK_END="# END ${PROJECT}"
readonly FIREWALLD_ZONE='jupyter-tailnet'
readonly TAILSCALE_IFACE='tailscale0'

# ---------------------------------------------------------------------------------------------
# Output helpers (colour only when the stream is a terminal)
# ---------------------------------------------------------------------------------------------

use_colour() {
  [[ -t "$1" && -z "${NO_COLOR:-}" ]]
}

info() {
  if use_colour 1; then
    printf '\033[1;34m==>\033[0m %s\n' "$*"
  else
    printf '==> %s\n' "$*"
  fi
}

warn() {
  if use_colour 2; then
    printf '\033[1;33mWARNING:\033[0m %s\n' "$*" >&2
  else
    printf 'WARNING: %s\n' "$*" >&2
  fi
}

die() {
  if use_colour 2; then
    printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2
  else
    printf 'ERROR: %s\n' "$*" >&2
  fi
  exit 1
}

on_unexpected_error() {
  printf 'ERROR: unexpected failure (exit %s) at line %s of %s\n' "$1" "$2" "$SCRIPT_PATH" >&2
}

# ---------------------------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------------------------

account_home() {
  local resolved_home
  resolved_home="$(getent passwd "$EUID" | cut -d: -f6)" || resolved_home=''
  printf '%s\n' "${resolved_home:-${HOME:?Unable to determine user home}}"
}

absolute_path() {
  if [[ "$1" == /* ]]; then
    printf '%s\n' "$1"
  else
    printf '%s/%s\n' "$PWD" "$1"
  fi
}

SCRIPT_PATH="$(readlink -f -- "${BASH_SOURCE[0]}")"
SCRIPT_DIR="${SCRIPT_PATH%/*}"
STACK_DIR="${SCRIPT_DIR}/stack"
# zenobia's theme files (THEME setting), copied into the stats image.
THEME_DIR="${STACK_DIR}/theme"
ACCOUNT_HOME="$(account_home)"
SETTINGS_FILE="$(absolute_path "${JLT_SETTINGS_FILE:-${SCRIPT_DIR}/.env}")"
APP_DIR="$(absolute_path "${JLT_APP_DIR:-${ACCOUNT_HOME}/.local/share/${PROJECT}}")"
WORKSPACE_DIR="$(absolute_path "${JLT_WORKSPACE_DIR:-${ACCOUNT_HOME}/jupyter-workspace}")"
LOCK_FILE="${XDG_RUNTIME_DIR:-/tmp}/${PROJECT}-${EUID}.lock"

# How to invoke this script in hints: short when run from the repo directory.
script_cmd() {
  if [[ "$PWD" == "$SCRIPT_DIR" ]]; then
    printf './%s\n' "${SCRIPT_PATH##*/}"
  else
    printf '%q\n' "$SCRIPT_PATH"
  fi
}

# ---------------------------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------------------------

# Writes content to a temp file in the destination directory and renames it into place, so
# readers never see a half-written file. Optional 4th argument: owner (root helpers only).
write_file_atomic() {
  local dest="$1" mode="$2" content="$3" owner="${4:-}" dir tmp
  dir="${dest%/*}"
  tmp="$(mktemp "${dir:-/}/.${dest##*/}.XXXXXX")" || die "Cannot create a temporary file next to $dest"
  if ! {
    printf '%s' "$content" >"$tmp" &&
      chmod "$mode" "$tmp" &&
      { [[ -z "$owner" ]] || chown "$owner" "$tmp"; } &&
      mv -f -- "$tmp" "$dest"
  }; then
    rm -f -- "$tmp"
    die "Could not write $dest"
  fi
}

# Prints the ports sorted numerically, de-duplicated and space separated.
sorted_ports() {
  local sorted
  (($# > 0)) || return 0
  sorted="$(printf '%s\n' "$@" | sort -n -u)"
  printf '%s\n' "${sorted//$'\n'/ }"
}

list_contains() {
  [[ " $1 " == *" $2 "* ]]
}

is_valid_port() {
  [[ "$1" =~ ^[1-9][0-9]{3,4}$ ]] && ((10#$1 >= 1024 && 10#$1 <= 65535))
}

is_valid_stats_user() {
  [[ "$1" =~ ^[A-Za-z0-9._-]{1,32}$ ]]
}

is_valid_theme_name() {
  [[ "$1" =~ ^[A-Za-z0-9_-]{1,64}$ ]]
}

# A regular file, not a symlink: sync_stack copies only regular files, so a linked theme
# would pass here and then be missing from the deployed copy.
is_theme_file() {
  [[ -f "$1" && ! -L "$1" ]]
}

# The theme names in stack/theme, space separated. Only file names are checked here (no host
# Python to parse JSON); the dashboard validates the colours and falls back to Amazing Moon
# with a warning in its log.
available_themes() {
  local path name names=()
  for path in "$THEME_DIR"/*.json; do
    name="${path##*/}"
    name="${name%.json}"
    if is_theme_file "$path" && is_valid_theme_name "$name"; then
      names+=("$name")
    fi
  done
  printf '%s\n' "${names[*]}"
}

# True for a canonical dotted quad inside Tailscale's CGNAT range 100.64.0.0/10.
is_tailscale_ipv4() {
  local octet re='^(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})\.(0|[1-9][0-9]{0,2})$'
  [[ "$1" =~ $re ]] || return 1
  for octet in "${BASH_REMATCH[@]:1:4}"; do
    ((octet <= 255)) || return 1
  done
  ((BASH_REMATCH[1] == 100 && BASH_REMATCH[2] >= 64 && BASH_REMATCH[2] <= 127))
}

# A path we are willing to write into the single-quoted runtime .env and bind-mount.
is_safe_workspace_path() {
  [[ "$1" == /* && "$1" != *\'* && "$1" != *\\* && "$1" != *[[:cntrl:]]* ]]
}

# Refuses to recursively delete "/", the home directory or an empty path.
is_safe_to_delete() {
  local target home
  target="$(readlink -m -- "$1")"
  home="$(readlink -m -- "$ACCOUNT_HOME")"
  [[ -n "$1" && "$target" != '/' && "$target" != "$home" ]]
}

# ---------------------------------------------------------------------------------------------
# KEY=value files (settings, runtime .env, root state). Never sourced.
# ---------------------------------------------------------------------------------------------

# Parses the file into the associative array named by $2. Accepts KEY=value, KEY='value' and
# KEY="value" (no escapes, no interpolation), skips blank and # lines, strips CR from CRLF
# files. Malformed lines go to ENV_PARSE_ERRORS by line number only, so a password is never
# echoed.
read_env_file() {
  local file="$1" line key raw lineno=0
  local -n parsed_env="$2"
  local key_re='^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=(.*)$'
  ENV_PARSE_ERRORS=()
  while IFS= read -r line || [[ -n "$line" ]]; do
    lineno=$((lineno + 1))
    line="${line%$'\r'}"
    if [[ "$line" =~ ^[[:space:]]*(#|$) ]]; then
      continue
    fi
    if [[ ! "$line" =~ $key_re ]]; then
      ENV_PARSE_ERRORS+=("line ${lineno}: expected KEY=value")
      continue
    fi
    key="${BASH_REMATCH[1]}"
    raw="${BASH_REMATCH[2]}"
    raw="${raw#"${raw%%[![:space:]]*}"}"
    raw="${raw%"${raw##*[![:space:]]}"}"
    if [[ ${#raw} -ge 2 && ( "$raw" == \'*\' || "$raw" == \"*\" ) ]]; then
      raw="${raw:1:${#raw}-2}"
    elif [[ "$raw" == [\'\"]* ]]; then
      ENV_PARSE_ERRORS+=("line ${lineno}: the value of ${key} has no closing quote")
      continue
    fi
    # shellcheck disable=SC2034 # nameref: the assignment lands in the caller's array
    parsed_env["$key"]="$raw"
  done <"$file"
}

# ---------------------------------------------------------------------------------------------
# Settings (.env next to this script)
# ---------------------------------------------------------------------------------------------

default_settings_content() {
  printf '%s\n' \
    '# JupyterLab over Tailscale settings. Read by setup-jupyterlab-tailscale.sh (never sourced).' \
    '# Values are literal. The password needs 8-128 characters without quote or backslash.' \
    '# After editing, apply with: ./setup-jupyterlab-tailscale.sh update' \
    '# THEME colours the dashboard and the builder: a file name from stack/theme without .json.' \
    "JUPYTER_PASSWORD='${DEFAULT_PASSWORD}'" \
    "JUPYTER_PORT='${DEFAULT_JUPYTER_PORT}'" \
    "STATS_ENABLED='${DEFAULT_STATS_ENABLED}'" \
    "STATS_PORT='${DEFAULT_STATS_PORT}'" \
    "STATS_USER='${DEFAULT_STATS_USER}'" \
    "THEME='${DEFAULT_THEME}'"
}

ensure_settings_file() {
  local mode
  if [[ ! -e "$SETTINGS_FILE" ]]; then
    [[ -d "${SETTINGS_FILE%/*}" ]] || die "Directory for the settings file does not exist: ${SETTINGS_FILE%/*}"
    write_file_atomic "$SETTINGS_FILE" 600 "$(default_settings_content)"$'\n'
    info "Created settings file with defaults: $SETTINGS_FILE (mode 600)"
    return 0
  fi
  [[ -f "$SETTINGS_FILE" ]] || die "Settings path exists but is not a regular file: $SETTINGS_FILE"
  mode="$(stat -L -c '%a' -- "$SETTINGS_FILE")"
  if (((8#$mode & 8#077) != 0)); then
    chmod 600 -- "$SETTINGS_FILE"
    info "Note: $SETTINGS_FILE was mode $mode; changed to 600 because it holds the password."
  fi
}

normalize_bool() {
  case "${1,,}" in
    1 | true | yes | on) printf '1\n' ;;
    0 | false | no | off) printf '0\n' ;;
    *) return 1 ;;
  esac
}

# Loads the settings into JUPYTER_PASSWORD, JUPYTER_PORT, STATS_ENABLED, STATS_PORT,
# STATS_USER, THEME. Missing keys (or a missing file) fall back to the defaults. Problems are
# collected in SETTINGS_PROBLEMS; callers decide whether they are fatal.
load_settings() {
  local key known entry bool themes
  declare -gA SETTINGS_RAW=()
  SETTINGS_PROBLEMS=()
  if [[ -e "$SETTINGS_FILE" ]]; then
    if [[ ! -r "$SETTINGS_FILE" ]]; then
      SETTINGS_PROBLEMS+=("$SETTINGS_FILE is not readable")
    else
      read_env_file "$SETTINGS_FILE" SETTINGS_RAW
      for entry in "${ENV_PARSE_ERRORS[@]}"; do
        SETTINGS_PROBLEMS+=("$entry")
      done
      while IFS= read -r key; do
        [[ -n "$key" ]] || continue
        known=0
        list_contains "${SETTINGS_KEYS[*]}" "$key" && known=1
        ((known)) || warn "Unknown key ${key} in $SETTINGS_FILE is ignored."
      done < <(printf '%s\n' "${!SETTINGS_RAW[@]}" | sort)
    fi
  fi

  JUPYTER_PASSWORD="${SETTINGS_RAW[JUPYTER_PASSWORD]-$DEFAULT_PASSWORD}"
  JUPYTER_PORT="${SETTINGS_RAW[JUPYTER_PORT]-$DEFAULT_JUPYTER_PORT}"
  STATS_ENABLED="${SETTINGS_RAW[STATS_ENABLED]-$DEFAULT_STATS_ENABLED}"
  STATS_PORT="${SETTINGS_RAW[STATS_PORT]-$DEFAULT_STATS_PORT}"
  STATS_USER="${SETTINGS_RAW[STATS_USER]-$DEFAULT_STATS_USER}"
  THEME="${SETTINGS_RAW[THEME]-$DEFAULT_THEME}"

  local length="${#JUPYTER_PASSWORD}"
  if ((length < 8 || length > 128)); then
    SETTINGS_PROBLEMS+=("JUPYTER_PASSWORD must be 8-128 characters long (it has ${length})")
  fi
  if [[ "$JUPYTER_PASSWORD" == *\'* ]]; then
    SETTINGS_PROBLEMS+=("JUPYTER_PASSWORD must not contain a single quote (')")
  fi
  if [[ "$JUPYTER_PASSWORD" == *\\* ]]; then
    SETTINGS_PROBLEMS+=("JUPYTER_PASSWORD must not contain a backslash (\\)")
  fi
  if [[ "$JUPYTER_PASSWORD" == *[[:cntrl:]]* ]]; then
    SETTINGS_PROBLEMS+=("JUPYTER_PASSWORD must not contain control characters (tabs, line breaks, ...)")
  fi
  if [[ "$JUPYTER_PASSWORD" =~ ^[[:space:]] || "$JUPYTER_PASSWORD" =~ [[:space:]]$ ]]; then
    SETTINGS_PROBLEMS+=("JUPYTER_PASSWORD must not start or end with whitespace")
  fi

  for key in JUPYTER_PORT STATS_PORT; do
    if ! is_valid_port "${!key}"; then
      SETTINGS_PROBLEMS+=("${key} must be a whole number from 1024 to 65535 (got $(printf '%q' "${!key}"))")
    fi
  done
  if [[ "$JUPYTER_PORT" == "$STATS_PORT" ]]; then
    SETTINGS_PROBLEMS+=("JUPYTER_PORT and STATS_PORT must be different (both are $(printf '%q' "$JUPYTER_PORT"))")
  fi

  if bool="$(normalize_bool "$STATS_ENABLED")"; then
    STATS_ENABLED="$bool"
  else
    SETTINGS_PROBLEMS+=("STATS_ENABLED must be 1 or 0 (true/false, yes/no, on/off also work; got $(printf '%q' "$STATS_ENABLED"))")
  fi

  if ! is_valid_stats_user "$STATS_USER"; then
    SETTINGS_PROBLEMS+=("STATS_USER must be 1-32 characters from A-Z a-z 0-9 . _ - (got $(printf '%q' "$STATS_USER"))")
  fi

  if ! is_valid_theme_name "$THEME" || ! is_theme_file "$THEME_DIR/$THEME.json"; then
    themes="$(available_themes)"
    if [[ -n "$themes" ]]; then
      SETTINGS_PROBLEMS+=("THEME must be one of: ${themes// /, } (files in ${THEME_DIR}; got $(printf '%q' "$THEME"))")
    else
      SETTINGS_PROBLEMS+=("THEME: no theme files found in ${THEME_DIR}; is the repository complete?")
    fi
  fi
}

require_valid_settings() {
  local message problem
  ((${#SETTINGS_PROBLEMS[@]} == 0)) && return 0
  message="Invalid settings in $SETTINGS_FILE:"
  for problem in "${SETTINGS_PROBLEMS[@]}"; do
    message+=$'\n'"  - ${problem}"
  done
  die "$message"
}

# Ports the settings want published: JupyterLab plus statistics when enabled.
wanted_ports() {
  if [[ "$STATS_ENABLED" == 1 ]]; then
    printf '%s %s\n' "$JUPYTER_PORT" "$STATS_PORT"
  else
    printf '%s\n' "$JUPYTER_PORT"
  fi
}

# ---------------------------------------------------------------------------------------------
# Tailscale
# ---------------------------------------------------------------------------------------------

# True when the address is assigned to a local interface (or when `ip` is unavailable).
ipv4_is_local() {
  local _index _ifname _family cidr _rest
  command -v ip >/dev/null 2>&1 || return 0
  while read -r _index _ifname _family cidr _rest; do
    if [[ "${cidr%/*}" == "$1" ]]; then
      return 0
    fi
  done < <(ip -4 -o addr show 2>/dev/null || true)
  return 1
}

# Sets TS_PROBE_IP, or returns 1 with the reason in TS_PROBE_ERROR. Not meant for $(...).
probe_tailscale_ipv4() {
  local output first
  TS_PROBE_IP=''
  TS_PROBE_ERROR=''
  if ! command -v tailscale >/dev/null 2>&1; then
    TS_PROBE_ERROR='Tailscale CLI not found. Install and connect Tailscale first: https://tailscale.com/download/linux'
    return 1
  fi
  output="$(tailscale ip -4 2>/dev/null)" || output=''
  first="${output%%$'\n'*}"
  first="${first%$'\r'}"
  first="${first//[[:space:]]/}"
  if [[ -z "$first" ]]; then
    TS_PROBE_ERROR='Tailscale has no IPv4 address (logged out or not running). Run: sudo tailscale up'
    return 1
  fi
  if ! is_tailscale_ipv4 "$first"; then
    TS_PROBE_ERROR="'tailscale ip -4' returned $(printf '%q' "$first"), which is not inside 100.64.0.0/10."
    return 1
  fi
  if ! ipv4_is_local "$first"; then
    TS_PROBE_ERROR="Tailscale reports ${first}, but no local interface has that address (is tailscaled up? Try: sudo tailscale up)."
    return 1
  fi
  TS_PROBE_IP="$first"
}

# Polls every 2 s for up to $1 seconds; sets TS_IP or dies with the last reason.
wait_for_tailscale() {
  local timeout="$1" started="$SECONDS" announced=0
  until probe_tailscale_ipv4; do
    command -v tailscale >/dev/null 2>&1 || die "$TS_PROBE_ERROR"
    if ((SECONDS - started >= timeout)); then
      die "$TS_PROBE_ERROR"
    fi
    if ((!announced)); then
      info "Waiting up to ${timeout}s for the Tailscale IPv4 address..."
      announced=1
    fi
    sleep 2
  done
  TS_IP="$TS_PROBE_IP"
}

# ---------------------------------------------------------------------------------------------
# Docker / Compose
# ---------------------------------------------------------------------------------------------

require_docker() {
  command -v docker >/dev/null 2>&1 ||
    die 'Docker CLI not found. Install Docker Engine: https://docs.docker.com/engine/install/'
  docker compose version >/dev/null 2>&1 ||
    die "'docker compose' (Compose v2) is not available. Install the docker-compose-plugin package."
  docker info >/dev/null 2>&1 ||
    die "Cannot talk to the Docker daemon. Is it running? If you were just added to the docker group, log out and back in (or run: newgrp docker)."
}

docker_usable() {
  command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 && docker compose version >/dev/null 2>&1
}

# Runs docker compose inside APP_DIR so the generated .env (COMPOSE_FILE, COMPOSE_PROFILES,
# ports, ...) applies. Shell variables take precedence over .env in Compose, so the contract
# variables are unset first: a stray exported TS_IP must not override the deployed one.
compose() {
  local progress=()
  [[ -t 1 ]] || progress=(--progress plain)
  (
    unset COMPOSE_PROJECT_NAME COMPOSE_FILE COMPOSE_PROFILES COMPOSE_ENV_FILES COMPOSE_PATH_SEPARATOR \
      TS_IP JUPYTER_PORT STATS_PORT STATS_USER THEME WORKSPACE_DIR HOST_NAME JLT_UID JLT_GID
    # BuildKit attaches a timestamped provenance attestation by default, so even a fully cached
    # rebuild gets a new image ID and 'up' recreates both containers (killing running kernels).
    # These images are never pushed; without the attestation the ID only changes with the content.
    # (Compose 5.3 ignores 'provenance: false' in compose.yaml, hence the environment variable.)
    export BUILDX_NO_DEFAULT_ATTESTATIONS=1
    cd "$APP_DIR" && exec docker compose "${progress[@]}" "$@"
  )
}

is_installed() {
  [[ -f "$APP_DIR/compose.yaml" && -f "$APP_DIR/.env" ]]
}

require_installed() {
  is_installed || die "Not installed ($APP_DIR has no compose.yaml/.env). Run: $(script_cmd) install"
}

require_stack() {
  local name
  for name in Dockerfile compose.yaml compose.gpu.yaml; do
    [[ -f "$STACK_DIR/$name" ]] || die "Missing $STACK_DIR/$name; is the repository complete?"
  done
  for name in jupyter stats theme; do
    [[ -d "$STACK_DIR/$name" ]] || die "Missing $STACK_DIR/$name/; is the repository complete?"
  done
}

compose_failure() {
  warn "$1"
  printf '\n--- docker compose ps -a ---\n' >&2
  compose ps -a >&2 || true
  printf '\n--- last 60 log lines per service ---\n' >&2
  compose logs --no-color --tail 60 >&2 || true
  die "$1 See the output above; '$(script_cmd) status' and '$(script_cmd) logs' help as well."
}

refuse_root() {
  if [[ "$EUID" -eq 0 ]]; then
    die "Run this as your normal desktop user (in the docker group), not as root or with sudo. It asks for sudo itself when a host change is needed."
  fi
}

# ---------------------------------------------------------------------------------------------
# Deploy steps
# ---------------------------------------------------------------------------------------------

validate_gpu_mode() {
  case "${JLT_GPU:-auto}" in
    auto | on | off) ;;
    *) die "JLT_GPU must be auto, on or off (got $(printf '%q' "$JLT_GPU"))" ;;
  esac
}

# auto: the driver works (nvidia-smi -L) AND Docker can hand the GPU to containers, either via
# the nvidia runtime or a CDI spec for nvidia.com/gpu.
gpu_available() {
  local runtimes spec
  command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1 || return 1
  # shellcheck disable=SC2016 # Go template, not a shell expansion
  runtimes="$(docker info --format '{{range $name, $_ := .Runtimes}}{{$name}} {{end}}' 2>/dev/null)" || runtimes=''
  if list_contains "$runtimes" nvidia; then
    return 0
  fi
  for spec in /etc/cdi/* /var/run/cdi/*; do
    if [[ -f "$spec" ]] && grep -qsF 'nvidia.com/gpu' -- "$spec"; then
      return 0
    fi
  done
  return 1
}

gpu_requested() {
  case "${JLT_GPU:-auto}" in
    on) return 0 ;;
    off) return 1 ;;
    *) gpu_available ;;
  esac
}

ensure_workspace() {
  is_safe_workspace_path "$WORKSPACE_DIR" ||
    die "Workspace path must be absolute and must not contain quotes, backslashes or control characters: $(printf '%q' "$WORKSPACE_DIR")"
  if [[ -e "$WORKSPACE_DIR" || -L "$WORKSPACE_DIR" ]]; then
    # An existing workspace is left exactly as it is (never chmod/chown user data).
    [[ -d "$WORKSPACE_DIR" ]] || die "Workspace path exists but is not a directory: $WORKSPACE_DIR"
    [[ -w "$WORKSPACE_DIR" ]] || warn "Workspace $WORKSPACE_DIR is not writable by $(id -un); notebooks cannot be saved."
  else
    install -d -m 700 -- "$WORKSPACE_DIR"
    info "Created workspace $WORKSPACE_DIR (mode 700)"
  fi
}

# Removes what the previous (venv + systemd user service) installer left behind.
legacy_cleanup() {
  local unit_name="${PROJECT}.service"
  local unit="$ACCOUNT_HOME/.config/systemd/user/$unit_name"
  local wants="$ACCOUNT_HOME/.config/systemd/user/default.target.wants/$unit_name"
  local launcher="$ACCOUNT_HOME/.local/bin/$PROJECT"
  local old_config="$ACCOUNT_HOME/.config/$PROJECT"

  if [[ -e "$unit" || -L "$unit" ]]; then
    if command -v systemctl >/dev/null 2>&1; then
      systemctl --user disable --now "$unit_name" >/dev/null 2>&1 ||
        warn "Could not stop/disable the old user service $unit_name (continuing)."
    fi
    rm -f -- "$unit"
    # disable normally removes the symlink; do it by hand when there was no user bus.
    [[ -L "$wants" ]] && rm -f -- "$wants"
    if command -v systemctl >/dev/null 2>&1; then
      systemctl --user daemon-reload >/dev/null 2>&1 || true
    fi
    info "Removed the old systemd user service: $unit"
  fi
  if [[ -f "$launcher" ]] && grep -qF 'JupyterLab over Tailscale' -- "$launcher"; then
    rm -f -- "$launcher"
    info "Removed the old launcher: $launcher"
  fi
  if [[ -d "$APP_DIR/venv" ]]; then
    rm -rf -- "${APP_DIR:?}/venv"
    info "Removed the old virtualenv: $APP_DIR/venv"
  fi
  if [[ -d "$old_config" ]]; then
    rm -rf -- "$old_config"
    info "Removed the old config directory: $old_config"
  fi
}

# Prints "<port> <local address>" for each listener that would stop Docker from publishing
# TS_IP:<port>. Listeners on another specific address (e.g. 127.0.0.1) do not conflict; ports
# already published by this project's own running containers are expected on a re-run.
find_port_conflicts() {
  local ts_ip="$1" port line rest addr inner _state _recvq _sendq local_addr _peer
  local published_re=':([0-9]+)->'
  local -A wanted=() ours=()
  shift
  for port in "$@"; do
    wanted["$port"]=1
  done

  while IFS= read -r line; do
    rest="$line"
    while [[ "$rest" =~ $published_re ]]; do
      ours["${BASH_REMATCH[1]}"]=1
      rest="${rest#*"${BASH_REMATCH[0]}"}"
    done
  done < <(docker ps --filter "label=com.docker.compose.project=${PROJECT}" --format '{{.Ports}}' 2>/dev/null || true)

  while read -r _state _recvq _sendq local_addr _peer; do
    [[ -n "${local_addr:-}" ]] || continue
    port="${local_addr##*:}"
    addr="${local_addr%:*}"
    [[ -n "${wanted[$port]:-}" ]] || continue
    # Drop an interface scope such as 0.0.0.0%wlan0 or [::%lo].
    if [[ "$addr" == \[*\] ]]; then
      inner="${addr:1:${#addr}-2}"
      addr="[${inner%%\%*}]"
    else
      addr="${addr%%\%*}"
    fi
    case "$addr" in
      "$ts_ip" | 0.0.0.0 | '[::]' | '*') ;;
      *) continue ;;
    esac
    [[ -n "${ours[$port]:-}" ]] && continue
    printf '%s %s\n' "$port" "$local_addr"
  done < <(ss -Htln 2>/dev/null || true)
}

check_ports() {
  local ts_ip="$1" conflicts port address setting message
  shift
  if ! command -v ss >/dev/null 2>&1; then
    warn "'ss' not found; skipping the port availability check."
    return 0
  fi
  conflicts="$(find_port_conflicts "$ts_ip" "$@")"
  [[ -n "$conflicts" ]] || return 0
  message='Ports needed by the stack are already in use:'
  while read -r port address; do
    setting='STATS_PORT'
    [[ "$port" == "$JUPYTER_PORT" ]] && setting='JUPYTER_PORT'
    message+=$'\n'"  - ${port} (${setting}) is taken by a listener on ${address}"
  done <<<"$conflicts"
  message+=$'\n'"Stop that program or choose another port in $SETTINGS_FILE, then run the command again."
  die "$message"
}

# Copies one directory tree with fixed modes (dirs 755, files 644), skipping Python caches
# and anything that is neither a file nor a directory.
copy_tree() {
  local src="$1" dst="$2" path rel
  while IFS= read -r -d '' path; do
    rel="${path#"$src"/}"
    if [[ -d "$path" ]]; then
      install -d -m 755 -- "$dst/$rel" || return 1
    else
      install -m 644 -- "$path" "$dst/$rel" || return 1
    fi
  done < <(find "$src" -mindepth 1 \( -name __pycache__ -o -name '*.pyc' \) -prune -o \( -type d -o -type f \) -print0)
}

# The running stack is a snapshot of stack/: editing the repo changes nothing until update.
sync_stack() {
  local name tmp ignore=''
  install -d -m 700 -- "$APP_DIR" "$APP_DIR/secrets"
  chmod 700 -- "$APP_DIR" "$APP_DIR/secrets"
  rm -rf -- "$APP_DIR"/.sync-*
  for name in Dockerfile compose.yaml compose.gpu.yaml; do
    install -m 644 -- "$STACK_DIR/$name" "$APP_DIR/$name"
  done
  for name in jupyter stats theme; do
    # Build the new tree next to the old one and swap, so a failed copy never leaves half a tree.
    tmp="$(mktemp -d "$APP_DIR/.sync-${name}.XXXXXX")"
    if ! copy_tree "$STACK_DIR/$name" "$tmp"; then
      rm -rf -- "$tmp"
      die "Could not copy $STACK_DIR/$name to $APP_DIR"
    fi
    chmod 755 -- "$tmp"
    rm -rf -- "${APP_DIR:?}/$name"
    mv -- "$tmp" "$APP_DIR/$name"
  done
  # The build context is APP_DIR itself, which also holds secrets/ and the generated .env.
  # BuildKit only sends what the Dockerfile copies, but keep them out explicitly anyway.
  if [[ -f "$STACK_DIR/.dockerignore" ]]; then
    ignore="$(<"$STACK_DIR/.dockerignore")"$'\n'
  fi
  ignore+=$'# Added by setup-jupyterlab-tailscale.sh\nsecrets\n.env\n.sync-*\n**/__pycache__\n'
  write_file_atomic "$APP_DIR/.dockerignore" 644 "$ignore"
}

sanitized_hostname() {
  local name
  name="$(hostname 2>/dev/null)" || name="$(uname -n)"
  name="${name//[^A-Za-z0-9._-]/}"
  printf '%s\n' "${name:-localhost}"
}

# Content of APP_DIR/.env (CONTRACT.md). Every value single-quoted: Compose treats those
# literally, so no $ or # in a value can be interpolated. No secrets are written here.
runtime_env_content() {
  local gpu="$1" compose_file='compose.yaml' profiles=''
  if [[ "$gpu" == 1 ]]; then
    compose_file='compose.yaml:compose.gpu.yaml'
  fi
  if [[ "$STATS_ENABLED" == 1 ]]; then
    profiles='stats'
  fi
  printf '%s\n' \
    "# Generated by setup-jupyterlab-tailscale.sh; do not edit. Change the settings file and run update." \
    "COMPOSE_PROJECT_NAME='${PROJECT}'" \
    "COMPOSE_FILE='${compose_file}'" \
    "COMPOSE_PROFILES='${profiles}'" \
    "TS_IP='${TS_IP}'" \
    "JUPYTER_PORT='${JUPYTER_PORT}'" \
    "STATS_PORT='${STATS_PORT}'" \
    "STATS_USER='${STATS_USER}'" \
    "THEME='${THEME}'" \
    "WORKSPACE_DIR='${WORKSPACE_DIR}'" \
    "HOST_NAME='$(sanitized_hostname)'" \
    "JLT_UID='$(id -u)'" \
    "JLT_GID='$(id -g)'"
}

write_runtime_env() {
  is_safe_workspace_path "$WORKSPACE_DIR" ||
    die "Refusing workspace path with a quote, backslash or control character: $(printf '%q' "$WORKSPACE_DIR")"
  is_tailscale_ipv4 "$TS_IP" || die "Refusing to write an invalid Tailscale IPv4: $(printf '%q' "$TS_IP")"
  is_valid_theme_name "$THEME" || die "Refusing to write an invalid theme name: $(printf '%q' "$THEME")"
  write_file_atomic "$APP_DIR/.env" 600 "$(runtime_env_content "$1")"$'\n'
}

gpu_probe() {
  local output
  if output="$(docker run --rm --network none --gpus all --entrypoint nvidia-smi "$JUPYTER_IMAGE" -L 2>&1)"; then
    info "GPU check passed: ${output%%$'\n'*}"
    return 0
  fi
  warn "GPU check failed inside the container: ${output%%$'\n'*}"
  return 1
}

# Keeps the argon2 hash when the password is unchanged: re-hashing produces a new salt, which
# changes Jupyter's cookie secret and logs every browser out. Sets HASH_CHANGED=1 otherwise.
ensure_secrets() {
  local dir="$APP_DIR/secrets" current='' hash
  local password_file="$dir/jupyter_password" hash_file="$dir/jupyter_hashed_password"
  HASH_CHANGED=0
  install -d -m 700 -- "$dir"
  chmod 700 -- "$dir"
  if [[ -f "$password_file" ]]; then
    current="$(<"$password_file")"
  fi
  if [[ -f "$password_file" && -s "$hash_file" && "$current" == "$JUPYTER_PASSWORD" ]]; then
    chmod 600 -- "$password_file" "$hash_file"
    info 'Password unchanged; keeping the existing hash (browser sessions stay logged in).'
    return 0
  fi
  info 'Hashing the JupyterLab password inside the image (argon2)...'
  # The password travels over stdin only: never in argv, the environment or a compose file.
  if ! hash="$(printf '%s' "$JUPYTER_PASSWORD" |
    docker run --rm -i --network none --entrypoint python "$JUPYTER_IMAGE" \
      -c 'import sys; from jupyter_server.auth import passwd; sys.stdout.write(passwd(sys.stdin.read()))')"; then
    die "Could not hash the password with $JUPYTER_IMAGE."
  fi
  [[ "$hash" == argon2:* ]] || die "Unexpected output while hashing the password (expected an argon2 hash)."
  # Hash first: if we are interrupted in between, the old plaintext no longer matches and
  # the next run hashes again instead of keeping a stale hash.
  write_file_atomic "$hash_file" 600 "$hash"
  write_file_atomic "$password_file" 600 "$JUPYTER_PASSWORD"
  HASH_CHANGED=1
}

# Random credential between the dashboard and the package runner (deps). deps never gets the
# JupyterLab password or its hash: pip runs the build scripts of third-party packages there.
# Created once and then kept; sets TOKEN_CHANGED=1 when it was (re)created, because Compose
# does not notice a changed secret file and the two containers must read the same one.
ensure_runner_token() {
  local file="$APP_DIR/secrets/deps_token" token=''
  TOKEN_CHANGED=0
  install -d -m 700 -- "$APP_DIR/secrets"
  if [[ -f "$file" ]]; then
    token="$(<"$file")"
  fi
  if [[ "$token" =~ ^[0-9a-f]{64}$ ]]; then
    chmod 600 -- "$file"
    return 0
  fi
  # Only through a pipe: the token never appears in argv or the environment.
  token="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
  [[ "$token" =~ ^[0-9a-f]{64}$ ]] || die 'Could not generate the package runner token.'
  write_file_atomic "$file" 600 "$token"
  TOKEN_CHANGED=1
}

print_summary() {
  local mode="$1" gpu="$2" cmd
  cmd="$(script_cmd)"
  printf '\n'
  if [[ "$mode" == install ]]; then
    info 'JupyterLab over Tailscale is installed and running.'
  else
    info 'JupyterLab over Tailscale is updated and running.'
  fi
  printf '  JupyterLab:  http://%s:%s/lab\n' "$TS_IP" "$JUPYTER_PORT"
  if [[ "$STATS_ENABLED" == 1 ]]; then
    printf '  Statistics:  http://%s:%s/\n' "$TS_IP" "$STATS_PORT"
    printf '  Packages:    http://%s:%s/dependencies\n' "$TS_IP" "$STATS_PORT"
    printf '  Stats user:  %s\n' "$STATS_USER"
  else
    printf '  Statistics:  disabled (the Dependencies page too)\n'
  fi
  if [[ -t 1 ]]; then
    printf '  Password:    %s\n' "$JUPYTER_PASSWORD"
  else
    printf '  Password:    (hidden; JUPYTER_PASSWORD in the settings file)\n'
  fi
  printf '  Workspace:   %s\n' "$WORKSPACE_DIR"
  printf '  Settings:    %s\n' "$SETTINGS_FILE"
  printf '  Theme:       %s\n' "$THEME"
  printf '  GPU:         %s\n' "$([[ "$gpu" == 1 ]] && printf 'enabled' || printf 'not used')"
  if [[ "$JUPYTER_PASSWORD" == "$DEFAULT_PASSWORD" ]]; then
    printf '\n'
    warn "You are using the default password. Change JUPYTER_PASSWORD in $SETTINGS_FILE and run: $cmd update"
  fi
  printf '\nNext:\n'
  printf '  %s status              containers, addresses, firewall\n' "$cmd"
  printf '  %s logs                follow the logs (Ctrl+C to leave)\n' "$cmd"
  printf '  %s stop | start        stop or start the containers\n' "$cmd"
  printf '  %s update              apply changed settings, refresh base images\n' "$cmd"
  printf '  %s uninstall           remove everything except the workspace and settings\n' "$cmd"
}

deploy() {
  local mode="$1" gpu=0 ports
  local build_args=(build)

  refuse_root
  validate_gpu_mode
  require_docker
  require_stack
  if [[ "$mode" == install ]] && is_installed; then
    info "Already installed in $APP_DIR; re-applying the current settings ('update' also refreshes the base image)."
  fi

  ensure_settings_file
  load_settings
  require_valid_settings
  read -r -a ports <<<"$(wanted_ports)"

  wait_for_tailscale 30
  info "Tailscale IPv4: $TS_IP"

  ensure_workspace
  legacy_cleanup
  check_ports "$TS_IP" "${ports[@]}"

  info "Copying $STACK_DIR to $APP_DIR"
  sync_stack
  ensure_runner_token

  if gpu_requested; then
    gpu=1
    info 'NVIDIA GPU available: both containers get access to it.'
  fi
  write_runtime_env "$gpu"

  if [[ "$mode" == update ]]; then
    build_args+=(--pull)
    info 'Building images (refreshing the base image)...'
  else
    info 'Building images...'
  fi
  compose "${build_args[@]}" || die 'docker compose build failed; see the output above.'

  if [[ "$gpu" == 1 ]] && ! gpu_probe; then
    warn 'Continuing without GPU access (set JLT_GPU=off to skip the check, or fix the NVIDIA container toolkit).'
    gpu=0
    write_runtime_env 0
  fi

  ensure_secrets

  if [[ "$STATS_ENABLED" != 1 ]]; then
    # A profile-disabled service is neither stopped by up nor treated as an orphan;
    # naming the services here enables their profile just for this command. The package
    # runner (deps) belongs to the dashboard; installed packages stay in their volume and
    # notebooks keep using them.
    info 'Statistics disabled: removing the stats and deps containers if they exist.'
    compose rm -s -f stats deps || warn 'Could not remove the stats and deps containers.'
  fi

  local up_args=(up -d --remove-orphans --wait --wait-timeout 300)
  if [[ "$HASH_CHANGED" == 1 || "$TOKEN_CHANGED" == 1 ]]; then
    # Secret file contents are not part of Compose's config hash, so a new password or runner
    # token would otherwise keep the old containers (still bound to the old files).
    up_args+=(--force-recreate)
  fi
  info 'Starting containers and waiting until they are healthy...'
  compose "${up_args[@]}" || compose_failure 'The containers did not become healthy.'

  maybe_root_step "$TS_IP" "${ports[@]}"

  docker image prune -f --filter dangling=true \
    --filter "label=org.opencontainers.image.vendor=${PROJECT}" >/dev/null 2>&1 || true

  print_summary "$mode" "$gpu"
}

# ---------------------------------------------------------------------------------------------
# Deployed state (runtime .env) and drift against the settings file
# ---------------------------------------------------------------------------------------------

load_runtime_env() {
  local file="$APP_DIR/.env"
  declare -gA RUNTIME_ENV=()
  read_env_file "$file" RUNTIME_ENV
  ((${#ENV_PARSE_ERRORS[@]} == 0)) || die "$file is damaged (${ENV_PARSE_ERRORS[0]}). Run: $(script_cmd) update"
  DEPLOYED_TS_IP="${RUNTIME_ENV[TS_IP]:-}"
  DEPLOYED_JUPYTER_PORT="${RUNTIME_ENV[JUPYTER_PORT]:-}"
  DEPLOYED_STATS_PORT="${RUNTIME_ENV[STATS_PORT]:-}"
  DEPLOYED_STATS_USER="${RUNTIME_ENV[STATS_USER]:-}"
  # A runtime .env from before the THEME setting: compose.yaml's default applies.
  DEPLOYED_THEME="${RUNTIME_ENV[THEME]:-$DEFAULT_THEME}"
  DEPLOYED_WORKSPACE_DIR="${RUNTIME_ENV[WORKSPACE_DIR]:-}"
  DEPLOYED_STATS_ENABLED=0
  if [[ ",${RUNTIME_ENV[COMPOSE_PROFILES]:-}," == *,stats,* ]]; then
    DEPLOYED_STATS_ENABLED=1
  fi
  DEPLOYED_GPU=0
  if [[ "${RUNTIME_ENV[COMPOSE_FILE]:-}" == *compose.gpu.yaml* ]]; then
    DEPLOYED_GPU=1
  fi
  if ! is_valid_port "$DEPLOYED_JUPYTER_PORT" || ! is_valid_port "$DEPLOYED_STATS_PORT"; then
    die "$file has invalid ports. Run: $(script_cmd) update"
  fi
}

deployed_ports() {
  if [[ "$DEPLOYED_STATS_ENABLED" == 1 ]]; then
    printf '%s %s\n' "$DEPLOYED_JUPYTER_PORT" "$DEPLOYED_STATS_PORT"
  else
    printf '%s\n' "$DEPLOYED_JUPYTER_PORT"
  fi
}

# Rewrites only the TS_IP line of the runtime .env.
set_runtime_ts_ip() {
  local file="$APP_DIR/.env" line content='' found=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == TS_IP=* ]]; then
      line="TS_IP='$1'"
      found=1
    fi
    content+="$line"$'\n'
  done <"$file"
  ((found)) || content+="TS_IP='$1'"$'\n'
  write_file_atomic "$file" 600 "$content"
}

settings_drift_warning() {
  local differences=() problem current='' message item
  load_settings
  if ((${#SETTINGS_PROBLEMS[@]} > 0)); then
    message="$SETTINGS_FILE has problems; 'update' will refuse it until they are fixed:"
    for problem in "${SETTINGS_PROBLEMS[@]}"; do
      message+=$'\n'"  - ${problem}"
    done
    warn "$message"
    return 0
  fi
  [[ "$JUPYTER_PORT" == "$DEPLOYED_JUPYTER_PORT" ]] ||
    differences+=("JUPYTER_PORT ${DEPLOYED_JUPYTER_PORT} -> ${JUPYTER_PORT}")
  [[ "$STATS_ENABLED" == "$DEPLOYED_STATS_ENABLED" ]] ||
    differences+=("STATS_ENABLED ${DEPLOYED_STATS_ENABLED} -> ${STATS_ENABLED}")
  [[ "$STATS_PORT" == "$DEPLOYED_STATS_PORT" ]] ||
    differences+=("STATS_PORT ${DEPLOYED_STATS_PORT} -> ${STATS_PORT}")
  [[ "$STATS_USER" == "$DEPLOYED_STATS_USER" ]] ||
    differences+=("STATS_USER ${DEPLOYED_STATS_USER} -> ${STATS_USER}")
  [[ "$THEME" == "$DEPLOYED_THEME" ]] ||
    differences+=("THEME ${DEPLOYED_THEME} -> ${THEME}")
  if [[ -r "$APP_DIR/secrets/jupyter_password" ]]; then
    current="$(<"$APP_DIR/secrets/jupyter_password")"
  fi
  [[ "$current" == "$JUPYTER_PASSWORD" ]] || differences+=('JUPYTER_PASSWORD changed')
  ((${#differences[@]} > 0)) || return 0
  message="Settings in $SETTINGS_FILE differ from what is deployed:"
  for item in "${differences[@]}"; do
    message+=$'\n'"  - ${item}"
  done
  message+=$'\n'"Run '$(script_cmd) update' to apply them."
  warn "$message"
}

# ---------------------------------------------------------------------------------------------
# Root step (from the unprivileged commands)
# ---------------------------------------------------------------------------------------------

# Which firewall is active, detected without root: ufw via its config file, firewalld via
# systemd. Prints ufw, firewalld or none.
active_firewall() {
  if [[ -r "$UFW_CONF" ]] && grep -Eqs '^[[:space:]]*ENABLED[[:space:]]*=[[:space:]]*"?yes"?[[:space:]]*$' -- "$UFW_CONF"; then
    printf 'ufw\n'
  elif command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet firewalld 2>/dev/null; then
    printf 'firewalld\n'
  else
    printf 'none\n'
  fi
}

read_root_state() {
  declare -gA ROOT_STATE=()
  ENV_PARSE_ERRORS=()
  if [[ -f "$ROOT_STATE_FILE" && -r "$ROOT_STATE_FILE" ]]; then
    read_env_file "$ROOT_STATE_FILE" ROOT_STATE
  fi
}

nonlocal_bind_value() {
  if [[ -r "$NONLOCAL_BIND_PROC" ]]; then
    printf '%s\n' "$(<"$NONLOCAL_BIND_PROC")"
  else
    printf 'unknown\n'
  fi
}

# The root step is skipped when nothing would change: the sysctl is in place and either no
# firewall is active or the recorded rules already match the wanted address and ports.
root_step_needed() {
  local ts_ip="$1" wanted firewall
  shift
  wanted="$(sorted_ports "$@")"
  [[ -f "$SYSCTL_FILE" ]] || return 0
  [[ "$(nonlocal_bind_value)" == 1 ]] || return 0
  firewall="$(active_firewall)"
  [[ "$firewall" != none ]] || return 1
  read_root_state
  # FIREWALL is compared too, so enabling ufw after host-setup ran still gets rules applied.
  if [[ "${ROOT_STATE[TS_IP]:-}" == "$ts_ip" && "${ROOT_STATE[PORTS]:-}" == "$wanted" &&
    "${ROOT_STATE[FIREWALL]:-}" == "$firewall" ]]; then
    return 1
  fi
  return 0
}

# Runs this script as root with the given helper arguments. From a terminal sudo may prompt;
# with cached credentials sudo -n is used; otherwise (e.g. the GUI builder) one machine-readable
# line tells the caller to run the step itself (pkexec). Never fatal.
run_root() {
  local runner=("$SCRIPT_PATH") hint
  [[ -x "$SCRIPT_PATH" ]] || runner=(/bin/bash "$SCRIPT_PATH")
  hint="sudo $(printf '%q ' "${runner[@]}")$*"
  if command -v sudo >/dev/null 2>&1 && [[ -t 0 && -t 1 ]]; then
    if [[ "$1" == host-setup ]]; then
      info "Root is needed once for host settings: net.ipv4.ip_nonlocal_bind=1 (so Docker can publish on the Tailscale IP even when tailscaled starts after Docker at boot) and, when a firewall is active, rules restricting the ports to ${TAILSCALE_IFACE}. sudo may ask for your password."
    else
      info 'Root is needed to remove the host settings made by host-setup (sysctl file, firewall rules). sudo may ask for your password.'
    fi
    if sudo -- "${runner[@]}" "$@"; then
      return 0
    fi
    warn "The root step did not complete. Run in a terminal: $hint"
  elif command -v sudo >/dev/null 2>&1 && sudo -n true >/dev/null 2>&1; then
    if sudo -n -- "${runner[@]}" "$@"; then
      return 0
    fi
    warn "The root step did not complete. Run in a terminal: $hint"
  else
    printf 'ROOT_STEP_REQUIRED: %s\n' "$*"
    printf 'Run in a terminal: %s\n' "$hint"
  fi
  return 0
}

maybe_root_step() {
  local ts_ip="$1"
  shift
  if root_step_needed "$ts_ip" "$@"; then
    run_root host-setup "$ts_ip" "$@"
  fi
}

# ---------------------------------------------------------------------------------------------
# Root helpers: host-setup / host-teardown
# ---------------------------------------------------------------------------------------------

root_prepare() {
  PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
  export PATH LC_ALL=C
  IFS=$' \t\n'
  umask 022
  if [[ "$EUID" -ne 0 ]]; then
    die "$1 must run as root, e.g.: sudo $(printf '%q' "$SCRIPT_PATH") $1 ..."
  fi
}

# ufw's own status (root only). Captured first: grep -q in a pipeline could SIGPIPE ufw
# and fail the pipeline under pipefail.
ufw_is_active() {
  local status
  command -v ufw >/dev/null 2>&1 || return 1
  status="$(ufw status 2>/dev/null)" || return 1
  [[ "$status" =~ (^|$'\n')'Status: active' ]]
}

firewalld_is_running() {
  command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1
}

# Prints "<ip> <port>" for each DROP rule in our after.rules block.
after_rules_block_pairs() {
  local line in_block=0 re='--ctorigdst ([0-9.]+) --ctorigdstport ([0-9]+) '
  [[ -f "$UFW_AFTER_RULES" ]] || return 0
  while IFS= read -r line; do
    if [[ "$line" == "$UFW_BLOCK_BEGIN" ]]; then
      in_block=1
    elif [[ "$line" == "$UFW_BLOCK_END" ]]; then
      in_block=0
    elif ((in_block)) && [[ "$line" =~ $re ]]; then
      printf '%s %s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
    fi
  done <"$UFW_AFTER_RULES"
}

# Replaces our marked block at the end of after.rules wholesale; without ports it only removes
# the block. Why DOCKER-USER: Docker-published ports are DNATed and take the FORWARD path, so
# ufw's INPUT rules never see them; DOCKER-USER is the one chain Docker leaves to the admin, and
# conntrack's original destination is needed because FORWARD only sees the container address.
# Declaring :DOCKER-USER in a ufw rules file makes every ufw reload flush and refill that chain.
after_rules_write_block() {
  local ts_ip="$1" begins ends content='' line in_block=0 port
  shift
  if [[ ! -f "$UFW_AFTER_RULES" ]]; then
    (($# == 0)) || warn "$UFW_AFTER_RULES not found; cannot add the DOCKER-USER rules."
    return 0
  fi
  begins=0
  ends=0
  while IFS= read -r line; do
    [[ "$line" == "$UFW_BLOCK_BEGIN" ]] && begins=$((begins + 1))
    [[ "$line" == "$UFW_BLOCK_END" ]] && ends=$((ends + 1))
  done <"$UFW_AFTER_RULES"
  if ((begins != ends)); then
    die "$UFW_AFTER_RULES has unbalanced '$UFW_BLOCK_BEGIN'/'$UFW_BLOCK_END' markers; fix the file by hand."
  fi
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == "$UFW_BLOCK_BEGIN" ]]; then
      in_block=1
    elif [[ "$line" == "$UFW_BLOCK_END" ]]; then
      in_block=0
    elif ((!in_block)); then
      content+="$line"$'\n'
    fi
  done <"$UFW_AFTER_RULES"
  if (($# > 0)); then
    content+="${UFW_BLOCK_BEGIN}"$'\n*filter\n:DOCKER-USER - [0:0]\n'
    for port in "$@"; do
      content+="-A DOCKER-USER ! -i ${TAILSCALE_IFACE} -p tcp -m conntrack --ctorigdst ${ts_ip} --ctorigdstport ${port} -j DROP"$'\n'
    done
    content+=$'COMMIT\n'"${UFW_BLOCK_END}"$'\n'
  fi
  write_file_atomic "$UFW_AFTER_RULES" 640 "$content" root:root
}

# Removes live DOCKER-USER DROP rules for "<ip> <port>" pairs read from stdin (ufw does not
# flush the chain once our block is gone).
delete_live_docker_user_rules() {
  local ip port tries
  while read -r ip port; do
    [[ -n "${port:-}" ]] || continue
    command -v iptables >/dev/null 2>&1 || continue
    # Delete repeatedly in case the rule was added more than once (bounded, just in case).
    tries=0
    while ((tries < 8)) && iptables -w -D DOCKER-USER ! -i "$TAILSCALE_IFACE" -p tcp -m conntrack \
      --ctorigdst "$ip" --ctorigdstport "$port" -j DROP >/dev/null 2>&1; do
      tries=$((tries + 1))
    done
  done
}

ufw_delete_port_rules() {
  local port
  for port in "$@"; do
    ufw delete allow in on "$TAILSCALE_IFACE" to any port "$port" proto tcp
  done
}

ufw_apply() {
  local ts_ip="$1" port stale=()
  local -a previous wanted
  read -r -a previous <<<"$2"
  read -r -a wanted <<<"$3"
  for port in "${previous[@]}"; do
    list_contains "${wanted[*]}" "$port" || stale+=("$port")
  done
  if ((${#stale[@]} > 0)); then
    ufw_delete_port_rules "${stale[@]}"
  fi
  # These allow rules only matter for host sockets; they document intent in 'ufw status'.
  for port in "${wanted[@]}"; do
    ufw allow in on "$TAILSCALE_IFACE" to any port "$port" proto tcp comment "$PROJECT"
  done
  after_rules_write_block "$ts_ip" "${wanted[@]}"
  ufw reload
  printf 'ufw: TCP %s allowed on %s; DOCKER-USER drops %s:<port> arriving on any other interface.\n' \
    "${wanted[*]}" "$TAILSCALE_IFACE" "$ts_ip"
}

firewalld_apply() {
  local previous_zone="$1" wanted_ports="$3" zone zones port
  local -a previous wanted
  read -r -a previous <<<"$2"
  read -r -a wanted <<<"$3"
  zone="$(firewall-cmd --get-zone-of-interface="$TAILSCALE_IFACE" 2>/dev/null)" || zone=''
  if [[ -z "$zone" || "$zone" == 'no zone' ]]; then
    zone="$FIREWALLD_ZONE"
    zones="$(firewall-cmd --permanent --get-zones)"
    list_contains "$zones" "$zone" || firewall-cmd --permanent --new-zone="$zone"
    firewall-cmd --permanent --zone="$zone" --add-interface="$TAILSCALE_IFACE"
  fi
  for port in "${previous[@]}"; do
    if [[ -n "$previous_zone" ]] && { [[ "$previous_zone" != "$zone" ]] || ! list_contains "$wanted_ports" "$port"; }; then
      firewall-cmd --permanent --zone="$previous_zone" --remove-port="${port}/tcp"
    fi
  done
  for port in "${wanted[@]}"; do
    firewall-cmd --permanent --zone="$zone" --add-port="${port}/tcp"
  done
  firewall-cmd --reload
  FIREWALLD_ZONE_USED="$zone"
  printf 'firewalld: TCP %s opened in zone %s (%s).\n' "$wanted_ports" "$zone" "$TAILSCALE_IFACE"
  printf 'Note: firewalld zones do not filter Docker-published ports; the password is the access control.\n'
}

cmd_host_setup() {
  root_prepare host-setup
  if (($# < 2 || $# > 3)); then
    die 'Usage: host-setup <tailscale-ipv4> <port> [<port>]'
  fi
  local ts_ip="$1" port ports previous_ports previous_firewall previous_bind previous_zone firewall='none'
  local state port_word
  local -a recorded stale_ports
  shift
  is_tailscale_ipv4 "$ts_ip" || die "host-setup: $(printf '%q' "$ts_ip") is not an IPv4 address inside 100.64.0.0/10"
  for port in "$@"; do
    is_valid_port "$port" || die "host-setup: $(printf '%q' "$port") is not a port from 1024 to 65535"
  done
  if (($# == 2)) && [[ "$1" == "$2" ]]; then
    die 'host-setup: the two ports must be different'
  fi
  ports="$(sorted_ports "$@")"

  read_root_state
  previous_ports=''
  read -r -a recorded <<<"${ROOT_STATE[PORTS]:-}"
  for port_word in "${recorded[@]}"; do
    is_valid_port "$port_word" && previous_ports+="${previous_ports:+ }$port_word"
  done
  previous_firewall="${ROOT_STATE[FIREWALL]:-none}"
  previous_zone="${ROOT_STATE[FIREWALLD_ZONE]:-}"
  previous_bind="${ROOT_STATE[PREV_NONLOCAL_BIND]:-}"

  # a) Boot race: see the comment written into the sysctl file.
  if [[ ! "$previous_bind" =~ ^[01]$ ]]; then
    if [[ -f "$SYSCTL_FILE" ]]; then
      previous_bind=0 # our file exists but the state was lost: assume the kernel default
    else
      previous_bind="$(nonlocal_bind_value)"
      [[ "$previous_bind" =~ ^[01]$ ]] || previous_bind=0
    fi
  fi
  install -d -m 755 /etc/sysctl.d
  write_file_atomic "$SYSCTL_FILE" 644 "$(printf '%s\n' \
    '# Written by setup-jupyterlab-tailscale.sh host-setup; removed by host-teardown/uninstall.' \
    '# Docker publishes JupyterLab only on the Tailscale IPv4. At boot dockerd may start before' \
    '# tailscaled has assigned that address; the bind then fails ("cannot assign requested' \
    '# address") and Docker never retries. Allowing non-local binds makes the publish succeed' \
    '# regardless of timing, and traffic flows as soon as tailscale0 gets the address.' \
    'net.ipv4.ip_nonlocal_bind = 1')"$'\n' root:root
  sysctl -q -w net.ipv4.ip_nonlocal_bind=1
  printf 'sysctl: net.ipv4.ip_nonlocal_bind=1 (persisted in %s)\n' "$SYSCTL_FILE"

  # b-d) Firewall, only when one is active.
  if ufw_is_active; then
    firewall='ufw'
    ufw_apply "$ts_ip" "$previous_ports" "$ports"
  elif firewalld_is_running; then
    firewall='firewalld'
    FIREWALLD_ZONE_USED=''
    firewalld_apply "$previous_zone" "$previous_ports" "$ports"
  else
    printf 'No active firewall (ufw/firewalld): nothing to configure. The services listen only on %s.\n' "$ts_ip"
  fi
  if [[ "$previous_firewall" == ufw && "$firewall" != ufw ]] && command -v ufw >/dev/null 2>&1; then
    # ufw was switched off since the last run: drop our now-stale rules so re-enabling ufw
    # does not resurrect old ports.
    if [[ -n "$previous_ports" ]]; then
      read -r -a stale_ports <<<"$previous_ports"
      ufw_delete_port_rules "${stale_ports[@]}"
    fi
    after_rules_block_pairs | delete_live_docker_user_rules
    after_rules_write_block "$ts_ip"
  fi

  # e) State for the unprivileged commands and for teardown.
  state="$(printf '%s\n' \
    '# Managed by setup-jupyterlab-tailscale.sh host-setup; read by the unprivileged commands.' \
    "TS_IP='${ts_ip}'" \
    "PORTS='${ports}'" \
    "FIREWALL='${firewall}'" \
    "PREV_NONLOCAL_BIND='${previous_bind}'")"$'\n'
  if [[ "$firewall" == firewalld && -n "${FIREWALLD_ZONE_USED:-}" ]]; then
    state+="FIREWALLD_ZONE='${FIREWALLD_ZONE_USED}'"$'\n'
  fi
  install -d -m 755 "$ROOT_STATE_DIR"
  chmod 755 "$ROOT_STATE_DIR"
  write_file_atomic "$ROOT_STATE_FILE" 644 "$state" root:root
  printf 'host-setup done: %s, ports %s, firewall %s\n' "$ts_ip" "$ports" "$firewall"
}

cmd_host_teardown() {
  root_prepare host-teardown
  (($# == 0)) || die 'Usage: host-teardown'
  local port firewall zone previous_bind removed=0
  local -a recorded port_list=()

  read_root_state
  # Root-owned, but still validated before anything reaches ufw/firewall-cmd.
  read -r -a recorded <<<"${ROOT_STATE[PORTS]:-}"
  for port in "${recorded[@]}"; do
    if is_valid_port "$port"; then
      port_list+=("$port")
    fi
  done
  firewall="${ROOT_STATE[FIREWALL]:-none}"
  zone="${ROOT_STATE[FIREWALLD_ZONE]:-}"
  previous_bind="${ROOT_STATE[PREV_NONLOCAL_BIND]:-}"

  # ufw delete works while ufw is inactive too (it edits user.rules).
  if command -v ufw >/dev/null 2>&1 && ((${#port_list[@]} > 0)); then
    ufw_delete_port_rules "${port_list[@]}"
    removed=1
  fi
  if [[ -f "$UFW_AFTER_RULES" ]] && grep -qxF -- "$UFW_BLOCK_BEGIN" "$UFW_AFTER_RULES"; then
    after_rules_block_pairs | delete_live_docker_user_rules
    after_rules_write_block ''
    if ufw_is_active; then
      ufw reload
    fi
    printf 'Removed the DOCKER-USER block from %s\n' "$UFW_AFTER_RULES"
    removed=1
  fi
  if [[ "$firewall" == firewalld && -n "$zone" ]] && firewalld_is_running; then
    for port in "${port_list[@]}"; do
      firewall-cmd --permanent --zone="$zone" --remove-port="${port}/tcp"
    done
    firewall-cmd --reload
    removed=1
  fi
  if [[ -f "$SYSCTL_FILE" ]]; then
    rm -f -- "$SYSCTL_FILE"
    printf 'Removed %s\n' "$SYSCTL_FILE"
    removed=1
    if [[ ! "$previous_bind" =~ ^[01]$ ]]; then
      printf 'Previous ip_nonlocal_bind value unknown; the current value stays until reboot.\n'
    fi
  fi
  if [[ "$previous_bind" =~ ^[01]$ ]]; then
    sysctl -q -w "net.ipv4.ip_nonlocal_bind=${previous_bind}"
    printf 'sysctl: net.ipv4.ip_nonlocal_bind restored to %s\n' "$previous_bind"
  fi
  if [[ -d "$ROOT_STATE_DIR" ]]; then
    rm -rf -- "$ROOT_STATE_DIR"
    printf 'Removed %s\n' "$ROOT_STATE_DIR"
    removed=1
  fi
  if ((!removed)); then
    printf 'host-teardown: nothing to remove.\n'
  else
    printf 'host-teardown done.\n'
  fi
}

# ---------------------------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------------------------

acquire_lock() {
  if ! command -v flock >/dev/null 2>&1; then
    warn 'flock not found; running without protection against concurrent runs.'
    return 0
  fi
  exec {LOCK_FD}>>"$LOCK_FILE" || die "Cannot open the lock file $LOCK_FILE"
  if ! flock -n "$LOCK_FD"; then
    die "Another run of this script is still in progress (lock: $LOCK_FILE). Please wait for it to finish, then try again."
  fi
}

# ---------------------------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------------------------

cmd_install() {
  deploy install
}

cmd_update() {
  deploy update
}

cmd_start() {
  local ports
  refuse_root
  require_installed
  require_docker
  load_runtime_env
  wait_for_tailscale 60
  if [[ "$TS_IP" != "$DEPLOYED_TS_IP" ]]; then
    set_runtime_ts_ip "$TS_IP"
    info "Tailscale IPv4 changed from ${DEPLOYED_TS_IP:-<none>} to ${TS_IP}; updated $APP_DIR/.env (the containers are recreated on the new address)."
    DEPLOYED_TS_IP="$TS_IP"
  fi
  read -r -a ports <<<"$(deployed_ports)"
  maybe_root_step "$TS_IP" "${ports[@]}"
  info 'Starting containers and waiting until they are healthy...'
  compose up -d --remove-orphans --wait --wait-timeout 180 ||
    compose_failure 'The containers did not become healthy.'
  info 'Running:'
  printf '  JupyterLab:  http://%s:%s/lab\n' "$TS_IP" "$DEPLOYED_JUPYTER_PORT"
  if [[ "$DEPLOYED_STATS_ENABLED" == 1 ]]; then
    printf '  Statistics:  http://%s:%s/  (user %s)\n' "$TS_IP" "$DEPLOYED_STATS_PORT" "$DEPLOYED_STATS_USER"
    printf '  Packages:    http://%s:%s/dependencies\n' "$TS_IP" "$DEPLOYED_STATS_PORT"
  fi
  settings_drift_warning
}

cmd_stop() {
  refuse_root
  require_installed
  require_docker
  info 'Stopping containers...'
  # --profile stats also stops stats and deps containers left over from before statistics
  # were disabled. A running package install in deps is cancelled and shown as interrupted.
  compose --profile stats stop || die 'docker compose stop failed; see the output above.'
}

cmd_restart() {
  # stop + start instead of 'compose restart', so a changed Tailscale IP is picked up.
  cmd_stop
  cmd_start
}

# Only the published ports of a `compose ps` Ports column: a port the image merely EXPOSEs
# (8888/tcp on the deps container, which runs the JupyterLab image) is not reachable.
published_ports() {
  local part out=''
  local -a parts=()
  IFS=',' read -r -a parts <<<"$1"
  for part in "${parts[@]}"; do
    part="${part# }"
    if [[ "$part" == *'->'* ]]; then
      out+="${out:+, }${part}"
    fi
  done
  printf '%s\n' "${out:--}"
}

# Name shown in the status table: the deps service is not self-explanatory.
service_label() {
  case "$1" in
    deps) printf 'deps (package runner)\n' ;;
    *) printf '%s\n' "$1" ;;
  esac
}

show_containers() {
  local output name service state health ports error
  local -a expected=(jupyterlab)
  local -A seen=()
  # --profile stats: also list stats/deps containers left over after statistics were disabled.
  if ! output="$(compose --profile stats ps -a --format '{{.Name}}|{{.Service}}|{{.State}}|{{.Health}}|{{.Ports}}' 2>/dev/null)"; then
    warn "'docker compose ps' failed in $APP_DIR."
    return 0
  fi
  printf '  %-22s %-11s %-10s %s\n' SERVICE STATE HEALTH PORTS
  while IFS='|' read -r name service state health ports; do
    [[ -n "$name" ]] || continue
    seen["$service"]=1
    printf '  %-22s %-11s %-10s %s\n' "$(service_label "$service")" "$state" "${health:--}" "$(published_ports "$ports")"
    if [[ "$state" != running ]]; then
      error="$(docker inspect --format '{{.State.Error}}' "$name" 2>/dev/null)" || error=''
      if [[ -n "$error" ]]; then
        printf '    error: %s\n' "$error"
        printf "    -> run '%s start' (Docker never retries a failed port bind at boot)\n" "$(script_cmd)"
      fi
    fi
  done <<<"$output"
  [[ "$DEPLOYED_STATS_ENABLED" == 1 ]] && expected+=(deps stats)
  for service in "${expected[@]}"; do
    if [[ -z "${seen[$service]:-}" ]]; then
      printf "  %-22s no container -> run '%s start'\n" "$(service_label "$service")" "$(script_cmd)"
    fi
  done
}

cmd_status() {
  local mode firewall bind live_ip='' ports
  refuse_root
  if [[ -f "$SETTINGS_FILE" ]]; then
    mode="$(stat -L -c '%a' -- "$SETTINGS_FILE")"
    printf 'Settings file:     %s (mode %s)\n' "$SETTINGS_FILE" "$mode"
  else
    printf 'Settings file:     %s (missing; install/update create it with defaults)\n' "$SETTINGS_FILE"
  fi
  if ! is_installed; then
    printf 'Deployment:        not installed (%s)\n' "$APP_DIR"
    printf "Run: %s install\n" "$(script_cmd)"
    exit 3
  fi
  load_runtime_env
  printf 'App dir:           %s\n' "$APP_DIR"
  printf 'Deployed:          JupyterLab port %s, statistics %s, GPU override %s\n' \
    "$DEPLOYED_JUPYTER_PORT" \
    "$([[ "$DEPLOYED_STATS_ENABLED" == 1 ]] && printf 'on (port %s, user %s)' "$DEPLOYED_STATS_PORT" "$DEPLOYED_STATS_USER" || printf 'off')" \
    "$([[ "$DEPLOYED_GPU" == 1 ]] && printf 'on' || printf 'off')"
  printf 'Workspace:         %s\n' "$DEPLOYED_WORKSPACE_DIR"
  printf 'Theme:             %s\n' "$DEPLOYED_THEME"

  if probe_tailscale_ipv4; then
    live_ip="$TS_PROBE_IP"
    if [[ "$live_ip" == "$DEPLOYED_TS_IP" ]]; then
      printf 'Tailscale IPv4:    %s (matches the deployment)\n' "$live_ip"
    else
      printf 'Tailscale IPv4:    live %s, deployed %s\n' "$live_ip" "${DEPLOYED_TS_IP:-<none>}"
      warn "The Tailscale address changed; run '$(script_cmd) restart' to publish on the new one."
    fi
  else
    printf 'Tailscale IPv4:    unavailable, deployed %s\n' "${DEPLOYED_TS_IP:-<none>}"
    printf '                   %s\n' "$TS_PROBE_ERROR"
  fi

  printf 'Containers:\n'
  if docker_usable; then
    show_containers
  else
    printf '  Docker is not reachable (daemon down, or not in the docker group yet).\n'
  fi

  printf 'URLs:\n'
  printf '  JupyterLab:  http://%s:%s/lab\n' "${DEPLOYED_TS_IP:-<ts-ip>}" "$DEPLOYED_JUPYTER_PORT"
  if [[ "$DEPLOYED_STATS_ENABLED" == 1 ]]; then
    printf '  Statistics:  http://%s:%s/\n' "${DEPLOYED_TS_IP:-<ts-ip>}" "$DEPLOYED_STATS_PORT"
    printf '  Packages:    http://%s:%s/dependencies\n' "${DEPLOYED_TS_IP:-<ts-ip>}" "$DEPLOYED_STATS_PORT"
  fi

  bind="$(nonlocal_bind_value)"
  printf 'Host:\n'
  printf '  ip_nonlocal_bind:  %s (%s %s)\n' "$bind" "$SYSCTL_FILE" \
    "$([[ -f "$SYSCTL_FILE" ]] && printf 'present' || printf 'missing')"
  firewall="$(active_firewall)"
  case "$firewall" in
    none) printf '  Firewall:          none active (ufw/firewalld); no rules are needed or applied\n' ;;
    *) printf '  Firewall:          %s active\n' "$firewall" ;;
  esac
  read_root_state
  if [[ -f "$ROOT_STATE_FILE" ]]; then
    printf '  Root state:        TS_IP=%s PORTS=%s FIREWALL=%s\n' \
      "${ROOT_STATE[TS_IP]:-?}" "${ROOT_STATE[PORTS]:-?}" "${ROOT_STATE[FIREWALL]:-?}"
  else
    printf '  Root state:        none (%s missing; host-setup has not run)\n' "$ROOT_STATE_FILE"
  fi
  read -r -a ports <<<"$(deployed_ports)"
  if root_step_needed "${live_ip:-$DEPLOYED_TS_IP}" "${ports[@]}"; then
    printf "  Root step needed:  yes -> sudo %s host-setup %s %s\n" \
      "$(printf '%q' "$SCRIPT_PATH")" "${live_ip:-$DEPLOYED_TS_IP}" "${ports[*]}"
  else
    printf '  Root step needed:  no\n'
  fi
  settings_drift_warning
}

cmd_logs() {
  local follow=1 arg
  local services=() args=(logs --tail 100)
  for arg in "$@"; do
    case "$arg" in
      --no-follow) follow=0 ;;
      -*) die "Unknown logs option: $arg (only --no-follow is supported)" ;;
      *)
        [[ "$arg" =~ ^[A-Za-z0-9._-]+$ ]] || die "Invalid service name: $(printf '%q' "$arg")"
        services+=("$arg")
        ;;
    esac
  done
  refuse_root
  require_installed
  require_docker
  if ((follow)); then
    args+=(-f)
  fi
  local rc=0
  compose "${args[@]}" "${services[@]}" || rc=$?
  # exit rather than return: Ctrl+C while following is normal, not an "unexpected failure".
  exit "$rc"
}

remove_project_resources() {
  local volumes image
  if is_installed && compose --profile stats down --remove-orphans --volumes --rmi all; then
    return 0
  fi
  info 'Removing the Compose project by name (app dir files missing or unusable)...'
  # With -p and no compose file Compose does not load (or interpolate) any model.
  (
    unset COMPOSE_FILE COMPOSE_PROFILES COMPOSE_PROJECT_NAME
    cd / && docker compose -p "$PROJECT" down --remove-orphans --volumes
  ) || warn "'docker compose -p $PROJECT down' failed."
  volumes="$(docker volume ls -q --filter "label=com.docker.compose.project=${PROJECT}" 2>/dev/null)" || volumes=''
  if [[ -n "$volumes" ]]; then
    # shellcheck disable=SC2086 # volume names never contain whitespace
    docker volume rm $volumes >/dev/null || warn 'Some volumes could not be removed.'
  fi
  for image in "$JUPYTER_IMAGE" "$STATS_IMAGE"; do
    if docker image inspect "$image" >/dev/null 2>&1; then
      docker image rm "$image" >/dev/null || warn "Could not remove image $image."
    fi
  done
}

cmd_uninstall() {
  local assume_yes=0 delete_workspace=0 arg answer=''
  for arg in "$@"; do
    case "$arg" in
      --yes | -y) assume_yes=1 ;;
      --delete-workspace) delete_workspace=1 ;;
      *) die "Unknown uninstall option: $arg (supported: --yes, --delete-workspace)" ;;
    esac
  done
  refuse_root
  if ((!assume_yes)); then
    if [[ ! -t 0 || ! -t 1 ]]; then
      die 'refusing to uninstall without confirmation: run it in a terminal or pass --yes.'
    fi
    printf 'This removes the containers, their images and volumes (JupyterLab state, extra packages),\n'
    printf 'the host settings made by host-setup and %s.\n' "$APP_DIR"
    printf 'Kept: the workspace %s and the settings file %s.\n' "$WORKSPACE_DIR" "$SETTINGS_FILE"
    printf "Type 'uninstall' to continue: "
    read -r answer || answer=''
    if [[ "$answer" != uninstall ]]; then
      printf 'Aborted; nothing was changed.\n'
      exit 1
    fi
  fi
  acquire_lock

  if docker_usable; then
    info 'Removing containers, images and volumes...'
    remove_project_resources
  elif [[ -d "$APP_DIR" ]]; then
    die "Docker is not reachable, so the containers cannot be removed. Start Docker and run uninstall again."
  else
    warn 'Docker is not reachable; skipping container cleanup (nothing appears to be deployed).'
  fi

  if [[ -e "$SYSCTL_FILE" || -e "$ROOT_STATE_FILE" ]]; then
    run_root host-teardown
  fi

  if [[ -e "$APP_DIR" ]]; then
    is_safe_to_delete "$APP_DIR" || die "Refusing to delete $APP_DIR"
    rm -rf -- "$APP_DIR"
    info "Removed $APP_DIR"
  fi
  legacy_cleanup

  if ((delete_workspace)) && [[ -d "$WORKSPACE_DIR" ]]; then
    answer='delete'
    if ((!assume_yes)); then
      printf "Type 'delete' to permanently delete %s and every notebook in it: " "$WORKSPACE_DIR"
      read -r answer || answer=''
    fi
    if [[ "$answer" == delete ]]; then
      is_safe_to_delete "$WORKSPACE_DIR" || die "Refusing to delete $WORKSPACE_DIR"
      rm -rf -- "$WORKSPACE_DIR"
      info "Deleted the workspace $WORKSPACE_DIR"
    else
      info "Workspace kept: $WORKSPACE_DIR"
    fi
  elif [[ -d "$WORKSPACE_DIR" ]]; then
    info "Workspace kept: $WORKSPACE_DIR"
  fi
  if [[ -f "$SETTINGS_FILE" ]]; then
    info "Settings file kept: $SETTINGS_FILE"
  fi
  info 'Uninstall complete.'
}

usage() {
  local cmd
  cmd="$(script_cmd)"
  cat <<EOF
JupyterLab over Tailscale: JupyterLab and a statistics dashboard in Docker,
published only on this machine's Tailscale IPv4 address.

Usage: $cmd <command> [options]

Commands:
  install                            Build and start the stack. Creates the settings file
                                     with defaults when it is missing.
  update                             Apply changed settings and refresh the base image
                                     (docker compose build --pull).
  start                              Start the containers; follows a changed Tailscale IP.
  stop                               Stop the containers.
  restart                            stop, then start.
  status                             Deployment, containers, addresses, firewall
                                     (exit 3 when not installed).
  logs [--no-follow] [SERVICE...]    Last 100 log lines, following unless --no-follow.
                                     Services: jupyterlab, stats, deps (package runner).
  uninstall [--yes] [--delete-workspace]
                                     Remove containers, images, volumes, host settings and
                                     the app dir. Keeps the workspace unless
                                     --delete-workspace, and always keeps the settings file.
  help                               Show this help.

Root-only helpers (run through sudo by the commands above, or pkexec by the builder):
  host-setup <tailscale-ipv4> <port> [<port>]
                                     net.ipv4.ip_nonlocal_bind=1 and, when ufw/firewalld is
                                     active, rules limiting the ports to ${TAILSCALE_IFACE}.
  host-teardown                      Undo host-setup.

Settings file: $SETTINGS_FILE
  JUPYTER_PASSWORD, JUPYTER_PORT (8888), STATS_ENABLED (1), STATS_PORT (8889), STATS_USER (jupyter),
  THEME (amazing; a file name from stack/theme without .json)

Pages:
  http://<tailscale-ip>:<JUPYTER_PORT>/lab           JupyterLab
  http://<tailscale-ip>:<STATS_PORT>/                Statistics      (when STATS_ENABLED=1)
  http://<tailscale-ip>:<STATS_PORT>/dependencies    Kernel packages (when STATS_ENABLED=1)

Environment overrides:
  JLT_SETTINGS_FILE   settings file              (default: <script dir>/.env)
  JLT_APP_DIR         deployed stack directory   (default: ~/.local/share/${PROJECT})
  JLT_WORKSPACE_DIR   notebook workspace         (default: ~/jupyter-workspace)
  JLT_GPU             auto | on | off            (default: auto)
EOF
}

main() {
  local command="${1:-}"
  if (($# > 0)); then
    shift
  fi
  case "$command" in
    install | update | start | stop | restart | status)
      (($# == 0)) || die "'$command' takes no arguments. See: $(script_cmd) help"
      ;;
  esac
  case "$command" in
    install) acquire_lock && cmd_install ;;
    update) acquire_lock && cmd_update ;;
    start) acquire_lock && cmd_start ;;
    stop) acquire_lock && cmd_stop ;;
    restart) acquire_lock && cmd_restart ;;
    status) cmd_status ;;
    logs) cmd_logs "$@" ;;
    uninstall) cmd_uninstall "$@" ;;
    host-setup) cmd_host_setup "$@" ;;
    host-teardown) cmd_host_teardown "$@" ;;
    help | -h | --help) usage ;;
    '')
      usage >&2
      exit 2
      ;;
    *)
      printf 'Unknown command: %s\n\n' "$command" >&2
      usage >&2
      exit 2
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  trap 'on_unexpected_error "$?" "$LINENO"' ERR
  main "$@"
fi
