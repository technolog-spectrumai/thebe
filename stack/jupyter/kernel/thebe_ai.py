"""%ai / %%ai: a request in plain language in, Python code out. Loaded into every python3 kernel.

    %%ai [PROVIDER] [--model MODEL] [--replace | --print] [--var NAME ...]
    <the request, any number of lines>

    %ai [PROVIDER] <one-line request>
    %ai status                     providers, the token budget and answer times

    from thebe_ai import ask       code = ask("...")  (insert=True also adds a cell below)

The kernel never sees an API key. It sends the request to the AI gateway (the ai container,
AI_GATEWAY_URL, with the token from AI_TOKEN_FILE), which asks OpenAI or Anthropic, counts the
tokens against the budget and streams the answer back. The answer is shown in the cell's output
as it arrives; the code in it then goes into a NEW cell below (IPython's set_next_input
payload), or with --replace into this cell (the request kept as # ai: comments on top), or with
--print nowhere. Generated code is NEVER run here: read it, then run it with Shift+Enter.

--var NAME adds a short description of a notebook variable (type, shape, column names and types,
or dictionary keys), never its values.

The AI button in the cell toolbar (the thebe-ai-cell JupyterLab extension) puts
"%%ai --replace" in front of the cell's text and runs the cell: the same code path.

Standard library only (and IPython for the magic), so it works in every kernel of the image.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shlex
import socket
import sys
import urllib.error
import urllib.request
from typing import Any, Callable

GATEWAY_URL = os.environ.get("AI_GATEWAY_URL", "")
TOKEN_FILE = os.environ.get("AI_TOKEN_FILE") or "/run/secrets/ai_token"
STATUS_TIMEOUT = 10.0
# Seconds the kernel waits for the next piece of an answer. The gateway itself gives up after
# ai.timeout per attempt (at most 600 s, 3 attempts), so this is only a guard.
READ_TIMEOUT = 2000.0
MAX_VARIABLES = 20

_FENCE = re.compile(r"```[ \t]*(?:python|py|python3)?[ \t]*\n(.*?)(?:\n```|\Z)", re.DOTALL | re.IGNORECASE)

OFF = ("AI is off in this Thebe: config.yaml has no ai: section, or the AI gateway is not running. "
       "Add the section with an API key and run update (see the AI manual).")


class AiError(Exception):
    """A request could not be answered; the message is meant for the notebook user."""


# --- helpers -------------------------------------------------------------------------------

def extract_code(text: str) -> str:
    """The first fenced code block, else the whole answer; one trailing newline."""
    match = _FENCE.search(text)
    return (match.group(1) if match else text).strip("\n").rstrip() + "\n"


def describe_variable(name: str, value: Any) -> str:
    """A description without data values: type, and shape/columns/keys where cheap."""
    kind = f"{type(value).__module__}.{type(value).__qualname__}".removeprefix("builtins.")
    details = []
    shape = getattr(value, "shape", None)
    if isinstance(shape, tuple):
        details.append(f"shape={shape}")
    columns = getattr(value, "columns", None)
    dtypes = getattr(value, "dtypes", None)
    if columns is not None and dtypes is not None:
        try:
            details.append("columns=" + ", ".join(f"{c} ({t})" for c, t in list(dtypes.items())[:50]))
        except Exception:
            pass
    elif isinstance(value, dict):
        details.append("keys=" + ", ".join(map(repr, list(value)[:30])))
    elif isinstance(value, (list, tuple, set)):
        details.append(f"len={len(value)}")
    return f"- {name}: {kind}" + (f" ({'; '.join(details)})" if details else "")


def budget_text(usage: dict) -> str:
    """'987,000 of 1,000,000 tokens left this month (until 2026-10-01)' or the used count."""
    period = "this month" if usage.get("period") == "month" else "in total"
    used = usage.get("used_tokens") or 0
    if not usage.get("max_tokens"):
        return f"{used:,} tokens used {period} (no limit)"
    until = f" (until {usage['period_end'][:10]})" if usage.get("period_end") else ""
    return f"{usage.get('remaining_tokens') or 0:,} of {usage['max_tokens']:,} tokens left {period}{until}"


def times_text(seconds: dict) -> str:
    count = seconds.get("count") or 0
    if not count:
        return "no answers yet"
    spread = f" ± {seconds['stdev']:.1f}" if seconds.get("stdev") is not None else ""
    return f"{seconds['mean']:.1f}{spread} s per answer (mean ± standard deviation of {count})"


# --- the gateway ---------------------------------------------------------------------------

class GatewayClient:
    """HTTP to the AI gateway. The token is read on every call, so a new one needs no restart."""

    def __init__(self, url: str = GATEWAY_URL, token_file: str = TOKEN_FILE) -> None:
        self.url, self.token_file = url.rstrip("/"), token_file
        # No proxy: HTTP(S)_PROXY in a notebook's environment must never see the token.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _request(self, method: str, path: str, body: dict | None, timeout: float):
        if not self.url:
            raise AiError(OFF)
        try:
            with open(self.token_file, encoding="ascii") as handle:
                token = handle.read().strip()
        except (OSError, UnicodeDecodeError):
            raise AiError(f"Cannot read the AI gateway token ({self.token_file}). Run update.") from None
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, application/x-ndjson"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            return self._opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            with exc:
                try:
                    message = json.loads(exc.read(64 * 1024)).get("error") or ""
                except (ValueError, AttributeError, OSError):
                    message = ""
            if exc.code == 401:
                raise AiError("The AI gateway refused this JupyterLab's token. Run restart.") from None
            raise AiError(message or f"The AI gateway answered HTTP {exc.code}.") from None
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.gaierror, ConnectionRefusedError)):
                raise AiError(OFF) from None
            raise AiError(f"Cannot reach the AI gateway: {reason}.") from None

    def status(self) -> dict:
        with self._request("GET", "/status", None, STATUS_TIMEOUT) as response:
            return json.loads(response.read())

    def generate(self, prompt: str, provider: str | None = None, model: str | None = None,
                 on_text: Callable[[str], None] | None = None) -> tuple[str, dict]:
        """The whole answer and the gateway's closing line (tokens, seconds, budget)."""
        body = {"prompt": prompt, "provider": provider, "model": model}
        parts: list[str] = []
        with self._request("POST", "/generate", body, READ_TIMEOUT) as response:
            try:
                for raw in response:
                    try:
                        item = json.loads(raw)
                    except ValueError:
                        continue
                    if "text" in item:
                        parts.append(item["text"])
                        if on_text:
                            on_text(item["text"])
                    elif "error" in item:
                        raise AiError(item["error"])
                    elif item.get("done"):
                        return "".join(parts), item
            except (OSError, http.client.HTTPException) as exc:
                raise AiError(f"The connection to the AI gateway broke: {exc}.") from None
        raise AiError("The AI gateway ended the answer without its closing line; nothing was inserted.")


