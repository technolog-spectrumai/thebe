#!/usr/bin/env bash
# End-to-end check that notebooks can use the NVIDIA GPU with PyTorch.
#
#   ./verify_cuda.sh                        check the GPU path; test PyTorch if it is installed
#   ./verify_cuda.sh --install              also install PyTorch (CUDA build from PyPI, about
#                                           3 GB) through the Dependencies page when it is missing
#   ./verify_cuda.sh --install --cleanup    install, verify, then restore the previous package list
#
# Nothing on the host changes, and the password never passes through this script:
#   - calls to the Dependencies page run inside the stats container, which already holds the
#     password as a mounted secret;
#   - the checks run in a real kernel started through JupyterLab's python3 kernelspec, so they
#     see exactly the environment a notebook gets (image packages plus the Dependencies venv).
#
# Environment:
#   TORCH_REQUIREMENTS   requirement lines to install (default: torch). Newline-separated, e.g.
#                        $'--extra-index-url https://download.pytorch.org/whl/cpu\ntorch==2.14.0+cpu'
#                        to exercise the flow with the small CPU build (which then fails the CUDA check).
#   MIN_FREE_GB          refuse to install with less free disk than this (default: 8)
#
# Exit codes: 0 PyTorch uses the GPU; 1 a check failed; 2 usage; 3 the stack is not running;
#             4 the GPU path works but PyTorch is not installed (run with --install).
set -Eeuo pipefail

readonly PROJECT='jupyterlab-tailscale'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SCRIPT_DIR
readonly INSTALLER="$SCRIPT_DIR/setup-jupyterlab-tailscale.sh"
readonly TORCH_REQUIREMENTS="${TORCH_REQUIREMENTS:-torch}"
readonly MIN_FREE_GB="${MIN_FREE_GB:-8}"

INSTALL=0
CLEANUP=0
ASSUME_YES=0
INSTALLED_BY_US=0
ORIGINAL_REQUIREMENTS_B64=''
STATS=''
JUPYTER=''

use_colour() { [[ -t 1 && -z "${NO_COLOR:-}" ]]; }
say() { printf '%s\n' "$*"; }
info() { if use_colour; then printf '\033[1;34m==>\033[0m %s\n' "$*"; else printf '==> %s\n' "$*"; fi; }
good() { if use_colour; then printf '  \033[32mok\033[0m    %s\n' "$*"; else printf '  ok    %s\n' "$*"; fi; }
bad() { if use_colour; then printf '  \033[31mFAIL\033[0m  %s\n' "$*"; else printf '  FAIL  %s\n' "$*"; fi; }
die() {
  local code="$1"
  shift
  printf 'ERROR: %s\n' "$*" >&2
  exit "$code"
}

