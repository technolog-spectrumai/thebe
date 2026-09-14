#!/usr/bin/env bash
set -Eeuo pipefail

# JupyterLab over Tailscale: single-user installer and service launcher.
# Run this script as your normal desktop user, not as root.

readonly JUPYTER_PASSWORD='TailLab-7mK9-vQ2x-N4pR!'
readonly JUPYTER_PORT='8888'
readonly INSTALL_NAME='jupyterlab-tailscale'

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

account_home() {
  local resolved_home
  resolved_home="$(getent passwd "$(id -un)" | cut -d: -f6)"
  printf '%s\n' "${resolved_home:-${HOME:?Unable to determine user home}}"
}

readonly ACCOUNT_HOME="$(account_home)"
readonly APP_DIR="${ACCOUNT_HOME}/.local/share/${INSTALL_NAME}"
readonly VENV_DIR="${APP_DIR}/venv"
readonly CONFIG_DIR="${ACCOUNT_HOME}/.config/${INSTALL_NAME}"
readonly CONFIG_FILE="${CONFIG_DIR}/jupyter_server_config.py"
readonly WORKSPACE_DIR="${ACCOUNT_HOME}/jupyter-workspace"
readonly INSTALLED_SCRIPT="${ACCOUNT_HOME}/.local/bin/${INSTALL_NAME}"
readonly SERVICE_DIR="${ACCOUNT_HOME}/.config/systemd/user"
readonly SERVICE_FILE="${SERVICE_DIR}/${INSTALL_NAME}.service"

tailscale_ip() {
  tailscale ip -4 2>/dev/null | head -n 1
}

wait_for_tailscale() {
  local ip_address=''
  local attempt
  for attempt in {1..30}; do
    ip_address="$(tailscale_ip)"
    if [[ -n "$ip_address" ]]; then
      printf '%s\n' "$ip_address"
      return 0
    fi
    sleep 2
  done
  return 1
}

write_runtime_config() {
  local ip_address="$1"
  local password_hash

  password_hash="$(JUPYTER_PASSWORD="$JUPYTER_PASSWORD" \
    "$VENV_DIR/bin/python" -c \
    'import os; from jupyter_server.auth.security import passwd; print(passwd(os.environ["JUPYTER_PASSWORD"]))')"

  install -d -m 700 "$CONFIG_DIR" "$WORKSPACE_DIR"
  export CONFIG_FILE WORKSPACE_DIR JUPYTER_PORT ip_address password_hash
  "$VENV_DIR/bin/python" <<'PY'
import os
from pathlib import Path

config = "\n".join([
    f"c.ServerApp.ip = {os.environ['ip_address']!r}",
    f"c.ServerApp.port = {int(os.environ['JUPYTER_PORT'])}",
    "c.ServerApp.port_retries = 0",
    "c.ServerApp.open_browser = False",
    "c.ServerApp.allow_remote_access = True",
    f"c.ServerApp.root_dir = {os.environ['WORKSPACE_DIR']!r}",
    f"c.PasswordIdentityProvider.hashed_password = {os.environ['password_hash']!r}",
    "c.IdentityProvider.token = ''",
    "",
])
Path(os.environ["CONFIG_FILE"]).write_text(config, encoding="utf-8")
PY
  chmod 600 "$CONFIG_FILE"
}

serve() {
  [[ -x "$VENV_DIR/bin/jupyter-lab" ]] || die 'JupyterLab is not installed. Run this script without arguments first.'
  command -v tailscale >/dev/null 2>&1 || die 'Tailscale is not installed.'

  local ip_address
  ip_address="$(wait_for_tailscale)" || die 'Tailscale has no IPv4 address. Run: sudo tailscale up'
  write_runtime_config "$ip_address"

  printf 'Starting JupyterLab at http://%s:%s/lab\n' "$ip_address" "$JUPYTER_PORT"
  exec "$VENV_DIR/bin/jupyter-lab" --config="$CONFIG_FILE"
}