# --- the magic -----------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="%%ai", add_help=False, exit_on_error=False)
    parser.add_argument("provider", nargs="?")
    parser.add_argument("-m", "--model")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--replace", action="store_true")
    where.add_argument("--print", dest="print_only", action="store_true")
    parser.add_argument("-v", "--var", action="append", default=[])
    return parser


USAGE = "Usage: %%ai [provider] [--model M] [--replace | --print] [--var NAME]"


def build_request(prompt: str, variables: list[str], namespace: dict) -> str:
    if len(variables) > MAX_VARIABLES:
        raise AiError(f"--var: at most {MAX_VARIABLES} variables.")
    context = []
    for name in variables:
        if name not in namespace:
            raise AiError(f"--var {name}: no such variable in the notebook. Run the cell that defines it first.")
        context.append(describe_variable(name, namespace[name]))
    request = prompt.strip()
    if context:
        request += "\n\nNotebook variables you can use:\n" + "\n".join(context)
    return request


def _run(shell, client: GatewayClient, line: str, cell: str | None) -> None:
    words = line.split()
    if cell is None and words[:1] == ["status"]:
        show_status(client)
        return
    if cell is None:
        # %ai [provider] request...: the first word is a provider when it looks like one of the
        # names; the gateway says which exist.
        if len(words) > 1 and re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", words[0]) and words[0] in _known(client):
            line, prompt = words[0], " ".join(words[1:])
        else:
            line, prompt = "", " ".join(words)
    else:
        prompt = cell
    try:
        args = _parser().parse_args(shlex.split(line))
    except (argparse.ArgumentError, ValueError) as exc:
        print(f"%%ai: {exc}. {USAGE}", file=sys.stderr)
        return
    if not prompt.strip():
        print("%%ai: write the request below the %%ai line (or in the cell, for the AI button).", file=sys.stderr)
        return
    try:
        request = build_request(prompt, args.var, shell.user_ns)
        answer, done = client.generate(request, args.provider, args.model,
                                       on_text=lambda text: print(text, end="", flush=True))
    except AiError as exc:
        print(f"\n%%ai: {exc}", file=sys.stderr)
        return
    except KeyboardInterrupt:
        print("\n%%ai: interrupted; nothing was inserted. (The gateway still receives the answer, "
              "and its tokens count.)", file=sys.stderr)
        return
    print(flush=True)
    code = extract_code(answer)
    note = (f"[{done.get('provider')} {done.get('model')}: {done.get('input_tokens', 0):,} + "
            f"{done.get('output_tokens', 0):,} tokens, {done.get('seconds', 0):.1f} s; "
            f"{budget_text(done.get('budget') or {})}]")
    if done.get("truncated"):
        print("%%ai: the answer was cut off at the output token limit (ai.max_output_tokens, or what is left "
              "of the budget); the code may be incomplete.", file=sys.stderr)
    if args.print_only:
        from IPython.display import Markdown, display
        display(Markdown(f"```python\n{code}```"))
        print(note)
    elif args.replace:
        header = "".join(f"# ai: {text}\n" for text in prompt.strip().splitlines())
        shell.set_next_input(header + code, replace=True)
        print(f"%%ai: this cell now holds the generated code. It has not run: read it, then press Shift+Enter. {note}")
    else:
        shell.set_next_input(code, replace=False)
        print(f"%%ai: code inserted in a new cell below. It has not run: read it, then press Shift+Enter. {note}")


