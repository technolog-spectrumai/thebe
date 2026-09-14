# JupyterLab over Tailscale

A small single-user installer that runs JupyterLab on a Linux laptop and makes it reachable from a tablet or another device through Tailscale.

The installer creates an isolated Python environment, configures password authentication, restricts JupyterLab to the laptop's Tailscale IPv4 address, opens the selected port on the Tailscale firewall interface, and installs a persistent `systemd` user service.

## What it does

- Installs JupyterLab in a dedicated Python virtual environment.
- Creates `~/jupyter-workspace` as the visible JupyterLab workspace.
- Listens only on the laptop's Tailscale IPv4 address.
- Uses TCP port `8888`.
- Requires a password and disables token login.
- Supports Fedora and Ubuntu/Debian package managers.
- Configures either `firewalld` or UFW when one is active.
- Runs JupyterLab as a restartable `systemd` user service.

## Requirements

- A Linux laptop using `systemd`.
- Tailscale installed, connected, and authenticated.
- The tablet connected to the same Tailscale network.
- A normal user account with `sudo` access.
- Internet access during installation so `pip` can install JupyterLab.

Check Tailscale before installing:

```bash
tailscale status
tailscale ip -4
```

If Tailscale is installed but disconnected:

```bash
sudo tailscale up
```

## Installation

Download `setup-jupyterlab-tailscale.sh`, then run it as your normal desktop user. Do not run the whole script with `sudo`.

```bash
chmod 700 setup-jupyterlab-tailscale.sh
./setup-jupyterlab-tailscale.sh
```

The script may request the `sudo` password while installing system packages or changing firewall rules. JupyterLab itself runs under your normal user account.

After installation, the script prints an address similar to:

```text
http://100.x.y.z:8888/lab
```

Open that address on the tablet while both devices are connected to Tailscale.

## Login

The default hardcoded password is:

```text
TailLab-7mK9-vQ2x-N4pR!
```

To change it, edit this line near the beginning of the installer before running it:

```bash
readonly JUPYTER_PASSWORD='replace-with-a-long-unique-password'
```

The plaintext password remains inside the installer. The generated Jupyter configuration stores a password hash and is readable only by the user.

## Installed locations

| Purpose | Location |
| --- | --- |
| Workspace | `~/jupyter-workspace` |
| Python environment | `~/.local/share/jupyterlab-tailscale/venv` |
| Installed launcher | `~/.local/bin/jupyterlab-tailscale` |
| Jupyter configuration | `~/.config/jupyterlab-tailscale/jupyter_server_config.py` |
| User service | `~/.config/systemd/user/jupyterlab-tailscale.service` |

Files uploaded through the JupyterLab browser are placed inside `~/jupyter-workspace` unless another directory is selected from within the workspace.

## Service commands

Show status:

```bash
systemctl --user status jupyterlab-tailscale
```

Start, stop, or restart JupyterLab:

```bash
systemctl --user start jupyterlab-tailscale
systemctl --user stop jupyterlab-tailscale
systemctl --user restart jupyterlab-tailscale
```

Follow logs:

```bash
journalctl --user -u jupyterlab-tailscale -f
```

Disable automatic startup:

```bash
systemctl --user disable --now jupyterlab-tailscale
```

## Network and security model

JupyterLab binds directly to the Tailscale IPv4 address instead of `0.0.0.0`. It therefore does not listen on the laptop's ordinary Wi-Fi, Ethernet, or public IP addresses.

The browser URL uses HTTP, but traffic between Tailscale devices travels through Tailscale's encrypted tunnel. Password authentication still protects JupyterLab from other devices that may be members of the same tailnet.

For tighter access control, add a Tailscale policy allowing TCP port `8888` only from the tablet to the laptop. The hardcoded password should be unique and the installer should have restrictive permissions:

```bash
chmod 700 setup-jupyterlab-tailscale.sh
```

Do not expose port `8888` through a router, public firewall, public reverse proxy, or Tailscale Funnel.

## Firewall behavior

When `firewalld` is active, the installer opens TCP port `8888` in the zone assigned to `tailscale0`. If that interface has no zone, it creates a `jupyter-tailnet` zone and assigns `tailscale0` to it.

When UFW is active, it adds a rule restricted to the `tailscale0` interface. If neither firewall manager is active, no firewall rule is created; binding to the Tailscale address still prevents JupyterLab from listening on ordinary network addresses.

## Resource statistics

JupyterLab does not provide a complete laptop monitoring dashboard by default. A resource-usage extension can add lightweight CPU and memory information inside JupyterLab, while a separate authenticated FastAPI service is more suitable when statistics must also be consumed by a tablet dashboard or a future Tauri client.

The current installer does not install or expose a separate statistics service.

## Troubleshooting

### The tablet cannot connect

Confirm that both devices appear in the same tailnet and that the laptop is reachable:

```bash
tailscale status
tailscale ip -4
```

From a terminal on another Tailscale device, test the laptop:

```bash
tailscale ping <laptop-name-or-tailscale-ip>
```

Then check the service and its logs:

```bash
systemctl --user status jupyterlab-tailscale
journalctl --user -u jupyterlab-tailscale -n 100 --no-pager
```

### Port 8888 is already used

Change the constant near the top of the installer:

```bash
readonly JUPYTER_PORT='8890'
```

Run the installer again and restart the service. Use the new port in the tablet URL.

### Tailscale starts after the service

The launcher waits for up to 60 seconds for a Tailscale IPv4 address. If the service still fails, connect Tailscale and restart JupyterLab:

```bash
sudo tailscale up
systemctl --user restart jupyterlab-tailscale
```

### JupyterLab should remain available after logout

Enable lingering for your user if your Linux configuration stops user services after logout:

```bash
sudo loginctl enable-linger "$(id -un)"
```

## Updating JupyterLab

Run the installer again to update `pip` and JupyterLab, then restart the service:

```bash
./setup-jupyterlab-tailscale.sh
systemctl --user restart jupyterlab-tailscale
```

