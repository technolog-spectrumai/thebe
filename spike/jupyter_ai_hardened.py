"""SPIKE: jupyter-ai's own %ai/%%ai magics (jupyter-ai-magic-commands 0.0.4) loaded under Thebe's rules.

Load this instead of `jupyter_ai_magic_commands` (kernel config:
c.InteractiveShellApp.extensions = ['jupyter_ai_hardened']). Four changes, and the reason for each:

1. Keys come only from the static config (THEBE_AI_CONFIG JSON, the same file as spike/thebe_ai)
   and become aliases: `%%ai claude`, `%%ai openai`, and a default model for a bare `%%ai`.
2. The magic never reads a `.env` from the notebook's folder. Stock 0.0.4 loads os.getcwd()/.env
   with override=True before every call, so any file in the workspace could replace the key or
   point the SDK at another server (ANTHROPIC_API_BASE, HTTPS_PROXY, ...) and receive the key.
   This patches a module global (`dotenv_path`), i.e. it depends on jupyter-ai internals.
3. litellm uses its bundled model price list instead of downloading one at import.
4. IPython's normal error handling is restored (0.0.4 installs a catch-all handler that also
   copies every traceback into a notebook variable `Err`).
"""

import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")      # before litellm is imported

ENV_NAMES = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def _static_config() -> dict:
    path = Path(os.environ.get("THEBE_AI_CONFIG") or "/run/secrets/thebe_ai")
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"providers": {}}


def load_ipython_extension(ipython) -> None:
    config = _static_config()
    aliases = {}
    for name, entry in config.get("providers", {}).items():
        if entry.get("api") in ENV_NAMES and entry.get("api_key"):
            os.environ[ENV_NAMES[entry["api"]]] = entry["api_key"]
            aliases[name] = {"target": f"{entry['api']}/{entry['model']}", "api_base": entry.get("base_url"),
                             "api_key_name": None}
    ipython.config.AiMagics.initial_aliases = aliases
    default = config.get("default")
    if default in aliases:
        ipython.config.AiMagics.initial_language_model = default

    import jupyter_ai_magic_commands.magics as magics
    empty = Path(tempfile.gettempdir()) / "thebe-ai-no.env"            # exists and is empty
    empty.write_text("")
    magics.dotenv_path = str(empty)

    from jupyter_ai_magic_commands import load_ipython_extension as load
    load(ipython)
    ipython.custom_exceptions = ()                                      # IPython's default handling