usage() {
  sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

# --------------------------------------------------------------------------
# Code that runs inside the containers (passed with python -c, never via the host's Python)
# --------------------------------------------------------------------------

# Runs in the stats container: talks to the Dependencies page on its own loopback address,
# with the Basic credentials from the mounted secret and the headers its CSRF guard requires.
# With HTTPS on (JLT_TLS_CERT in the container's environment) the dashboard speaks TLS on the
# same port; the certificate names the public MagicDNS host, not 127.0.0.1, so it is not verified.
read -r -d '' DEPS_API_PY <<'PY' || true
import base64, json, os, ssl, sys, time, urllib.error, urllib.request

if os.environ.get("JLT_TLS_CERT"):
    BASE = "https://127.0.0.1:8889"
    TLS = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    TLS.check_hostname = False
    TLS.verify_mode = ssl.CERT_NONE
else:
    BASE = "http://127.0.0.1:8889"
    TLS = None
user = os.environ.get("STATS_USER", "jupyter")
with open(os.environ.get("JUPYTER_PASSWORD_FILE", "/run/secrets/jupyter_password"), encoding="utf-8") as fh:
    password = fh.read().rstrip("\n")
AUTH = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
del password


def call(method, path, body=None, timeout=30):
    headers = {"Authorization": AUTH, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers.update({"Content-Type": "application/json", "X-Requested-With": "thebe"})
    request = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=TLS) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.load(exc)
        except ValueError:
            return exc.code, {"error": str(exc.reason)}
    except OSError as exc:
        return 0, {"error": str(exc)}


def fail(message, errors=()):
    print(f"error={message}")
    for item in errors:
        print(f"error_line={item.get('line')}: {item.get('message')}")
    sys.exit(1)


command = sys.argv[1]
if command == "summary":
    code, state = call("GET", "/api/dependencies")
    if code != 200:
        fail(state.get("error", f"HTTP {code}"))
    print(f"status={state['job']['status']}")
    print(f"free_bytes={state['sizes'].get('free_bytes') or 0}")
    print("torch_installed=" + ("1" if any(p["name"].lower() == "torch" for p in state["installed"]) else "0"))
    print("requirements_b64=" + base64.b64encode((state.get("requirements") or "").encode()).decode())
elif command == "put":
    text = base64.b64decode(sys.argv[2]).decode()
    code, body = call("PUT", "/api/dependencies/requirements", {"text": text})
    if code != 200:
        fail(body.get("error", f"HTTP {code}"), body.get("errors") or ())
elif command == "run":
    code, body = call("POST", "/api/dependencies/jobs", {"action": sys.argv[2]})
    if code != 202:
        fail(body.get("error", f"HTTP {code}"))
    offset, pending = 0, ""
    while True:
        code, chunk = call("GET", f"/api/dependencies/log?offset={offset}")
        if code == 200:
            if chunk.get("reset"):
                pending = ""
            pending += chunk.get("text", "")
            offset = chunk.get("next_offset", offset)
            *lines, pending = pending.split("\n")
            for line in lines:
                if line.strip():
                    print("      | " + line, flush=True)
            if not chunk.get("running"):
                code, state = call("GET", "/api/dependencies")
                if code == 200 and state["job"]["status"] != "running":
                    if pending.strip():
                        print("      | " + pending, flush=True)
                    job = state["job"]
                    print(f"status={job['status']}")
                    print(f"message={job.get('message') or ''}")
                    sys.exit(0 if job["status"] == "succeeded" else 1)
        time.sleep(2)
elif command == "cancel":
    call("POST", "/api/dependencies/cancel", {})
elif command == "cache-clear":
    code, body = call("POST", "/api/dependencies/cache/clear", {})
    if code != 200:
        fail(body.get("error", f"HTTP {code}"))
else:
    fail(f"unknown command {command}")
PY

# Runs in the jupyterlab container: starts a kernel through the python3 kernelspec (the same
# launcher notebooks use) and reports key=value lines.
read -r -d '' KERNEL_CHECK_PY <<'PY' || true
import sys
from jupyter_client.manager import start_new_kernel

CODE = r'''
import ctypes, time
report = {}
try:
    lib = ctypes.CDLL("libcuda.so.1")
    report["cuinit"] = lib.cuInit(0)
    count = ctypes.c_int()
    lib.cuDeviceGetCount(ctypes.byref(count))
    report["devices"] = count.value
except OSError as exc:
    report["libcuda_error"] = exc
try:
    import torch
except Exception as exc:
    report["torch_error"] = f"{type(exc).__name__}: {exc}"
else:
    report["torch"] = torch.__version__
    report["torch_file"] = torch.__file__
    report["torch_cuda_build"] = torch.version.cuda
    report["cuda_available"] = torch.cuda.is_available()
    if report["cuda_available"]:
        report["device"] = torch.cuda.get_device_name(0)
        a = torch.randn(1024, 1024, device="cuda")
        b = torch.randn(1024, 1024, device="cuda")
        torch.cuda.synchronize()
        started = time.perf_counter()
        c = a @ b
        torch.cuda.synchronize()
        report["matmul_ms"] = round((time.perf_counter() - started) * 1000, 2)
        report["matmul_matches_cpu"] = bool(torch.allclose(c.cpu(), a.cpu() @ b.cpu(), rtol=1e-3, atol=1e-3))
for key, value in report.items():
    print(f"{key}={str(value).splitlines()[0] if str(value) else ''}")
'''

km, kc = start_new_kernel(kernel_name="python3", startup_timeout=120)
output, errors = [], []


def hook(message):
    content = message["content"]
    if message["msg_type"] == "stream":
        output.append(content.get("text", ""))
    elif message["msg_type"] == "error":
        errors.append(f"{content.get('ename')}: {content.get('evalue')}")


try:
    kc.execute_interactive(CODE, timeout=300, output_hook=hook)
finally:
    kc.stop_channels()
    km.shutdown_kernel(now=True)
sys.stdout.write("".join(output))
for error in errors:
    print(f"kernel_error={error}")
PY

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

container_of() {
  docker ps --filter "label=com.docker.compose.project=$PROJECT" \
    --filter "label=com.docker.compose.service=$1" --filter status=running \
    --format '{{.Names}}' | head -n 1
}

deps_api() {
  docker exec "$STATS" python -c "$DEPS_API_PY" "$@"
}

# key=value output -> the value of one key.
value_of() {
  local key="$1" line
  while IFS= read -r line; do
    if [[ "$line" == "$key="* ]]; then
      printf '%s\n' "${line#*=}"
      return 0
    fi
  done
  return 1
}

run_kernel_check() {
  local errfile
  errfile="$(mktemp)"
  if ! KERNEL_REPORT="$(docker exec "$JUPYTER" python -c "$KERNEL_CHECK_PY" 2>"$errfile")"; then
    bad 'could not run code in a notebook kernel:'
    sed 's/^/        /' "$errfile" | tail -n 15 >&2
    rm -f -- "$errfile"
    exit 1
  fi
  rm -f -- "$errfile"
}

report() { value_of "$1" <<<"$KERNEL_REPORT" || true; }

# shellcheck disable=SC2329  # called through the trap below
on_interrupt() {
  if [[ -n "$STATS" && "$INSTALLED_BY_US" == running ]]; then
    printf '\nInterrupted: cancelling the package install...\n' >&2
    deps_api cancel >/dev/null 2>&1 || true
  fi
  exit 130
}
trap on_interrupt INT TERM

confirm_download() {
  ((ASSUME_YES)) && return 0
  if [[ ! -t 0 ]]; then
    die 2 'refusing to start a large download without confirmation: run it in a terminal or pass --yes.'
  fi
  printf 'Install "%s" from the Dependencies page? PyTorch with CUDA is about 3 GB. [y/N] ' \
    "${TORCH_REQUIREMENTS//$'\n'/ | }"
  local answer=''
  read -r answer || true
  [[ "$answer" == [yY] || "$answer" == [yY][eE][sS] ]] || die 1 'cancelled.'
}

wait_idle() {
  local summary status i
  for i in $(seq 1 150); do
    summary="$(deps_api summary)" || die 1 "the Dependencies page does not answer: $(value_of error <<<"$summary" || true)"
    status="$(value_of status <<<"$summary")"
    [[ "$status" != running ]] && return 0
    ((i == 1)) && info 'Waiting for the package job that is already running...'
    sleep 2
  done
  die 1 'a package job is still running after 5 minutes; try again later.'
}

install_torch() {
  STATS="$(container_of stats)"
  [[ -n "$(container_of deps)" && -n "$STATS" ]] ||
    die 3 "the Dependencies page is not running. Enable statistics (STATS_ENABLED='1') and run: $INSTALLER update"
  wait_idle

  local summary free_bytes min_bytes original new_text
  summary="$(deps_api summary)"
  free_bytes="$(value_of free_bytes <<<"$summary")"
  ORIGINAL_REQUIREMENTS_B64="$(value_of requirements_b64 <<<"$summary")"
  min_bytes=$((MIN_FREE_GB * 1024 * 1024 * 1024))
  if ((free_bytes < min_bytes)); then
    die 1 "only $((free_bytes / 1024 / 1024 / 1024)) GB free on the Docker disk; at least ${MIN_FREE_GB} GB is needed (set MIN_FREE_GB to override)."
  fi
  confirm_download

  original="$(printf '%s' "$ORIGINAL_REQUIREMENTS_B64" | base64 -d)"
  if [[ "$original" == *download.pytorch.org/whl/cpu* ]]; then
    say '  note  the saved requirements already point pip at the CPU-only PyTorch index;'
    say '        the result will be a CPU build unless that line is removed.'
  fi
  new_text="${original%$'\n'}"
  new_text="${new_text:+$new_text$'\n'}# added by verify_cuda.sh"$'\n'"$TORCH_REQUIREMENTS"$'\n'

  info 'Saving the requirements on the Dependencies page...'
  local put_output
  if ! put_output="$(deps_api put "$(printf '%s' "$new_text" | base64 -w0)")"; then
    printf '%s\n' "$put_output" | sed 's/^error[_a-z]*=/        /' >&2
    die 1 'the Dependencies page refused the requirements.'
  fi
  INSTALLED_BY_US=running
  info 'Installing (pip output follows; this can take a while)...'
  local run_output rc=0
  # Show pip's output while capturing it. Not `tee /dev/stderr`: that reopens the file behind
  # stderr, and when the output is redirected to a file it writes from offset 0 over what is there.
  run_output="$(deps_api run install | while IFS= read -r line; do
    [[ "$line" == status=* || "$line" == message=* ]] || printf '%s\n' "$line" >&2
    printf '%s\n' "$line"
  done)" || rc=$?
  INSTALLED_BY_US=1
  if ((rc != 0)); then
    bad "package install $(value_of status <<<"$run_output" || printf 'failed'): $(value_of message <<<"$run_output" || true)"
    return 1
  fi
  good 'package install succeeded'
}

