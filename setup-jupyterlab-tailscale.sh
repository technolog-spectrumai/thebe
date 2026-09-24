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
readonly DEFAULT_HTTPS='auto'
readonly DEFAULT_NVIDIA='auto'
readonly -a SETTINGS_KEYS=(JUPYTER_PASSWORD JUPYTER_PORT STATS_ENABLED STATS_PORT STATS_USER THEME HTTPS NVIDIA)
readonly JUPYTER_IMAGE="${PROJECT}/jupyterlab:local"
readonly STATS_IMAGE="${PROJECT}/stats:local"
readonly AI_IMAGE="${PROJECT}/ai:local"
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
# Certificates from 'tailscale cert' for the MagicDNS name, written by host-setup --cert.
readonly DEFAULT_TLS_DIR="${ROOT_STATE_DIR}/tls"
# Renew (root step) when fewer days are left. host-setup asks 'tailscale cert' for at least one
# day more (--min-validity): without it tailscaled hands back its cached certificate until its own
# background renewal has finished, and the root step would be asked for again. With it a
# successful step always leaves more than CERT_RENEW_DAYS, so the need ends.
readonly CERT_RENEW_DAYS=21
readonly CERT_MIN_VALIDITY="$(((CERT_RENEW_DAYS + 1) * 24))h"

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
# Optional Jupyter packages for the custom packages environment (the Dependencies page's venv).
REQUIREMENTS_FILE="$(absolute_path "${JLT_REQUIREMENTS_FILE:-${SCRIPT_DIR}/requirements.txt}")"
# AI settings with the API keys (JSON), next to the settings file. run.sh and the builder write it
# from config.yaml's ai: section, and delete it when AI is off.
AI_FILE="${SETTINGS_FILE%/*}/.ai.json"
# JLT_TLS_DIR is for tests only. The root helpers never take paths from the caller's
# environment, so they always use the default.
if [[ "$EUID" -eq 0 ]]; then
  TLS_DIR="$DEFAULT_TLS_DIR"
else
  TLS_DIR="$(absolute_path "${JLT_TLS_DIR:-$DEFAULT_TLS_DIR}")"
fi
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