_names_cache: list[str] | None = None


def _known(client: GatewayClient) -> list[str]:
    """Provider names, asked once per kernel (for %ai <provider> <request>)."""
    global _names_cache
    if _names_cache is None:
        try:
            _names_cache = [p["name"] for p in client.status().get("providers", [])]
        except (AiError, ValueError, KeyError, TypeError):
            return []
    return _names_cache


def show_status(client: GatewayClient) -> None:
    try:
        status = client.status()
    except (AiError, ValueError) as exc:
        print(f"%ai: {exc}")
        return
    for provider in status.get("providers", []):
        default = " (default)" if provider.get("name") == status.get("default") else ""
        print(f"{provider.get('name')}{default}: {provider.get('api')} {provider.get('model')}")
    usage = status.get("usage") or {}
    print(f"Budget: {budget_text(usage)}")
    print(f"Answers: {usage.get('requests', 0)} ({usage.get('errors', 0)} failed); "
          f"{times_text(usage.get('seconds') or {})}")
    print(f"Timeout {status.get('timeout', 0):.0f} s, answers of at most {status.get('max_output_tokens', 0):,} tokens. "
          "Keys and the budget are set in config.yaml (ai: section).")


_magics = None


def load_ipython_extension(ipython) -> None:
    global _magics
    from IPython.core.magic import Magics, line_cell_magic, magics_class

    @magics_class
    class AiMagics(Magics):
        client = GatewayClient()

        @line_cell_magic
        def ai(self, line: str, cell: str | None = None):
            _run(self.shell, self.client, line, cell)

    _magics = AiMagics(ipython)
    ipython.register_magics(_magics)


def ask(prompt: str, provider: str | None = None, *, model: str | None = None, insert: bool = False,
        client: GatewayClient | None = None) -> str:
    """The same request as %%ai, as a function: the code as text (not run).

    insert=True also puts the code into a new cell below, like %%ai.
    """
    client = client or GatewayClient()
    answer, _done = client.generate(prompt.strip(), provider, model)
    code = extract_code(answer)
    if insert:
        from IPython import get_ipython
        shell = get_ipython()
        if shell is not None:
            shell.set_next_input(code, replace=False)
    return code