cleanup_packages() {
  info 'Cleaning up: restoring the previous requirements and reinstalling them from scratch...'
  deps_api put "$ORIGINAL_REQUIREMENTS_B64" >/dev/null || {
    bad 'could not restore the previous requirements; fix them on the Dependencies page.'
    return 1
  }
  if deps_api run reset >/dev/null; then
    good 'previous package list reinstalled'
  else
    bad 'the reset job failed; see the log on the Dependencies page.'
  fi
  deps_api cache-clear >/dev/null && good 'download cache cleared'
}

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

while (($#)); do
  case "$1" in
    --install) INSTALL=1 ;;
    --cleanup) CLEANUP=1 ;;
    --yes | -y) ASSUME_YES=1 ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die 2 "unknown option: $1"
      ;;
  esac
  shift
done
[[ "$MIN_FREE_GB" =~ ^[0-9]+$ ]] || die 2 'MIN_FREE_GB must be a whole number.'
((CLEANUP == 0 || INSTALL == 1)) || die 2 '--cleanup only makes sense together with --install.'

command -v docker >/dev/null 2>&1 || die 3 'docker is not installed.'
docker info >/dev/null 2>&1 || die 3 'the Docker daemon is not reachable (is your user in the docker group?).'

JUPYTER="$(container_of jupyterlab)"
[[ -n "$JUPYTER" ]] || die 3 "JupyterLab is not running. Start it with: $INSTALLER start"

