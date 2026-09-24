"""Spike demo in a real browser: JupyterLab 4.6 + the ✨ AI cell button + the %%ai magic.

Starts JupyterLab (foreground, stopped at the end) with a stand-in for the Anthropic/OpenAI
APIs in this process, opens a notebook in headless Chromium and checks two flows:

  1. A cell containing only a plain-language prompt, then the "✨ AI" button in the cell
     toolbar -> the cell is replaced by the generated code (prompt kept as # ai: comments),
     and the generated code is not run.
  2. A %%ai cell run with Shift+Enter -> the code appears in a NEW cell below, not run.

Needs: jupyterlab, ipykernel, openai, anthropic, playwright in the venv, the extension built
from spike/labextension and copied to <venv>/share/jupyter/labextensions/thebe-ai-cell.

    python spike/browser_demo.py [screenshot-dir]
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

SPIKE = Path(__file__).resolve().parent
sys.path.insert(0, str(SPIKE / "tests"))
from test_thebe_ai import Stub  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

TOKEN = "spike-demo-token"
CHROMIUM = os.environ.get("CHROMIUM", "/opt/pw-browsers/chromium")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def saved_cells(page, base: str, name: str) -> list[dict]:
    """Save the notebook (Ctrl+S) and read it back through the contents API: exact sources."""
    page.keyboard.press("Escape")
    page.keyboard.press("Control+s")
    page.wait_for_timeout(1500)
    with urllib.request.urlopen(f"{base}/api/contents/{name}?content=1&token={TOKEN}", timeout=10) as response:
        cells = json.load(response)["content"]["cells"]
    return [{"source": c["source"], "count": c.get("execution_count"),
             "text": "".join(o.get("text", "") for o in c.get("outputs", []))} for c in cells]


def main(shots: Path) -> int:
    stub = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    api = f"http://127.0.0.1:{stub.server_port}"
    work = Path(tempfile.mkdtemp(prefix="thebe-ai-browser-"))
    (work / "ai.json").write_text(json.dumps({"default": "claude", "timeout": 20, "max_retries": 0, "providers": {
        "claude": {"api": "anthropic", "model": "claude-sonnet-5", "api_key": "sk-ant-demo-key-0000", "base_url": api}}}))
    # The magic is loaded by kernel config, as the proposed MVP would do (no %load_ext).
    profile = work / "ipython" / "profile_default"
    profile.mkdir(parents=True)
    (profile / "ipython_kernel_config.py").write_text("c.InteractiveShellApp.extensions = ['thebe_ai']\n")
    notebooks = work / "notebooks"
    notebooks.mkdir()
    empty = {"cell_type": "code", "metadata": {}, "source": "", "outputs": [], "execution_count": None}
    for name in ("button.ipynb", "magic.ipynb"):
        (notebooks / name).write_text(json.dumps({"cells": [empty], "metadata": {"kernelspec": {
            "name": "python3", "display_name": "Python 3", "language": "python"}}, "nbformat": 4, "nbformat_minor": 5}))

    port = free_port()
    log = open(work / "lab.log", "wb")
    env = dict(os.environ, THEBE_AI_CONFIG=str(work / "ai.json"), PYTHONPATH=str(SPIKE), IPYTHONDIR=str(work / "ipython"))
    lab = subprocess.Popen([sys.executable, "-m", "jupyterlab", "--no-browser", f"--port={port}",
                            f"--IdentityProvider.token={TOKEN}", f"--ServerApp.root_dir={notebooks}",
                            f"--LabApp.workspaces_dir={work / 'workspaces'}",
                            *(["--allow-root"] if os.geteuid() == 0 else [])],
                           env=env, stdout=log, stderr=subprocess.STDOUT)
    failures = []
    page = None
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(240):
            try:
                urllib.request.urlopen(f"{base}/api/status?token={TOKEN}", timeout=2)
                break
            except OSError:
                if lab.poll() is not None:
                    break
                time.sleep(0.5)
        else:
            lab.poll()
        if lab.returncode is not None:
            raise SystemExit("JupyterLab did not start:\n" + (work / "lab.log").read_text()[-3000:])
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROMIUM)
            page = browser.new_page(viewport={"width": 1200, "height": 800})

            # Flow 1: the whole cell is the prompt; the toolbar button does the rest.
            page.goto(f"{base}/lab/tree/button.ipynb?reset&token={TOKEN}")
            page.wait_for_selector(".jp-NotebookPanel:not(.lm-mod-hidden) .jp-Cell .cm-content", timeout=60000)
            page.wait_for_selector(".jp-Notebook-ExecutionIndicator[data-status='idle']", timeout=60000)
            page.click(".jp-NotebookPanel:not(.lm-mod-hidden) .jp-Cell .cm-content")
            page.keyboard.type("print pi to the console")
            page.keyboard.press("Escape")                           # command mode; the cell stays active
            page.click(".jp-cell-toolbar button:has-text('AI'), .jp-cell-toolbar jp-button:has-text('AI')")
            page.wait_for_function("""() => [...document.querySelectorAll('.jp-NotebookPanel:not(.lm-mod-hidden) .jp-Cell .cm-content')]
                .some(e => e.innerText.includes('import math'))""", timeout=30000)
            page.wait_for_timeout(500)
            page.screenshot(path=str(shots / "button.png"))
            cells = saved_cells(page, base, "button.ipynb")
            print("flow 1 cells:", json.dumps(cells, indent=1))
            if [c["source"] for c in cells] != ["# ai: print pi to the console\nimport math\nprint(math.pi)\n"]:
                failures.append("flow 1: the cell was not replaced by the generated code")
            if "3.14159" in cells[0]["text"] or "now holds the generated code" not in cells[0]["text"]:
                failures.append("flow 1: the generated code was run, or the magic's note is missing")

            # Flow 2: a %%ai cell run by hand; the code goes into a new cell below.
            page = browser.new_context(viewport={"width": 1200, "height": 800}).new_page()
            page.goto(f"{base}/lab/tree/magic.ipynb?reset&token={TOKEN}")
            page.wait_for_selector(".jp-NotebookPanel:not(.lm-mod-hidden) .jp-Cell .cm-content", timeout=60000)
            page.wait_for_selector(".jp-Notebook-ExecutionIndicator[data-status='idle']", timeout=60000)
            page.click(".jp-NotebookPanel:not(.lm-mod-hidden) .jp-Cell .cm-content")
            page.keyboard.type("%%ai\nprint pi")
            page.keyboard.press("Shift+Enter")
            try:
                page.wait_for_function("""() => [...document.querySelectorAll('.jp-NotebookPanel:not(.lm-mod-hidden) .jp-Cell .cm-content')]
                    .some(e => e.innerText.startsWith('import math'))""", timeout=30000)
            except Exception:
                page.screenshot(path=str(shots / "failure.png"))
                print("DEBUG cells:", page.evaluate("""() => [...document.querySelectorAll('.jp-NotebookPanel:not(.lm-mod-hidden) .jp-Cell')]
                    .map(c => c.innerText)"""))
                raise
            page.wait_for_timeout(800)
            page.screenshot(path=str(shots / "magic.png"))
            cells = saved_cells(page, base, "magic.ipynb")
            print("flow 2 cells:", json.dumps(cells, indent=1))
            # Shift+Enter on the last cell also adds an empty cell at the end (JupyterLab's own behaviour).
            if [c["source"] for c in cells[:2]] != ["%%ai\nprint pi", "import math\nprint(math.pi)\n"]:
                failures.append("flow 2: the code was not inserted in a new cell right below")
            elif cells[1]["count"] is not None or cells[1]["text"]:
                failures.append("flow 2: the inserted cell was run")
            browser.close()
    except Exception:
        try:
            page.screenshot(path=str(shots / "failure.png"))
        except Exception:
            pass
        raise
    finally:
        lab.terminate()
        try:
            lab.wait(15)
        except subprocess.TimeoutExpired:
            lab.kill()
        stub.shutdown()
    for failure in failures:
        print("FAILED:", failure)
    print("OK" if not failures else f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.gettempdir())
    out.mkdir(parents=True, exist_ok=True)
    sys.exit(main(out))