# The certificate directory also goes into 'docker run --mount source=...', where a comma would
# start another option: plain characters only.
is_safe_tls_dir() {
  [[ "$1" =~ ^/[A-Za-z0-9._/-]+$ && "$1" != *//* && "/$1/" != */../* ]]
}

# A MagicDNS name as 'tailscale cert' accepts it: lower-case dot-separated labels (at least two),
# 253 characters at most. The letters are spelled out because a range such as [a-z] can match
# other characters in some locales, and the name ends up in root's argv and in file names.
is_valid_fqdn() {
  local chars='abcdefghijklmnopqrstuvwxyz0123456789'
  local label="[${chars}]([${chars}-]{0,61}[${chars}])?"
  local re="^${label}(\\.${label})+\$"
  ((${#1} <= 253)) && [[ "$1" =~ $re ]]
}

# A group id for the certificate files: decimal, not root's group, below the reserved -1.
is_valid_gid() {
  [[ "$1" =~ ^[1-9][0-9]{0,9}$ ]] && ((10#$1 <= 4294967294))
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
    "# HTTPS: auto serves HTTPS on the Tailscale name when MagicDNS and HTTPS certificates are" \
    "# enabled in the tailnet (certificate from 'tailscale cert'); off keeps plain HTTP." \
    '# NVIDIA: 1 expects an NVIDIA GPU (install/start fail when Docker cannot use it), 0 runs' \
    '# without one (no GPU check at all), auto uses the GPU when it works.' \
    "JUPYTER_PASSWORD='${DEFAULT_PASSWORD}'" \
    "JUPYTER_PORT='${DEFAULT_JUPYTER_PORT}'" \
    "STATS_ENABLED='${DEFAULT_STATS_ENABLED}'" \
    "STATS_PORT='${DEFAULT_STATS_PORT}'" \
    "STATS_USER='${DEFAULT_STATS_USER}'" \
    "THEME='${DEFAULT_THEME}'" \
    "HTTPS='${DEFAULT_HTTPS}'" \
    "NVIDIA='${DEFAULT_NVIDIA}'"
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
# STATS_USER, THEME, HTTPS, NVIDIA (auto, 1 or 0). Missing keys (or a missing file) fall back to
# the defaults. Problems are collected in SETTINGS_PROBLEMS; callers decide whether they are fatal.
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
  HTTPS="${SETTINGS_RAW[HTTPS]-$DEFAULT_HTTPS}"
  NVIDIA="${SETTINGS_RAW[NVIDIA]-$DEFAULT_NVIDIA}"

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

  case "$HTTPS" in
    auto | off) ;;
    *) SETTINGS_PROBLEMS+=("HTTPS must be one of: auto, off (got $(printf '%q' "$HTTPS"))") ;;
  esac

  if [[ "${NVIDIA,,}" == auto ]]; then
    NVIDIA='auto'
  elif bool="$(normalize_bool "$NVIDIA")"; then
    NVIDIA="$bool"
  else
    SETTINGS_PROBLEMS+=("NVIDIA must be auto, 1 or 0 (true/false, yes/no, on/off also work; got $(printf '%q' "$NVIDIA"))")
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
  local progress=() override=()
  [[ -t 1 ]] || progress=(--progress plain)
  # One start without the GPU override (start_check_gpu); the runtime .env keeps it.
  [[ -z "$COMPOSE_FILE_OVERRIDE" ]] || override=(env "COMPOSE_FILE=${COMPOSE_FILE_OVERRIDE}")
  (
    unset COMPOSE_PROJECT_NAME COMPOSE_FILE COMPOSE_PROFILES COMPOSE_ENV_FILES COMPOSE_PATH_SEPARATOR \
      TS_IP JUPYTER_PORT STATS_PORT STATS_USER THEME WORKSPACE_DIR HOST_NAME JLT_UID JLT_GID \
      HTTPS_MODE PUBLIC_HOST PUBLIC_SCHEME TLS TLS_NAME TLS_NOT_AFTER TLS_DIR
    # BuildKit attaches a timestamped provenance attestation by default, so even a fully cached
    # rebuild gets a new image ID and 'up' recreates both containers (killing running kernels).
    # These images are never pushed; without the attestation the ID only changes with the content.
    # (Compose 5.3 ignores 'provenance: false' in compose.yaml, hence the environment variable.)
    export BUILDX_NO_DEFAULT_ATTESTATIONS=1
    cd "$APP_DIR" && exec "${override[@]}" docker compose "${progress[@]}" "$@"
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
  for name in Dockerfile compose.yaml compose.gpu.yaml compose.tls.yaml; do
    [[ -f "$STACK_DIR/$name" ]] || die "Missing $STACK_DIR/$name; is the repository complete?"
  done
  for name in jupyter stats ai theme; do
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
# HTTPS by Tailscale name (MagicDNS + 'tailscale cert')
# ---------------------------------------------------------------------------------------------

# Reads 'tailscale status --json' on stdin inside the JupyterLab image (no host Python or jq).
# Prints name=<MagicDNS name>, cert_ok=1 when 'tailscale cert' can issue a certificate for it,
# and reason=<text> when either is missing; probe_error=1 when the answer says nothing definite
# (no usable output, tailscaled not Running at the moment). Characters outside a host name become
# "?", so a hostile DNSName can neither add lines nor pass the installer's own name check.
readonly TS_STATUS_PARSER='
import json, re, sys
try:
    status = json.load(sys.stdin)
except ValueError:
    status = None
if not isinstance(status, dict):
    print("reason=tailscale status --json gave no usable output")
    print("probe_error=1")
    sys.exit(0)
state = status.get("BackendState")
state = re.sub(r"[^A-Za-z]", "", state)[:24] if isinstance(state, str) else ""
tailnet = status.get("CurrentTailnet")
node = status.get("Self")
dns_name = node.get("DNSName") if isinstance(node, dict) else None
if state != "Running":
    print("reason=Tailscale is not running (state " + (state or "unknown") + ")")
    print("probe_error=1")
elif not isinstance(tailnet, dict) or tailnet.get("MagicDNSEnabled") is not True:
    print("reason=MagicDNS is disabled in the tailnet (Tailscale admin console, DNS page)")
elif not isinstance(dns_name, str) or not dns_name.strip("."):
    print("reason=Tailscale reports no MagicDNS name for this machine")
else:
    name = dns_name.lower().removesuffix(".")
    print("name=" + re.sub(r"[^a-z0-9.-]", "?", name)[:300])
    domains = status.get("CertDomains")
    domains = domains if isinstance(domains, list) else []
    if name in [d.lower().removesuffix(".") for d in domains if isinstance(d, str)]:
        print("cert_ok=1")
    else:
        print("reason=HTTPS certificates are disabled in the tailnet (Tailscale admin console, DNS page)")
'

# Checks <TLS_DIR>/<name>.crt and .key as the container user (argv[1]: the name). Prints valid=1|0,
# days_left, not_after (UTC, ISO 8601) and problem. load_cert_chain proves both files are
# readable and belong together, exactly as JupyterLab and uvicorn will load them.
readonly CERT_INSPECTOR='
import os, re, ssl, sys, time
name = sys.argv[1]
crt, key = "/run/tls/" + name + ".crt", "/run/tls/" + name + ".key"
def done(valid=0, days="", not_after="", problem=""):
    print("valid=%s\ndays_left=%s\nnot_after=%s\nproblem=%s" % (valid, days, not_after, problem))
    sys.exit(0)
if not os.access("/run/tls", os.R_OK | os.X_OK):
    done(problem="the certificate directory is not readable by the container user")
if not os.path.lexists(crt) or not os.path.lexists(key):
    done(problem="missing")
try:
    ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(crt, key)
    info = ssl._ssl._test_decode_cert(crt)
    expires = ssl.cert_time_to_seconds(info["notAfter"])
except FileNotFoundError:
    done(problem="missing")
except PermissionError:
    done(problem="not readable by the container user")
except ssl.SSLError:
    done(problem="the key does not match the certificate (or a file is not PEM)")
except (OSError, KeyError, ValueError) as exc:
    done(problem="unreadable (" + type(exc).__name__ + ")")
days = int((expires - time.time()) // 86400)
not_after = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires))
names = [v.lower() for k, v in info.get("subjectAltName", ()) if k == "DNS"]
if name not in names:
    shown = re.sub(r"[^a-z0-9.*,-]", "?", ",".join(names))[:120] or "no DNS names"
    done(days=days, not_after=not_after, problem="issued for " + shown + ", not for " + name)
if expires <= time.time():
    done(days=days, not_after=not_after, problem="expired")
done(1, days, not_after)
'

# The GPU mode of this deploy (auto, 1 or 0; see gpu_mode) and a COMPOSE_FILE for this run only.
GPU_MODE="$DEFAULT_NVIDIA"
COMPOSE_FILE_OVERRIDE=''

# Defaults until resolve_https_state runs: plain HTTP on the Tailscale IP.
HTTPS_MODE="$DEFAULT_HTTPS"
TS_NAME=''
TS_CERT_OK=0
TS_NAME_REASON=''
CERT_VALID=0
CERT_DAYS_LEFT=''
CERT_NOT_AFTER=''
CERT_PROBLEM=''
CERT_NEEDED=0
USE_TLS=''
PUBLIC_HOST=''
PUBLIC_SCHEME='http'
# 1 when the name probe or the certificate check could not answer (see https_check_failed).
TS_PROBE_FAILED=0
CERT_CHECK_FAILED=0
# 1 when such a failure kept the deployed HTTPS values (keep_https_state_on_check_error).
HTTPS_KEPT=0
KEPT_OK=0

# Only printable text from a probe, at most 200 characters.
clean_text() {
  local text="${1//[^[:print:]]/}"
  printf '%s\n' "${text:0:200}"
}

# Sets TS_NAME (a valid MagicDNS name or ''), TS_CERT_OK and TS_NAME_REASON. Never fails: without
# a name the pages are simply published by IP. TS_PROBE_FAILED=1 when there was no definite answer
# (nothing from tailscale, the image could not run, tailscaled not Running at the moment), unlike
# a tailnet without MagicDNS. Needs the JupyterLab image. Not meant for $(...).
probe_tailscale_name() {
  local output line value name='' cert_ok=0 reason='' probe_error=0 runner=()
  TS_NAME=''
  TS_CERT_OK=0
  TS_NAME_REASON=''
  TS_PROBE_FAILED=0
  if ! command -v tailscale >/dev/null 2>&1; then
    TS_NAME_REASON='Tailscale CLI not found'
    return 0
  fi
  command -v timeout >/dev/null 2>&1 && runner=(timeout 20)
  # tailscale's exit status is ignored: the parser explains empty or partial output itself.
  if ! output="$({ "${runner[@]}" tailscale status --json 2>/dev/null || true; } |
    docker run --rm -i --network none --entrypoint python "$JUPYTER_IMAGE" -c "$TS_STATUS_PARSER" 2>/dev/null)"; then
    TS_NAME_REASON="could not parse 'tailscale status --json' with ${JUPYTER_IMAGE}"
    TS_PROBE_FAILED=1
    return 0
  fi
  while IFS= read -r line; do
    value="${line#*=}"
    case "$line" in
      name=*)
        if [[ -z "$name" ]]; then
          name="$value"
        fi
        ;;
      cert_ok=1) cert_ok=1 ;;
      probe_error=1) probe_error=1 ;;
      reason=*)
        if [[ -z "$reason" ]]; then
          reason="$(clean_text "$value")"
        fi
        ;;
    esac
  done <<<"$output"
  if [[ -n "$name" ]] && ! is_valid_fqdn "$name"; then
    warn "Ignoring the MagicDNS name $(printf '%q' "${name:0:80}") from 'tailscale status': not a valid host name. The pages are published by IP."
    name=''
    cert_ok=0
    reason='the MagicDNS name reported by Tailscale is not a valid host name'
  fi
  if [[ -z "$name" && -z "$reason" ]]; then
    reason="no usable answer from 'tailscale status --json'"
    probe_error=1
  fi
  TS_NAME="$name"
  TS_CERT_OK="$cert_ok"
  TS_NAME_REASON="$reason"
  TS_PROBE_FAILED="$probe_error"
}

# Sets CERT_VALID (1: readable by the container user, key and certificate match, issued for $1,
# not expired), CERT_DAYS_LEFT, CERT_NOT_AFTER and CERT_PROBLEM for <TLS_DIR>/$1.crt/.key.
# Checked inside the JupyterLab image as JLT_UID:JLT_GID, so group permissions count exactly as
# they do for the services. CERT_CHECK_FAILED=1 when the check itself could not run, which says
# nothing about the files. Not meant for $(...).
inspect_certificate() {
  local name="$1" output line value
  CERT_VALID=0
  CERT_DAYS_LEFT=''
  CERT_NOT_AFTER=''
  CERT_PROBLEM=''
  CERT_CHECK_FAILED=0
  if ! is_valid_fqdn "$name"; then
    CERT_PROBLEM='no valid name'
    return 0
  fi
  if ! is_safe_tls_dir "$TLS_DIR"; then
    CERT_PROBLEM="unusable certificate directory $(printf '%q' "$TLS_DIR")"
    return 0
  fi
  if [[ ! -d "$TLS_DIR" ]]; then
    CERT_PROBLEM='missing'
    return 0
  fi
  # --mount, not -v: a missing source must fail instead of being created by the daemon.
  if ! output="$(docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --user "$(id -u):$(id -g)" \
    --mount "type=bind,source=${TLS_DIR},target=/run/tls,readonly" \
    --entrypoint python "$JUPYTER_IMAGE" -c "$CERT_INSPECTOR" "$name" 2>/dev/null)"; then
    CERT_PROBLEM="could not be checked (docker run ${JUPYTER_IMAGE} failed)"
    CERT_CHECK_FAILED=1
    return 0
  fi
  while IFS= read -r line; do
    value="${line#*=}"
    case "$line" in
      valid=1) CERT_VALID=1 ;;
      days_left=*) [[ ! "$value" =~ ^-?[0-9]{1,6}$ ]] || CERT_DAYS_LEFT="$value" ;;
      not_after=*) [[ ! "$value" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] || CERT_NOT_AFTER="$value" ;;
      problem=*) CERT_PROBLEM="$(clean_text "$value")" ;;
    esac
  done <<<"$output"
  if [[ "$CERT_VALID" == 1 && (-z "$CERT_DAYS_LEFT" || -z "$CERT_NOT_AFTER") ]]; then
    CERT_VALID=0
    CERT_PROBLEM='unexpected output from the certificate check'
    CERT_CHECK_FAILED=1
  fi
  if [[ "$CERT_VALID" != 1 && -z "$CERT_PROBLEM" ]]; then
    CERT_PROBLEM='unusable'
  fi
}

# Decides from the probe and the certificate: USE_TLS ('1' or ''), CERT_NEEDED (1 when
# host-setup --cert should issue or renew the certificate; never after a check that could not
# run), PUBLIC_HOST (the name when there is one, else TS_IP) and PUBLIC_SCHEME. Uses HTTPS_MODE,
# TS_NAME, TS_CERT_OK and TS_IP.
refresh_certificate_state() {
  USE_TLS=''
  CERT_NEEDED=0
  CERT_VALID=0
  CERT_DAYS_LEFT=''
  CERT_NOT_AFTER=''
  CERT_PROBLEM=''
  CERT_CHECK_FAILED=0
  if [[ "$HTTPS_MODE" == auto && -n "$TS_NAME" ]]; then
    inspect_certificate "$TS_NAME"
    if [[ "$CERT_VALID" == 1 ]] && ((CERT_DAYS_LEFT > 0)) && [[ -f "$APP_DIR/compose.tls.yaml" ]]; then
      USE_TLS=1
    fi
    if [[ "$TS_CERT_OK" == 1 && "$CERT_CHECK_FAILED" != 1 ]] && is_valid_gid "$(id -g)" &&
      { [[ "$CERT_VALID" != 1 ]] || ((CERT_DAYS_LEFT < CERT_RENEW_DAYS)); }; then
      CERT_NEEDED=1
    fi
  fi
  PUBLIC_HOST="${TS_NAME:-$TS_IP}"
  PUBLIC_SCHEME='http'
  if [[ "$USE_TLS" == 1 ]]; then
    PUBLIC_SCHEME='https'
  fi
}

# $1: HTTPS mode (auto|off). Probes the name, then checks the certificate.
resolve_https_state() {
  HTTPS_MODE="$1"
  HTTPS_KEPT=0
  probe_tailscale_name
  refresh_certificate_state
}

# True when the name probe or the certificate check could not answer (tailscale status timed out,
# docker run failed, tailscaled not Running at the moment), unlike a definite state such as
# MagicDNS being off or the certificate missing.
https_check_failed() {
  [[ "$TS_PROBE_FAILED" == 1 || "$CERT_CHECK_FAILED" == 1 ]]
}