info 'GPU access'
if command -v nvidia-smi >/dev/null 2>&1 && host_gpu="$(nvidia-smi -L 2>/dev/null | head -n 1)" && [[ -n "$host_gpu" ]]; then
  good "host: $host_gpu"
else
  bad 'host: nvidia-smi does not list a GPU (driver missing?)'
fi
requests="$(docker inspect -f '{{json .HostConfig.DeviceRequests}}' "$JUPYTER")"
if [[ "$requests" == null || "$requests" == '[]' ]]; then
  bad 'the jupyterlab container was started without GPU access'
  die 1 "enable it with: JLT_GPU=on $INSTALLER update"
fi
good 'jupyterlab container has a GPU device request'
if container_gpu="$(docker exec "$JUPYTER" nvidia-smi -L 2>/dev/null | head -n 1)" && [[ -n "$container_gpu" ]]; then
  good "inside jupyterlab: $container_gpu"
else
  bad 'nvidia-smi inside the jupyterlab container lists no GPU'
  exit 1
fi

info 'Notebook kernel'
run_kernel_check
if [[ "$(report cuinit)" == 0 && "$(report devices)" -ge 1 ]]; then
  good "libcuda from a kernel: cuInit 0, $(report devices) device(s)"
else
  bad "libcuda from a kernel: $(report libcuda_error)${KERNEL_REPORT:+ }$(report cuinit)"
  exit 1
fi

if [[ -z "$(report torch)" ]]; then
  if ((INSTALL == 0)); then
    say "  --    PyTorch is not installed in the notebook environment ($(report torch_error))."
    say '        Run ./verify_cuda.sh --install to install it from the Dependencies page (about 3 GB).'
    exit 4
  fi
  info 'PyTorch'
  if install_torch; then
    info 'Notebook kernel (again)'
    run_kernel_check
  fi
fi

verdict=1
torch_version="$(report torch)"
if [[ -n "$torch_version" ]]; then
  good "PyTorch $torch_version (CUDA build: $(report torch_cuda_build)) from $(report torch_file)"
  if [[ "$(report cuda_available)" == True ]]; then
    good "torch.cuda.is_available(): True on $(report device)"
    if [[ "$(report matmul_matches_cpu)" == True ]]; then
      good "1024x1024 matrix multiply on the GPU: $(report matmul_ms) ms, matches the CPU result"
      verdict=0
    else
      bad 'the GPU matrix multiply did not match the CPU result'
    fi
  elif [[ "$(report torch_cuda_build)" == None ]]; then
    bad 'torch.cuda.is_available(): False — this is a CPU-only build of PyTorch'
  else
    bad 'torch.cuda.is_available(): False although the GPU is visible (driver/CUDA version mismatch?)'
  fi
else
  bad "PyTorch is still not importable: $(report torch_error)"
fi
kernel_error="$(report kernel_error)"
[[ -z "$kernel_error" ]] || bad "kernel error: $kernel_error"

if ((CLEANUP)) && [[ "$INSTALLED_BY_US" == 1 ]]; then
  cleanup_packages || true
fi

if ((verdict == 0)); then
  info 'PASS: notebooks can use the GPU with PyTorch.'
else
  info 'FAIL: see the lines marked FAIL above.'
fi
exit "$verdict"