install_python_requirements() {
  if ! command -v python3 >/dev/null 2>&1; then
    if command -v dnf >/dev/null 2>&1; then
      sudo dnf install -y python3 python3-pip
    elif command -v apt-get >/dev/null 2>&1; then
      sudo apt-get update
      sudo apt-get install -y python3 python3-venv python3-pip
    else
      die 'Install Python 3 with venv support, then rerun this script.'
    fi
  fi

  if ! python3 -m venv --help >/dev/null 2>&1; then
    if command -v apt-get >/dev/null 2>&1; then
      sudo apt-get update
      sudo apt-get install -y python3-venv
    else
      die 'Python venv support is missing. Install the python3-venv equivalent for this system.'
    fi
  fi
}

configure_firewall() {
  local zone

  if command -v firewall-cmd >/dev/null 2>&1 && sudo firewall-cmd --state >/dev/null 2>&1; then
    zone="$(sudo firewall-cmd --get-zone-of-interface=tailscale0 2>/dev/null || true)"
    if [[ -z "$zone" || "$zone" == 'no zone' ]]; then
      zone='jupyter-tailnet'
      sudo firewall-cmd --permanent --get-zones | tr ' ' '\n' | grep -Fxq "$zone" || \
        sudo firewall-cmd --permanent --new-zone="$zone"
      sudo firewall-cmd --permanent --zone="$zone" --add-interface=tailscale0
    fi
    sudo firewall-cmd --permanent --zone="$zone" --add-port="${JUPYTER_PORT}/tcp"
    sudo firewall-cmd --reload
    printf 'Opened TCP %s only in firewalld zone %s (tailscale0).\n' "$JUPYTER_PORT" "$zone"
  elif command -v ufw >/dev/null 2>&1 && sudo ufw status | grep -q '^Status: active'; then
    sudo ufw allow in on tailscale0 to any port "$JUPYTER_PORT" proto tcp
    printf 'Opened TCP %s only on tailscale0 in UFW.\n' "$JUPYTER_PORT"
  else
    printf 'No active firewalld/UFW detected; no firewall rule was needed.\n'
    printf 'JupyterLab will still bind only to the Tailscale IPv4 address.\n'
  fi
}

install_service() {
  [[ "$EUID" -ne 0 ]] || die 'Run this script as your normal user, not with sudo.'
  command -v tailscale >/dev/null 2>&1 || die 'Install and connect Tailscale first: https://tailscale.com/download/linux'
  [[ -n "$(tailscale_ip)" ]] || die 'Tailscale is not connected. Run: sudo tailscale up'
  command -v systemctl >/dev/null 2>&1 || die 'This installer requires systemd.'

  install_python_requirements
  install -d -m 700 "$APP_DIR" "$CONFIG_DIR" "$WORKSPACE_DIR"
  install -d -m 755 "$(dirname "$INSTALLED_SCRIPT")" "$SERVICE_DIR"

  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    python3 -m venv "$VENV_DIR"
  fi
  "$VENV_DIR/bin/python" -m pip install --upgrade pip jupyterlab

  install -m 700 "$(realpath "$0")" "$INSTALLED_SCRIPT"

  export SERVICE_FILE INSTALLED_SCRIPT
  python3 <<'PY'
import os
from pathlib import Path

unit = """[Unit]
Description=JupyterLab bound to the Tailscale interface
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
ExecStart={script} serve
Restart=on-failure
RestartSec=5
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=default.target
""".format(script=os.environ["INSTALLED_SCRIPT"])
Path(os.environ["SERVICE_FILE"]).write_text(unit, encoding="utf-8")
PY
  chmod 600 "$SERVICE_FILE"

  configure_firewall
  systemctl --user daemon-reload
  systemctl --user enable --now "$INSTALL_NAME.service"

  local ip_address
  ip_address="$(tailscale_ip)"
  printf '\nInstalled and started successfully.\n'
  printf 'Tablet URL: http://%s:%s/lab\n' "$ip_address" "$JUPYTER_PORT"
  printf 'Password:   %s\n' "$JUPYTER_PASSWORD"
  printf 'Workspace:  %s\n' "$WORKSPACE_DIR"
  printf '\nStatus: systemctl --user status %s\n' "$INSTALL_NAME"
  printf 'Stop:   systemctl --user stop %s\n' "$INSTALL_NAME"
  printf 'Start:  systemctl --user start %s\n' "$INSTALL_NAME"
  printf 'Logs:   journalctl --user -u %s -f\n' "$INSTALL_NAME"
}

case "${1:-install}" in
  install) install_service ;;
  serve) serve ;;
  *) die "Usage: $0 [install|serve]" ;;
esac