https_check_error() {
  if [[ "$TS_PROBE_FAILED" == 1 ]]; then
    printf '%s\n' "${TS_NAME_REASON:-no answer from tailscale status}"
  else
    printf 'certificate %s\n' "${CERT_PROBLEM:-could not be checked}"
  fi
}

# A host name rather than an address: all-digit labels such as 100.82.217.101 pass is_valid_fqdn
# too, so the last label must contain a letter.
is_dns_name() {
  is_valid_fqdn "$1" && [[ "${1##*.}" == *[abcdefghijklmnopqrstuvwxyz]* ]]
}

# Reads the HTTPS values of the deployed runtime .env into KEPT_* and sets KEPT_OK=1 when they are
# complete, consistent and deployed for the current TS_IP (a runtime .env from before the HTTPS
# setting has none); returns 1 otherwise. Call it before anything rewrites the runtime .env.
read_kept_https_state() {
  local file="$APP_DIR/.env" not_after_re='^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$'
  local -A kept_env=()
  KEPT_OK=0
  KEPT_MODE=''
  KEPT_TLS=''
  KEPT_NAME=''
  KEPT_NOT_AFTER=''
  KEPT_HOST="$TS_IP"
  KEPT_SCHEME='http'
  [[ -f "$file" && -r "$file" ]] || return 1
  read_env_file "$file" kept_env
  ((${#ENV_PARSE_ERRORS[@]} == 0)) || return 1
  [[ "${kept_env[TS_IP]:-}" == "$TS_IP" ]] || return 1
  case "${kept_env[HTTPS_MODE]:-}" in
    auto | off) KEPT_MODE="${kept_env[HTTPS_MODE]}" ;;
    *) return 1 ;;
  esac
  if [[ "${kept_env[TLS]:-}" == 1 ]]; then
    if ! is_dns_name "${kept_env[TLS_NAME]:-}" ||
      [[ "${kept_env[PUBLIC_HOST]:-}" != "${kept_env[TLS_NAME]}" || "${kept_env[PUBLIC_SCHEME]:-}" != https ||
        "${kept_env[COMPOSE_FILE]:-}" != *compose.tls.yaml* || "${kept_env[TLS_DIR]:-}" != "$TLS_DIR" ||
        ! "${kept_env[TLS_NOT_AFTER]:-}" =~ $not_after_re ]]; then
      return 1
    fi
    KEPT_TLS=1
    KEPT_NAME="${kept_env[TLS_NAME]}"
    KEPT_NOT_AFTER="${kept_env[TLS_NOT_AFTER]}"
    KEPT_HOST="$KEPT_NAME"
    KEPT_SCHEME='https'
  elif is_dns_name "${kept_env[PUBLIC_HOST]:-}"; then
    KEPT_NAME="${kept_env[PUBLIC_HOST]}"
    KEPT_HOST="$KEPT_NAME"
  fi
  KEPT_OK=1
}

# Puts the KEPT_* values in place of the probe results; no certificate step is asked for.
apply_kept_https_state() {
  HTTPS_MODE="$KEPT_MODE"
  TS_NAME="$KEPT_NAME"
  USE_TLS="$KEPT_TLS"
  CERT_NOT_AFTER="$KEPT_NOT_AFTER"
  PUBLIC_HOST="$KEPT_HOST"
  PUBLIC_SCHEME="$KEPT_SCHEME"
  CERT_NEEDED=0
}

# After resolve_https_state: a check that could not answer is no reason to switch the scheme or
# the host, which recreates JupyterLab and ends its kernels. When read_kept_https_state found
# usable values for the same mode ($1), they stay; otherwise the resolved fallback (HTTP) stays.
# Either way no certificate step is asked for, and the next start or update checks again.
keep_https_state_on_check_error() {
  https_check_failed || return 0
  CERT_NEEDED=0
  if [[ "$KEPT_OK" == 1 && "$KEPT_MODE" == "$1" ]]; then
    apply_kept_https_state
    HTTPS_KEPT=1
    warn "Could not check the Tailscale name or the HTTPS certificate ($(https_check_error)); keeping ${PUBLIC_SCHEME}://${PUBLIC_HOST} as deployed. The next start or update checks again."
  else
    warn "Could not check the Tailscale name or the HTTPS certificate ($(https_check_error)); using ${PUBLIC_SCHEME}://${PUBLIC_HOST} for now. The next start or update checks again."
  fi
}

# One line describing how the pages are served, for the summary, start and status.
https_description() {
  local renewal=''
  if [[ "$HTTPS_KEPT" == 1 ]]; then
    printf 'unchanged, %s://%s as deployed (could not check: %s)\n' "$PUBLIC_SCHEME" "$PUBLIC_HOST" "$(https_check_error)"
  elif [[ "$HTTPS_MODE" == off ]]; then
    printf "off (HTTPS='off' in the settings); plain HTTP inside the Tailscale tunnel\n"
  elif https_check_failed; then
    printf 'could not be checked (%s); %s://%s for now\n' "$(https_check_error)" "$PUBLIC_SCHEME" "$PUBLIC_HOST"
  elif [[ -z "$TS_NAME" ]]; then
    printf 'not available: %s; HTTP by IP inside the Tailscale tunnel\n' "${TS_NAME_REASON:-no Tailscale name}"
  elif [[ "$USE_TLS" == 1 ]]; then
    if [[ "$CERT_NEEDED" == 1 ]]; then
      renewal='; renewal due (root step)'
    elif ((CERT_DAYS_LEFT < CERT_RENEW_DAYS)); then
      renewal='; renewal due, but Tailscale cannot issue certificates right now'
    fi
    printf 'on, certificate for %s valid until %s (%s days left%s)\n' "$TS_NAME" "$CERT_NOT_AFTER" "$CERT_DAYS_LEFT" "$renewal"
  elif [[ "$TS_CERT_OK" != 1 ]]; then
    printf 'not available: %s; HTTP by name inside the Tailscale tunnel\n' "${TS_NAME_REASON:-no certificate}"
  elif [[ "$CERT_VALID" == 1 ]]; then
    printf 'certificate for %s valid until %s, but not in use yet; run update\n' "$TS_NAME" "$CERT_NOT_AFTER"
  elif [[ "$CERT_PROBLEM" == missing ]]; then
    printf 'certificate for %s missing; HTTP by name until the root step issues it\n' "$TS_NAME"
  else
    printf 'certificate for %s unusable (%s); HTTP by name until the root step replaces it\n' "$TS_NAME" "$CERT_PROBLEM"
  fi
}

# Deploy/start log lines for the probe results.
report_https_state() {
  if [[ -n "$TS_NAME" ]]; then
    info "Tailscale name: $TS_NAME"
  else
    info "No Tailscale name (${TS_NAME_REASON}); the pages are published by IP."
  fi
  info "HTTPS: $(https_description)"
}

# COMPOSE_FILE for the runtime .env; $1 GPU (1/0), $2 TLS ('1' or '').
compose_file_value() {
  local value='compose.yaml'
  if [[ "$1" == 1 ]]; then
    value+=':compose.gpu.yaml'
  fi
  if [[ "$2" == 1 ]]; then
    value+=':compose.tls.yaml'
  fi
  printf '%s\n' "$value"
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

# The GPU mode for this deploy (auto, 1 or 0): JLT_GPU when it is set, else the NVIDIA setting.
gpu_mode() {
  case "${JLT_GPU:-}" in
    on) printf '1\n' ;;
    off) printf '0\n' ;;
    auto) printf 'auto\n' ;;
    *) printf '%s\n' "$NVIDIA" ;;
  esac
}

