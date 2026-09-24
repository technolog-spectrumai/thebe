"""Spike demo: Kernel -> Interrupt during a slow %%ai request stops it and inserts nothing.

    /tmp/ai/bin/python spike/interrupt_demo.py      (same venv as kernel_demo.py)
"""
import json, os, tempfile, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from jupyter_client.manager import start_new_kernel

class Slow(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        time.sleep(30)
server = ThreadingHTTPServer(("127.0.0.1", 0), Slow)
threading.Thread(target=server.serve_forever, daemon=True).start()
conf = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
json.dump({"default": "claude", "timeout": 25, "max_retries": 0, "providers": {"claude": {"api": "anthropic",
    "model": "m", "api_key": "sk-ant-demo-key-0000", "base_url": f"http://127.0.0.1:{server.server_port}"}}}, conf); conf.close()
km, kc = start_new_kernel(kernel_name="python3", env=dict(os.environ, THEBE_AI_CONFIG=conf.name, PYTHONPATH=os.path.dirname(os.path.abspath(__file__))))
try:
    kc.execute_interactive("%load_ext thebe_ai", timeout=30)
    out = []
    threading.Timer(3, km.interrupt_kernel).start()
    start = time.monotonic()
    reply = kc.execute_interactive("%%ai\nslow request", timeout=40,
        output_hook=lambda m: out.append(m["content"].get("text", "")) if m["msg_type"] == "stream" else None)
    print(f"status={reply['content']['status']} payload={reply['content'].get('payload')} after={time.monotonic()-start:.1f}s output={''.join(out)!r}")
finally:
    kc.stop_channels(); km.shutdown_kernel(now=True); server.shutdown(); os.unlink(conf.name)
