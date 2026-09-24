"""Spike demo: a real ipykernel runs %%ai against an in-process stub API and prints what the
notebook frontend would receive (execute_reply payload) - no API key, no JupyterLab server.

    python -m venv /tmp/ai && /tmp/ai/bin/pip install ipykernel jupyter_client openai anthropic
    /tmp/ai/bin/python spike/kernel_demo.py
"""
import json, os, sys, tempfile, threading
from http.server import ThreadingHTTPServer
SPIKE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SPIKE, "tests"))
from test_thebe_ai import Stub
from jupyter_client.manager import start_new_kernel

server = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{server.server_port}"
conf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
json.dump({"default": "claude", "timeout": 20, "max_retries": 1, "providers": {
    "claude": {"api": "anthropic", "model": "claude-sonnet-5", "api_key": "sk-ant-demo-key-0000", "base_url": url},
    "openai": {"api": "openai", "model": "gpt-5.4-mini", "api_key": "sk-openai-demo-9999", "base_url": url + "/v1"}}}, conf)
conf.close()
env = dict(os.environ, THEBE_AI_CONFIG=conf.name, PYTHONPATH=SPIKE)
km, kc = start_new_kernel(kernel_name="python3", env=env)
try:
    def run(code):
        streams = []
        reply = kc.execute_interactive(code, timeout=60, output_hook=lambda m: streams.append(
            m["content"].get("text", "") if m["msg_type"] == "stream" else ""))
        return reply["content"], "".join(streams)
    for code in ("%load_ext thebe_ai\n%ai status", "%%ai\nprint pi", "%%ai openai --replace\nprint pi here",
                 "print('math' in globals())", "%%ai nosuch\nx"):
        content, out = run(code)
        print(f"--- cell: {code!r}\nstatus={content['status']} payload={json.dumps(content.get('payload'))}\noutput={out!r}")
finally:
    kc.stop_channels(); km.shutdown_kernel(now=True); server.shutdown(); os.unlink(conf.name)