# Where the GPU mode came from, for messages.
gpu_mode_source() {
  if [[ -n "${JLT_GPU:-}" ]]; then
    printf 'JLT_GPU=%s\n' "$JLT_GPU"
  else
    printf "NVIDIA='%s' in %s\n" "$NVIDIA" "$SETTINGS_FILE"
  fi
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
  for name in Dockerfile compose.yaml compose.gpu.yaml compose.tls.yaml; do
    install -m 644 -- "$STACK_DIR/$name" "$APP_DIR/$name"
  done
  for name in jupyter stats ai theme; do
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
# HTTPS values: PUBLIC_HOST/PUBLIC_SCHEME build the URLs; TLS='1' adds compose.tls.yaml, which
# mounts TLS_DIR and names the files after TLS_NAME. TLS_NOT_AFTER changes with every renewed
# certificate, which makes Compose recreate the two services so they load the new files.
runtime_env_content() {
  local gpu="$1" profiles='' tls_name='' tls_not_after=''
  if [[ "$STATS_ENABLED" == 1 ]]; then
    profiles='stats'
  fi
  if [[ "${AI_ENABLED:-0}" == 1 ]]; then
    profiles="${profiles:+${profiles},}ai"
  fi
  if [[ "$USE_TLS" == 1 ]]; then
    tls_name="$TS_NAME"
    tls_not_after="$CERT_NOT_AFTER"
  fi
  printf '%s\n' \
    "# Generated by setup-jupyterlab-tailscale.sh; do not edit. Change the settings file and run update." \
    "COMPOSE_PROJECT_NAME='${PROJECT}'" \
    "COMPOSE_FILE='$(compose_file_value "$gpu" "$USE_TLS")'" \
    "COMPOSE_PROFILES='${profiles}'" \
    "TS_IP='${TS_IP}'" \
    "JUPYTER_PORT='${JUPYTER_PORT}'" \
    "STATS_PORT='${STATS_PORT}'" \
    "STATS_USER='${STATS_USER}'" \
    "THEME='${THEME}'" \
    "WORKSPACE_DIR='${WORKSPACE_DIR}'" \
    "HOST_NAME='$(sanitized_hostname)'" \
    "JLT_UID='$(id -u)'" \
    "JLT_GID='$(id -g)'" \
    "HTTPS_MODE='${HTTPS_MODE}'" \
    "NVIDIA='${GPU_MODE}'" \
    "PUBLIC_HOST='${PUBLIC_HOST:-$TS_IP}'" \
    "PUBLIC_SCHEME='${PUBLIC_SCHEME}'" \
    "TLS='${USE_TLS}'" \
    "TLS_NAME='${tls_name}'" \
    "TLS_NOT_AFTER='${tls_not_after}'" \
    "TLS_DIR='${TLS_DIR}'" \
    "AI_CONFIG_HASH='${AI_CONFIG_HASH:-}'"
}

# Dies unless the HTTPS values are safe to write into the single-quoted runtime .env.
validate_https_values() {
  case "$HTTPS_MODE" in
    auto | off) ;;
    *) die "Refusing to write an invalid HTTPS mode: $(printf '%q' "$HTTPS_MODE")" ;;
  esac
  if ! is_tailscale_ipv4 "${PUBLIC_HOST:-$TS_IP}" && ! is_valid_fqdn "${PUBLIC_HOST:-$TS_IP}"; then
    die "Refusing to write an invalid public host: $(printf '%q' "$PUBLIC_HOST")"
  fi
  [[ "$PUBLIC_SCHEME" == http || "$PUBLIC_SCHEME" == https ]] ||
    die "Refusing to write an invalid URL scheme: $(printf '%q' "$PUBLIC_SCHEME")"
  is_safe_tls_dir "$TLS_DIR" ||
    die "Refusing certificate directory $(printf '%q' "$TLS_DIR"): use an absolute path of letters, digits and . _ / -"
  if [[ "$USE_TLS" == 1 ]]; then
    is_valid_fqdn "$TS_NAME" || die "Refusing to enable HTTPS without a valid name: $(printf '%q' "$TS_NAME")"
    [[ "$PUBLIC_SCHEME" == https && "$PUBLIC_HOST" == "$TS_NAME" ]] || die 'Inconsistent HTTPS state (scheme or host).'
    [[ "$CERT_NOT_AFTER" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] ||
      die "Refusing to write an invalid certificate expiry: $(printf '%q' "$CERT_NOT_AFTER")"
  elif [[ -n "$USE_TLS" || "$PUBLIC_SCHEME" != http ]]; then
    die 'Inconsistent HTTPS state (TLS off but scheme https).'
  fi
}

write_runtime_env() {
  is_safe_workspace_path "$WORKSPACE_DIR" ||
    die "Refusing workspace path with a quote, backslash or control character: $(printf '%q' "$WORKSPACE_DIR")"
  is_tailscale_ipv4 "$TS_IP" || die "Refusing to write an invalid Tailscale IPv4: $(printf '%q' "$TS_IP")"
  is_valid_theme_name "$THEME" || die "Refusing to write an invalid theme name: $(printf '%q' "$THEME")"
  [[ "$GPU_MODE" == auto || "$GPU_MODE" == 1 || "$GPU_MODE" == 0 ]] ||
    die "Refusing to write an invalid GPU mode: $(printf '%q' "$GPU_MODE")"
  [[ "${AI_CONFIG_HASH:-}" =~ ^([0-9a-f]{64})?$ ]] || die 'Refusing to write an invalid AI settings hash.'
  validate_https_values
  write_file_atomic "$APP_DIR/.env" 600 "$(runtime_env_content "$1")"$'\n'
}

# A throwaway container with the GPU request of compose.gpu.yaml. On failure it says why and,
# for a known host problem, how to fix it.
# The project's requirements.txt, checked with the package runner's own rules (the image's
# deps_runner.py) right after the build, before any container is recreated.
check_requirements() {
  [[ -e "$REQUIREMENTS_FILE" ]] || return 0
  [[ -f "$REQUIREMENTS_FILE" && -r "$REQUIREMENTS_FILE" ]] ||
    die "$REQUIREMENTS_FILE is not a readable file."
  docker run --rm -i --pull never --network none --entrypoint python "$JUPYTER_IMAGE" \
    /srv/jupyter/deps_runner.py check <"$REQUIREMENTS_FILE" ||
    die "$REQUIREMENTS_FILE is not accepted (see above); nothing was changed. Fix it and deploy again."
}

# Makes the custom packages environment (the one the Dependencies page shows) contain the
# project's requirements.txt, through the package runner: the same pip job, constraints and
# download cache as the page, skipped when the file and the environment are unchanged. With
# statistics on, the running runner does it and the page shows the job; with them off, a
# one-off runner container does. A missing file hands over an empty list: nothing extra is
# installed, and packages it listed before are no longer part of later installs (they stay
# until Reset & reinstall on the page).
apply_requirements() {
  local source=/dev/null runner=(python /srv/jupyter/deps_runner.py baseline)
  if [[ -f "$REQUIREMENTS_FILE" ]]; then
    source="$REQUIREMENTS_FILE"
  fi
  if [[ "$STATS_ENABLED" == 1 ]]; then
    info "Packages from $REQUIREMENTS_FILE: checking the custom packages environment..."
    compose exec -T deps "${runner[@]}" <"$source" || requirements_failed
  elif [[ "$source" != /dev/null ]]; then
    info "Packages from $REQUIREMENTS_FILE: checking the custom packages environment (one-off runner)..."
    compose --profile stats run --rm --no-deps -T deps "${runner[@]}" --local <"$source" || requirements_failed
  fi
}

requirements_failed() {
  die "The packages from $REQUIREMENTS_FILE were not installed (see above). JupyterLab runs, with the custom packages installed before; fix the file and run: $(script_cmd) update"
}

gpu_probe() {
  local output spec
  if output="$(docker run --rm --pull never --network none --gpus all --entrypoint nvidia-smi \
    "$JUPYTER_IMAGE" -L 2>&1)"; then
    info "GPU check passed: ${output%%$'\n'*}"
    return 0
  fi
  warn "GPU check failed inside the container: ${output%%$'\n'*}"
  if [[ "$output" == *nvidia-persistenced* ]]; then
    # A CDI spec generated while nvidia-persistenced ran lists its socket as a mount; with the
    # daemon stopped (reboot, driver update) Docker cannot create any GPU container.
    warn "Docker's NVIDIA setup mounts /run/nvidia-persistenced/socket, but nvidia-persistenced is not running. Start it (and at boot): sudo systemctl enable --now nvidia-persistenced"
    for spec in /etc/cdi/* /var/run/cdi/*; do
      if [[ -f "$spec" ]] && grep -qsF 'nvidia-persistenced' -- "$spec"; then
        warn "Or regenerate the CDI spec without the socket: sudo nvidia-ctk cdi generate --output=$spec"
      fi
    done
  fi
  return 1
}

# How to run without the GPU, for messages.
gpu_off_hint() {
  if [[ -n "${JLT_GPU:-}" ]]; then
    printf 'JLT_GPU=off %s update' "$(script_cmd)"
  else
    printf "set NVIDIA='0' in %s (builder: untick NVIDIA GPU), then run: %s update" "$SETTINGS_FILE" "$(script_cmd)"
  fi
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

# The AI gateway's settings: the content of AI_FILE, or {} (no keys) when AI is off.
ai_config_content() {
  if [[ -e "$AI_FILE" || -L "$AI_FILE" ]]; then
    [[ -f "$AI_FILE" && -r "$AI_FILE" ]] || die "$AI_FILE is not a readable file."
    printf '%s\n' "$(<"$AI_FILE")"
  else
    printf '{}\n'
  fi
}

# The AI gateway's secrets: ai_config (the ai: section with the API keys, for the ai container
# only) and ai_token (random, shared with jupyterlab and stats, created once). Sets AI_ENABLED,
# AI_CONFIG_HASH (in the runtime .env, so a changed ai_config recreates the ai container: Compose
# does not notice a changed secret file) and AI_TOKEN_CHANGED.
ensure_ai_secrets() {
  local dir="$APP_DIR/secrets" file="$APP_DIR/secrets/ai_token" content token=''
  AI_ENABLED=0
  AI_TOKEN_CHANGED=0
  install -d -m 700 -- "$dir"
  content="$(ai_config_content)"$'\n'
  if [[ -f "$AI_FILE" ]]; then
    AI_ENABLED=1
  fi
  write_file_atomic "$dir/ai_config" 600 "$content"
  AI_CONFIG_HASH="$(printf '%s' "$content" | sha256sum)"
  AI_CONFIG_HASH="${AI_CONFIG_HASH%% *}"
  if [[ -f "$file" ]]; then
    token="$(<"$file")"
  fi
  if [[ "$token" =~ ^[0-9a-f]{64}$ ]]; then
    chmod 600 -- "$file"
    return 0
  fi
  token="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
  [[ "$token" =~ ^[0-9a-f]{64}$ ]] || die 'Could not generate the AI gateway token.'
  write_file_atomic "$file" 600 "$token"
  AI_TOKEN_CHANGED=1
}

print_summary() {
  local mode="$1" gpu="$2" cmd base
  cmd="$(script_cmd)"
  base="${PUBLIC_SCHEME}://${PUBLIC_HOST:-$TS_IP}"
  printf '\n'
  if [[ "$mode" == install ]]; then
    info 'JupyterLab over Tailscale is installed and running.'
  else
    info 'JupyterLab over Tailscale is updated and running.'
  fi
  printf '  JupyterLab:  %s:%s/lab\n' "$base" "$JUPYTER_PORT"
  if [[ "$STATS_ENABLED" == 1 ]]; then
    printf '  Statistics:  %s:%s/\n' "$base" "$STATS_PORT"
    printf '  Packages:    %s:%s/dependencies\n' "$base" "$STATS_PORT"
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
  if [[ -f "$REQUIREMENTS_FILE" ]]; then
    printf '  Packages:    %s (in the custom packages environment)\n' "$REQUIREMENTS_FILE"
  else
    printf '  Packages:    no %s; the Dependencies page adds packages\n' "$REQUIREMENTS_FILE"
  fi
  printf '  GPU:         %s (NVIDIA %s)\n' "$([[ "$gpu" == 1 ]] && printf 'enabled' || printf 'not used')" "$GPU_MODE"
  if [[ "$AI_ENABLED" == 1 ]]; then
    printf '  AI:          on (%%%%ai and the AI cell button; token budget on the Statistics page)\n'
  else
    printf '  AI:          off (no ai: section in config.yaml)\n'
  fi
  printf '  Address:     %s\n' "$TS_IP"
  if [[ -n "$TS_NAME" ]]; then
    printf '  Name:        %s\n' "$TS_NAME"
  else
    printf '  Name:        none (%s)\n' "${TS_NAME_REASON:-unknown}"
  fi
  printf '  HTTPS:       %s\n' "$(https_description)"
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
  ensure_ai_secrets

  GPU_MODE="$(gpu_mode)"
  case "$GPU_MODE" in
    1)
      gpu=1
      info "NVIDIA GPU expected ($(gpu_mode_source)): both containers get access to it."
      ;;
    auto)
      if gpu_available; then
        gpu=1
        info 'NVIDIA GPU available: both containers get access to it.'
      fi
      ;;
    *) info "NVIDIA GPU off ($(gpu_mode_source)): the containers run without it." ;;
  esac
  # Until the name and the certificate are checked with the built image, the runtime .env keeps
  # the deployed HTTPS values when they belong to this address: a failed build must not leave it
  # pointing at http://<ip> while the running containers still serve HTTPS by name. A first
  # install (or a new address) starts from plain HTTP on the IP.
  HTTPS_MODE="$HTTPS"
  TS_NAME=''
  USE_TLS=''
  CERT_NOT_AFTER=''
  PUBLIC_HOST="$TS_IP"
  PUBLIC_SCHEME='http'
  if read_kept_https_state; then
    apply_kept_https_state
  fi
  write_runtime_env "$gpu"

  if [[ "$mode" == update ]]; then
    build_args+=(--pull)
    info 'Building images (refreshing the base image)...'
  else
    info 'Building images...'
  fi
  compose "${build_args[@]}" || die 'docker compose build failed; see the output above.'
  check_requirements

  if [[ "$gpu" == 1 ]] && ! gpu_probe; then
    if [[ "$GPU_MODE" == 1 ]]; then
      die "An NVIDIA GPU is expected ($(gpu_mode_source)), but Docker cannot hand it to a container (see above). Fix the NVIDIA driver or Container Toolkit, or run without the GPU: $(gpu_off_hint)"
    fi
    warn "Continuing without GPU access. Fix the NVIDIA driver or Container Toolkit and run update again, or skip the check: $(gpu_off_hint)"
    gpu=0
    write_runtime_env 0
  fi

  resolve_https_state "$HTTPS"
  keep_https_state_on_check_error "$HTTPS"
  ROOT_ATTEMPTED=0
  early_root_step "$TS_IP" "${ports[@]}"
  report_https_state
  write_runtime_env "$gpu"

  ensure_secrets

  if [[ "$STATS_ENABLED" != 1 ]]; then
    # A profile-disabled service is neither stopped by up nor treated as an orphan;
    # naming the services here enables their profile just for this command. The package
    # runner (deps) belongs to the dashboard; installed packages stay in their volume and
    # notebooks keep using them.
    info 'Statistics disabled: removing the stats and deps containers if they exist.'
    compose rm -s -f stats deps || warn 'Could not remove the stats and deps containers.'
  fi
  if [[ "$AI_ENABLED" != 1 ]]; then
    info 'AI off (no ai: section in config.yaml): removing the ai container if it exists.'
    compose rm -s -f ai || warn 'Could not remove the ai container.'
  fi

  local up_args=(up -d --remove-orphans --wait --wait-timeout 300)
  if [[ "$HASH_CHANGED" == 1 || "$TOKEN_CHANGED" == 1 || "$AI_TOKEN_CHANGED" == 1 ]]; then
    # Secret file contents are not part of Compose's config hash, so a new password or token
    # would otherwise keep the old containers (still bound to the old files).
    up_args+=(--force-recreate)
  fi
  info 'Starting containers and waiting until they are healthy...'
  compose "${up_args[@]}" || compose_failure 'The containers did not become healthy.'

  if ((!ROOT_ATTEMPTED)); then
    maybe_root_step "$TS_IP" "${ports[@]}"
  fi
  apply_requirements

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
  DEPLOYED_AI_ENABLED=0
  if [[ ",${RUNTIME_ENV[COMPOSE_PROFILES]:-}," == *,ai,* ]]; then
    DEPLOYED_AI_ENABLED=1
  fi
  DEPLOYED_GPU=0
  if [[ "${RUNTIME_ENV[COMPOSE_FILE]:-}" == *compose.gpu.yaml* ]]; then
    DEPLOYED_GPU=1
  fi
  # auto for a runtime .env from before the NVIDIA setting.
  DEPLOYED_NVIDIA="${RUNTIME_ENV[NVIDIA]:-auto}"
  [[ "$DEPLOYED_NVIDIA" == 1 || "$DEPLOYED_NVIDIA" == 0 ]] || DEPLOYED_NVIDIA='auto'
  # Empty for a runtime .env from before the HTTPS setting (HTTP on the IP, no name).
  DEPLOYED_HTTPS_MODE="${RUNTIME_ENV[HTTPS_MODE]:-}"
  [[ "$DEPLOYED_HTTPS_MODE" == auto || "$DEPLOYED_HTTPS_MODE" == off ]] || DEPLOYED_HTTPS_MODE=''
  DEPLOYED_TLS=''
  if [[ "${RUNTIME_ENV[TLS]:-}" == 1 && "${RUNTIME_ENV[COMPOSE_FILE]:-}" == *compose.tls.yaml* ]]; then
    DEPLOYED_TLS=1
  fi
  DEPLOYED_PUBLIC_HOST="${RUNTIME_ENV[PUBLIC_HOST]:-}"
  if ! is_valid_fqdn "$DEPLOYED_PUBLIC_HOST" && ! is_tailscale_ipv4 "$DEPLOYED_PUBLIC_HOST"; then
    DEPLOYED_PUBLIC_HOST="$DEPLOYED_TS_IP"
  fi
  DEPLOYED_PUBLIC_SCHEME='http'
  if [[ "$DEPLOYED_TLS" == 1 && "${RUNTIME_ENV[PUBLIC_SCHEME]:-}" == https ]]; then
    DEPLOYED_PUBLIC_SCHEME='https'
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

# Rewrites only the given KEY VALUE pairs of the runtime .env (single-quoted; missing keys are
# appended). Callers validate the values.
set_runtime_values() {
  local file="$APP_DIR/.env" line key content=''
  local -A pending=()
  while (($# >= 2)); do
    pending["$1"]="$2"
    shift 2
  done
  while IFS= read -r line || [[ -n "$line" ]]; do
    key="${line%%=*}"
    if [[ "$line" == *=* && -n "${pending[$key]+set}" ]]; then
      line="${key}='${pending[$key]}'"
      unset 'pending[$key]'
    fi
    content+="$line"$'\n'
  done <"$file"
  while IFS= read -r key; do
    [[ -n "$key" ]] || continue
    content+="${key}='${pending[$key]}'"$'\n'
  done < <(printf '%s\n' "${!pending[@]}" | sort)
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
  [[ "$HTTPS" == "$DEPLOYED_HTTPS_MODE" ]] ||
    differences+=("HTTPS ${DEPLOYED_HTTPS_MODE:-(deployed before the setting existed)} -> ${HTTPS}")
  [[ "$NVIDIA" == "$DEPLOYED_NVIDIA" ]] ||
    differences+=("NVIDIA ${DEPLOYED_NVIDIA} -> ${NVIDIA}")
  if [[ -r "$APP_DIR/secrets/jupyter_password" ]]; then
    current="$(<"$APP_DIR/secrets/jupyter_password")"
  fi
  [[ "$current" == "$JUPYTER_PASSWORD" ]] || differences+=('JUPYTER_PASSWORD changed')
  # Only when the file can be read (update reports a broken one), and not for a deployment from
  # before the AI setting that still has no AI file.
  if [[ -e "$AI_FILE" || -n "${RUNTIME_ENV[AI_CONFIG_HASH]:-}" ]] &&
    [[ ! -e "$AI_FILE" || (-f "$AI_FILE" && -r "$AI_FILE") ]]; then
    current="$(ai_config_content)"$'\n'
    current="$(printf '%s' "$current" | sha256sum)"
    [[ "${current%% *}" == "${RUNTIME_ENV[AI_CONFIG_HASH]:-}" ]] ||
      differences+=("AI settings ($AI_FILE, from config.yaml's ai: section) changed")
  fi
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

# The root step is skipped when nothing would change: no certificate is due (CERT_NEEDED, see
# refresh_certificate_state), the sysctl is in place and either no firewall is active or the
# recorded rules already match the wanted address and ports.
root_step_needed() {
  local ts_ip="$1" wanted firewall
  shift
  wanted="$(sorted_ports "$@")"
  [[ "$CERT_NEEDED" != 1 ]] || return 0
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
      if [[ " $* " == *' --cert '* ]]; then
        info "The same step issues or renews the HTTPS certificate for ${*: -2:1} with 'tailscale cert' into ${DEFAULT_TLS_DIR} (readable by your group)."
      fi
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

# host-setup arguments for the ports, plus the certificate pair when one is due.
root_step_args() {
  ROOT_ARGS=(host-setup "$@")
  if [[ "$CERT_NEEDED" == 1 ]]; then
    ROOT_ARGS+=(--cert "$TS_NAME" "$(id -g)")
  fi
}

# True when run_root can run the step itself now (sudo from a terminal, or cached credentials)
# instead of only printing ROOT_STEP_REQUIRED.
root_runs_here() {
  command -v sudo >/dev/null 2>&1 || return 1
  [[ -t 0 && -t 1 ]] && return 0
  sudo -n true >/dev/null 2>&1
}

maybe_root_step() {
  local ts_ip="$1"
  shift
  if root_step_needed "$ts_ip" "$@"; then
    root_step_args "$ts_ip" "$@"
    run_root "${ROOT_ARGS[@]}"
  fi
}

# Before 'compose up': when the step can run here, run it now, so a new certificate is already
# in place when the containers start; then check the certificate again. Otherwise the caller
# deploys with what is usable now and maybe_root_step prints ROOT_STEP_REQUIRED afterwards.
# Sets ROOT_ATTEMPTED=1 when the step ran (successfully or not), so it is not asked twice.
early_root_step() {
  local ts_ip="$1"
  shift
  if root_step_needed "$ts_ip" "$@" && root_runs_here; then
    root_step_args "$ts_ip" "$@"
    run_root "${ROOT_ARGS[@]}"
    ROOT_ATTEMPTED=1
    if [[ "$CERT_NEEDED" == 1 ]]; then
      refresh_certificate_state
    fi
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

# Warns when group $1 lets more than one account read the key: its members plus every account
# with it as primary group (a shared group such as 'users' instead of a per-user group).
root_warn_shared_key_group() {
  local gid="$1" entry name _password _uid primary _rest shown
  local -a members=()
  local -A readers=()
  entry="$(getent group "$gid" 2>/dev/null)" || entry=''
  if [[ -n "$entry" ]]; then
    IFS=',' read -r -a members <<<"${entry##*:}"
    for name in "${members[@]}"; do
      if [[ -n "$name" ]]; then
        readers["$name"]=1
      fi
    done
  fi
  while IFS=: read -r name _password _uid primary _rest; do
    if [[ -n "$name" && "$primary" == "$gid" ]]; then
      readers["$name"]=1
    fi
  done < <(getent passwd 2>/dev/null || true)
  ((${#readers[@]} > 1)) || return 0
  shown="$(printf '%s\n' "${!readers[@]}" | sort | head -n 8 | tr '\n' ' ')"
  warn "Group ${gid} is shared by ${#readers[@]} accounts ($(clean_text "${shown% }")): each of them can read the HTTPS private key in ${TLS_DIR}. Give your account its own primary group to keep the key private."
}

# host-setup --cert: 'tailscale cert' for the full MagicDNS name into TLS_DIR (root:<gid> 0750,
# certificate 0644, key 0640: the containers run with the desktop user's gid). Files for other
# names are removed. Returns 1 with the reason printed when no certificate could be written.
# It runs as an 'if' condition, where set -e does not apply: every step checks its own result,
# and the previous files stay until both new ones are complete.
root_issue_certificate() {
  local name="$1" gid="$2" output tmp_crt='' tmp_key='' path base
  local help='Check in the Tailscale admin console (https://login.tailscale.com/admin/dns) that MagicDNS and HTTPS Certificates are enabled, and that this machine is logged in (tailscale status).'
  if [[ -L "$ROOT_STATE_DIR" || -L "$TLS_DIR" ]]; then
    warn "Refusing to write certificates: $ROOT_STATE_DIR or $TLS_DIR is a symbolic link."
    return 1
  fi
  if [[ -e "$ROOT_STATE_DIR" && ! -d "$ROOT_STATE_DIR" ]] || [[ -e "$TLS_DIR" && ! -d "$TLS_DIR" ]]; then
    warn "Refusing to write certificates: $ROOT_STATE_DIR or $TLS_DIR exists but is not a directory."
    return 1
  fi
  if ! command -v tailscale >/dev/null 2>&1; then
    warn "tailscale not found in root's PATH; no certificate for ${name}."
    return 1
  fi
  if [[ ! -d "$ROOT_STATE_DIR" ]] && ! mkdir -m 755 -- "$ROOT_STATE_DIR"; then
    warn "Could not create $ROOT_STATE_DIR; no certificate for ${name}."
    return 1
  fi
  if [[ ! -d "$TLS_DIR" ]] && ! mkdir -m 750 -- "$TLS_DIR"; then
    warn "Could not create $TLS_DIR; no certificate for ${name}."
    return 1
  fi
  if ! chown -h "root:${gid}" -- "$TLS_DIR" || ! chmod 750 -- "$TLS_DIR"; then
    warn "Could not give $TLS_DIR owner root:${gid} and mode 0750; no certificate for ${name}."
    return 1
  fi
  # Without both temp names the call below would be a bare 'tailscale cert', which writes a
  # root-only key into the working directory (TLS_DIR) over the group-readable one.
  if ! tmp_crt="$(mktemp "$TLS_DIR/.${name}.crt.XXXXXX")" || [[ -z "$tmp_crt" ]] ||
    ! tmp_key="$(mktemp "$TLS_DIR/.${name}.key.XXXXXX")" || [[ -z "$tmp_key" ]]; then
    rm -f -- ${tmp_crt:+"$tmp_crt"} ${tmp_key:+"$tmp_key"}
    warn "Could not create temporary files in $TLS_DIR (disk or inodes full?); no certificate for ${name}."
    return 1
  fi
  # Always the full name and both file flags: a bare 'tailscale cert <name>' writes into the
  # working directory, and the short node name is refused. --min-validity: see CERT_RENEW_DAYS.
  # Run from TLS_DIR all the same.
  if ! output="$(cd -- "$TLS_DIR" && tailscale cert --cert-file "$tmp_crt" --key-file "$tmp_key" \
    --min-validity "$CERT_MIN_VALIDITY" "$name" 2>&1)"; then
    rm -f -- "$tmp_crt" "$tmp_key"
    printf '%s\n' "$output" | sed 's/^/  tailscale cert: /' >&2
    warn "tailscale cert could not issue a certificate for ${name}. ${help}"
    return 1
  fi
  if [[ ! -s "$tmp_crt" || ! -s "$tmp_key" ]]; then
    rm -f -- "$tmp_crt" "$tmp_key"
    warn "tailscale cert reported success but wrote no certificate for ${name}. ${help}"
    return 1
  fi
  if ! chown "root:${gid}" -- "$tmp_crt" "$tmp_key" || ! chmod 644 -- "$tmp_crt" || ! chmod 640 -- "$tmp_key"; then
    rm -f -- "$tmp_crt" "$tmp_key"
    warn "Could not set owner root:${gid} and modes 0644/0640 on the new certificate for ${name}; the previous files are unchanged."
    return 1
  fi
  # Two renames cannot happen at once: a service starting in between sees a mismatched pair,
  # exits and is started again by its restart policy.
  if ! mv -f -- "$tmp_key" "$TLS_DIR/${name}.key"; then
    rm -f -- "$tmp_crt" "$tmp_key"
    warn "Could not move the new key for ${name} into place; the previous files are unchanged."
    return 1
  fi
  if ! mv -f -- "$tmp_crt" "$TLS_DIR/${name}.crt"; then
    rm -f -- "$tmp_crt"
    warn "Could not move the new certificate for ${name} into place after its key: the two files no longer match. Run host-setup again."
    return 1
  fi
  for path in "$TLS_DIR"/* "$TLS_DIR"/.[!.]*; do
    [[ -e "$path" || -L "$path" ]] || continue
    base="${path##*/}"
    case "$base" in
      "${name}.crt" | "${name}.key") ;;
      *.crt | *.key | .*.crt.* | .*.key.*)
        if [[ -f "$path" || -L "$path" ]] && rm -f -- "$path"; then
          printf 'tls: removed %s\n' "$path"
        fi
        ;;
    esac
  done
  printf 'tls: certificate for %s in %s (root:%s, certificate 0644, key 0640)\n' "$name" "$TLS_DIR" "$gid"
  root_warn_shared_key_group "$gid"
}

cmd_host_setup() {
  root_prepare host-setup
  local usage='Usage: host-setup <tailscale-ipv4> <port> [<port>] [--cert <magicdns-name> <gid>]'
  local cert_name='' cert_gid='' cert_failed=0 tls_name tls_gid
  # The certificate pair is optional and always last.
  if (($# >= 5)) && [[ "${*: -3:1}" == --cert ]]; then
    cert_name="${*: -2:1}"
    cert_gid="${*: -1}"
    set -- "${@:1:$#-3}"
    is_valid_fqdn "$cert_name" ||
      die "host-setup: $(printf '%q' "$cert_name") is not a full MagicDNS name (e.g. host.tailnet.ts.net)"
    is_valid_gid "$cert_gid" || die "host-setup: $(printf '%q' "$cert_gid") is not a group id from 1 to 4294967294"
  fi
  if (($# < 2 || $# > 3)); then
    die "$usage"
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
  # Without --cert an existing certificate stays as it is, and so does its record.
  tls_name="${ROOT_STATE[TLS_NAME]:-}"
  tls_gid="${ROOT_STATE[TLS_GID]:-}"
  if ! is_valid_fqdn "$tls_name" || ! is_valid_gid "$tls_gid"; then
    tls_name=''
    tls_gid=''
  fi

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

  # e) HTTPS certificate for the MagicDNS name. A failure is reported, and the exit status says
  #    so, but the settings above stay applied.
  if [[ -n "$cert_name" ]]; then
    if root_issue_certificate "$cert_name" "$cert_gid"; then
      tls_name="$cert_name"
      tls_gid="$cert_gid"
    else
      cert_failed=1
    fi
  fi

  # f) State for the unprivileged commands and for teardown.
  state="$(printf '%s\n' \
    '# Managed by setup-jupyterlab-tailscale.sh host-setup; read by the unprivileged commands.' \
    "TS_IP='${ts_ip}'" \
    "PORTS='${ports}'" \
    "FIREWALL='${firewall}'" \
    "PREV_NONLOCAL_BIND='${previous_bind}'")"$'\n'
  if [[ "$firewall" == firewalld && -n "${FIREWALLD_ZONE_USED:-}" ]]; then
    state+="FIREWALLD_ZONE='${FIREWALLD_ZONE_USED}'"$'\n'
  fi
  if [[ -n "$tls_name" ]]; then
    state+="TLS_NAME='${tls_name}'"$'\n'"TLS_GID='${tls_gid}'"$'\n'
  fi
  if [[ -L "$ROOT_STATE_DIR" ]]; then
    die "host-setup: $ROOT_STATE_DIR is a symbolic link; refusing to write the state file."
  fi
  install -d -m 755 "$ROOT_STATE_DIR"
  chmod 755 "$ROOT_STATE_DIR"
  write_file_atomic "$ROOT_STATE_FILE" 644 "$state" root:root
  if ((cert_failed)); then
    # The builder looks for "but no certificate for" to say that only the certificate failed.
    die "host-setup: sysctl and firewall settings applied (${ts_ip}, ports ${ports}, firewall ${firewall}), but no certificate for ${cert_name}; see above."
  fi
  printf 'host-setup done: %s, ports %s, firewall %s%s\n' "$ts_ip" "$ports" "$firewall" \
    "${cert_name:+, certificate for ${cert_name}}"
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
  # Certificates first (not following a symlink), then the state directory around them.
  if [[ -L "$TLS_DIR" ]]; then
    rm -f -- "$TLS_DIR"
    printf 'Removed the symbolic link %s\n' "$TLS_DIR"
    removed=1
  elif [[ -d "$TLS_DIR" ]]; then
    rm -rf -- "$TLS_DIR"
    printf 'Removed %s (HTTPS certificates)\n' "$TLS_DIR"
    removed=1
  fi
  if [[ -L "$ROOT_STATE_DIR" ]]; then
    rm -f -- "$ROOT_STATE_DIR"
    printf 'Removed the symbolic link %s\n' "$ROOT_STATE_DIR"
    removed=1
  elif [[ -d "$ROOT_STATE_DIR" ]]; then
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

# start follows the Tailscale name and the certificate for deployments with the HTTPS setting:
# rewrites the published address and TLS values when they changed (a renewed certificate
# included), so 'up' recreates the services with them. A check that could not answer changes
# nothing (keep_https_state_on_check_error).
start_follow_https() {
  local tls_name='' tls_not_after='' compose_file
  local -a values
  read_kept_https_state || true
  resolve_https_state "$DEPLOYED_HTTPS_MODE"
  keep_https_state_on_check_error "$DEPLOYED_HTTPS_MODE"
  early_root_step "$TS_IP" "${ports[@]}"
  report_https_state
  if [[ "$USE_TLS" == 1 ]]; then
    tls_name="$TS_NAME"
    tls_not_after="$CERT_NOT_AFTER"
  fi
  compose_file="$(compose_file_value "$DEPLOYED_GPU" "$USE_TLS")"
  validate_https_values
  values=(COMPOSE_FILE "$compose_file" PUBLIC_HOST "$PUBLIC_HOST" PUBLIC_SCHEME "$PUBLIC_SCHEME"
    TLS "$USE_TLS" TLS_NAME "$tls_name" TLS_NOT_AFTER "$tls_not_after" TLS_DIR "$TLS_DIR")
  if [[ "${RUNTIME_ENV[COMPOSE_FILE]:-}" != "$compose_file" || "${RUNTIME_ENV[PUBLIC_HOST]:-}" != "$PUBLIC_HOST" ||
    "${RUNTIME_ENV[PUBLIC_SCHEME]:-}" != "$PUBLIC_SCHEME" || "${RUNTIME_ENV[TLS]:-}" != "$USE_TLS" ||
    "${RUNTIME_ENV[TLS_NAME]:-}" != "$tls_name" || "${RUNTIME_ENV[TLS_NOT_AFTER]:-}" != "$tls_not_after" ||
    "${RUNTIME_ENV[TLS_DIR]:-}" != "$TLS_DIR" ]]; then
    set_runtime_values "${values[@]}"
    if [[ "$DEPLOYED_PUBLIC_SCHEME://$DEPLOYED_PUBLIC_HOST" != "$PUBLIC_SCHEME://$PUBLIC_HOST" ]]; then
      info "Published address changed from ${DEPLOYED_PUBLIC_SCHEME}://${DEPLOYED_PUBLIC_HOST} to ${PUBLIC_SCHEME}://${PUBLIC_HOST}; updated $APP_DIR/.env (the services are recreated)."
    else
      info "HTTPS certificate or settings changed; updated $APP_DIR/.env (the services are recreated to load them)."
    fi
  fi
}

# The GPU hand-off can break after a deploy (nvidia-persistenced not running after a reboot, a
# driver update, a stale CDI spec), and Docker then refuses to create the GPU containers at all.
# So check it first: NVIDIA='1' stops with the reason; auto starts without the GPU this time and
# keeps it in the runtime .env, so the next start checks again.
start_check_gpu() {
  # A removed image leaves nothing to check with; 'up' rebuilds it.
  docker image inspect "$JUPYTER_IMAGE" >/dev/null 2>&1 || return 0
  gpu_probe && return 0
  if [[ "$DEPLOYED_NVIDIA" == 1 ]]; then
    die "An NVIDIA GPU is expected (NVIDIA='1'), but Docker cannot hand it to a container (see above). Fix the host and start again, or run without the GPU: $(gpu_off_hint)"
  fi
  warn "Starting without GPU access this time; the next start checks again. To stop checking: $(gpu_off_hint)"
  COMPOSE_FILE_OVERRIDE="$(compose_file_value 0 "$USE_TLS")"
}

cmd_start() {
  local ports base
  refuse_root
  require_installed
  require_docker
  load_runtime_env
  wait_for_tailscale 60
  if [[ "$TS_IP" != "$DEPLOYED_TS_IP" ]]; then
    set_runtime_values TS_IP "$TS_IP"
    info "Tailscale IPv4 changed from ${DEPLOYED_TS_IP:-<none>} to ${TS_IP}; updated $APP_DIR/.env (the containers are recreated on the new address)."
    if [[ "$DEPLOYED_PUBLIC_HOST" == "$DEPLOYED_TS_IP" ]]; then
      DEPLOYED_PUBLIC_HOST="$TS_IP"
    fi
    DEPLOYED_TS_IP="$TS_IP"
  fi
  read -r -a ports <<<"$(deployed_ports)"
  ROOT_ATTEMPTED=0
  if [[ -n "$DEPLOYED_HTTPS_MODE" ]]; then
    start_follow_https
  else
    # Deployed before the HTTPS setting: HTTP on the IP until the next update.
    PUBLIC_HOST="$TS_IP"
    PUBLIC_SCHEME='http'
  fi
  if ((!ROOT_ATTEMPTED)); then
    maybe_root_step "$TS_IP" "${ports[@]}"
  fi
  if [[ "$DEPLOYED_GPU" == 1 ]]; then
    start_check_gpu
  fi
  info 'Starting containers and waiting until they are healthy...'
  compose up -d --remove-orphans --wait --wait-timeout 180 ||
    compose_failure 'The containers did not become healthy.'
  base="${PUBLIC_SCHEME}://${PUBLIC_HOST}"
  info 'Running:'
  printf '  JupyterLab:  %s:%s/lab\n' "$base" "$DEPLOYED_JUPYTER_PORT"
  if [[ "$DEPLOYED_STATS_ENABLED" == 1 ]]; then
    printf '  Statistics:  %s:%s/  (user %s)\n' "$base" "$DEPLOYED_STATS_PORT" "$DEPLOYED_STATS_USER"
    printf '  Packages:    %s:%s/dependencies\n' "$base" "$DEPLOYED_STATS_PORT"
  fi
  settings_drift_warning
}

cmd_stop() {
  refuse_root
  require_installed
  require_docker
  info 'Stopping containers...'
  # --profile stats also stops stats and deps containers left over from before statistics
  # were disabled (--profile ai the same for the ai container). A running package install in
  # deps is cancelled and shown as interrupted.
  compose --profile stats --profile ai stop || die 'docker compose stop failed; see the output above.'
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
    ai) printf 'ai (AI gateway)\n' ;;
    *) printf '%s\n' "$1" ;;
  esac
}

show_containers() {
  local output name service state health ports error
  local -a expected=(jupyterlab)
  local -A seen=()
  # --profile stats/ai: also list containers left over after statistics or AI were disabled.
  if ! output="$(compose --profile stats --profile ai ps -a --format '{{.Name}}|{{.Service}}|{{.State}}|{{.Health}}|{{.Ports}}' 2>/dev/null)"; then
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
  [[ "$DEPLOYED_AI_ENABLED" == 1 ]] && expected+=(ai)
  for service in "${expected[@]}"; do
    if [[ -z "${seen[$service]:-}" ]]; then
      printf "  %-22s no container -> run '%s start'\n" "$(service_label "$service")" "$(script_cmd)"
    fi
  done
}

cmd_status() {
  local mode firewall bind live_ip='' ports base docker_ok=0
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
  printf 'Deployed:          JupyterLab port %s, statistics %s, GPU override %s (NVIDIA %s)\n' \
    "$DEPLOYED_JUPYTER_PORT" \
    "$([[ "$DEPLOYED_STATS_ENABLED" == 1 ]] && printf 'on (port %s, user %s)' "$DEPLOYED_STATS_PORT" "$DEPLOYED_STATS_USER" || printf 'off')" \
    "$([[ "$DEPLOYED_GPU" == 1 ]] && printf 'on' || printf 'off')" "$DEPLOYED_NVIDIA"
  printf 'Workspace:         %s\n' "$DEPLOYED_WORKSPACE_DIR"
  printf 'Packages file:     %s (%s)\n' "$REQUIREMENTS_FILE" \
    "$([[ -f "$REQUIREMENTS_FILE" ]] && printf 'installed on install/update' || printf 'absent: nothing extra')"
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
    docker_ok=1
    show_containers
  else
    printf '  Docker is not reachable (daemon down, or not in the docker group yet).\n'
  fi

  # Live name and certificate (both need the JupyterLab image), compared with the deployment.
  TS_IP="${live_ip:-$DEPLOYED_TS_IP}"
  HTTPS_MODE="${DEPLOYED_HTTPS_MODE:-$DEFAULT_HTTPS}"
  if ((docker_ok)) && [[ -n "$TS_IP" ]]; then
    resolve_https_state "$HTTPS_MODE"
    if [[ -n "$TS_NAME" ]]; then
      printf 'Name:              %s\n' "$TS_NAME"
    elif [[ "$TS_PROBE_FAILED" == 1 ]]; then
      printf 'Name:              unknown (%s)\n' "$TS_NAME_REASON"
    else
      printf 'Name:              none (%s)\n' "$TS_NAME_REASON"
    fi
    if [[ -z "$DEPLOYED_HTTPS_MODE" ]]; then
      printf "HTTPS:             not deployed yet (deployed before the HTTPS setting); run '%s update'\n" "$(script_cmd)"
    elif https_check_failed; then
      printf 'HTTPS:             could not be checked right now (%s); deployed %s://%s\n' "$(https_check_error)" \
        "$DEPLOYED_PUBLIC_SCHEME" "$DEPLOYED_PUBLIC_HOST"
    else
      printf 'HTTPS:             %s\n' "$(https_description)"
      if [[ "$DEPLOYED_PUBLIC_SCHEME://$DEPLOYED_PUBLIC_HOST" != "$PUBLIC_SCHEME://$PUBLIC_HOST" ]]; then
        warn "Deployed as ${DEPLOYED_PUBLIC_SCHEME}://${DEPLOYED_PUBLIC_HOST}, but ${PUBLIC_SCHEME}://${PUBLIC_HOST} applies now; run '$(script_cmd) restart' to follow."
      fi
    fi
  else
    printf 'Name:              unknown (the check needs Docker and the Tailscale IPv4); deployed host %s\n' "${DEPLOYED_PUBLIC_HOST:-<none>}"
    printf 'HTTPS:             deployed %s\n' "$([[ "$DEPLOYED_TLS" == 1 ]] && printf 'on (%s)' "${RUNTIME_ENV[TLS_NAME]:-?}" || printf 'off')"
  fi

  printf 'URLs:\n'
  base="${DEPLOYED_PUBLIC_SCHEME}://${DEPLOYED_PUBLIC_HOST:-<ts-ip>}"
  printf '  JupyterLab:  %s:%s/lab\n' "$base" "$DEPLOYED_JUPYTER_PORT"
  if [[ "$DEPLOYED_STATS_ENABLED" == 1 ]]; then
    printf '  Statistics:  %s:%s/\n' "$base" "$DEPLOYED_STATS_PORT"
    printf '  Packages:    %s:%s/dependencies\n' "$base" "$DEPLOYED_STATS_PORT"
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
    printf '  Root state:        TS_IP=%s PORTS=%s FIREWALL=%s%s\n' \
      "${ROOT_STATE[TS_IP]:-?}" "${ROOT_STATE[PORTS]:-?}" "${ROOT_STATE[FIREWALL]:-?}" \
      "${ROOT_STATE[TLS_NAME]:+ TLS_NAME=${ROOT_STATE[TLS_NAME]}}"
  else
    printf '  Root state:        none (%s missing; host-setup has not run)\n' "$ROOT_STATE_FILE"
  fi
  printf '  Certificates:      %s (%s)\n' "$TLS_DIR" "$([[ -d "$TLS_DIR" ]] && printf 'present' || printf 'missing')"
  read -r -a ports <<<"$(deployed_ports)"
  if root_step_needed "${live_ip:-$DEPLOYED_TS_IP}" "${ports[@]}"; then
    root_step_args "${live_ip:-$DEPLOYED_TS_IP}" "${ports[@]}"
    printf "  Root step needed:  yes -> sudo %s %s\n" "$(printf '%q' "$SCRIPT_PATH")" "${ROOT_ARGS[*]}"
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
  if is_installed && compose --profile stats --profile ai down --remove-orphans --volumes --rmi all; then
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
  for image in "$JUPYTER_IMAGE" "$STATS_IMAGE" "$AI_IMAGE"; do
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

  if [[ -e "$SYSCTL_FILE" || -e "$ROOT_STATE_FILE" || -e "$DEFAULT_TLS_DIR" ]]; then
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
published only on this machine's Tailscale IPv4 address, reachable by its MagicDNS name
and served over HTTPS when the tailnet allows it.

Usage: $cmd <command> [options]

Commands:
  install                            Build and start the stack. Creates the settings file
                                     with defaults when it is missing.
  update                             Apply changed settings and refresh the base image
                                     (docker compose build --pull).
  start                              Start the containers; follows a changed Tailscale IP,
                                     name or certificate (renewed when due).
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
  host-setup <tailscale-ipv4> <port> [<port>] [--cert <magicdns-name> <gid>]
                                     net.ipv4.ip_nonlocal_bind=1 and, when ufw/firewalld is
                                     active, rules limiting the ports to ${TAILSCALE_IFACE}.
                                     --cert: 'tailscale cert' for the full MagicDNS name into
                                     ${DEFAULT_TLS_DIR} (group <gid> may read the key).
  host-teardown                      Undo host-setup, certificates included.

Settings file: $SETTINGS_FILE
  JUPYTER_PASSWORD, JUPYTER_PORT (8888), STATS_ENABLED (1), STATS_PORT (8889), STATS_USER (jupyter),
  THEME (amazing; a file name from stack/theme without .json),
  HTTPS (auto | off; default auto),
  NVIDIA (auto | 1 | 0; default auto: 1 expects an NVIDIA GPU, 0 never uses one)

HTTPS='auto': when MagicDNS and HTTPS Certificates are enabled in the tailnet (Tailscale admin
console, DNS page), the root step issues a certificate for <name>.<tailnet>.ts.net and renews it
when fewer than ${CERT_RENEW_DAYS} days are left (on install, update and start); JupyterLab and
the dashboard then serve HTTPS on the same ports. Without a certificate the pages stay on HTTP:
by name when MagicDNS works, else by IP. HTTPS='off' always uses HTTP.

Pages (<host>: the MagicDNS name, else the Tailscale IP; https when a certificate is in use):
  http(s)://<host>:<JUPYTER_PORT>/lab           JupyterLab
  http(s)://<host>:<STATS_PORT>/                Statistics      (when STATS_ENABLED=1)
  http(s)://<host>:<STATS_PORT>/dependencies    Kernel packages (when STATS_ENABLED=1)

Environment overrides:
  JLT_SETTINGS_FILE   settings file              (default: <script dir>/.env)
  JLT_APP_DIR         deployed stack directory   (default: ~/.local/share/${PROJECT})
  JLT_WORKSPACE_DIR   notebook workspace         (default: ~/jupyter-workspace)
  JLT_REQUIREMENTS_FILE  optional Jupyter packages, installed on install/update
                      into the custom packages environment (default: <script dir>/requirements.txt)
  JLT_GPU             auto | on | off            (overrides NVIDIA for install/update)
  JLT_TLS_DIR         certificate directory, for tests only; host-setup always writes
                      ${DEFAULT_TLS_DIR}
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
