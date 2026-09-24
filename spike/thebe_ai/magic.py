"""%ai / %%ai: a natural-language prompt in, Python code out (SPIKE ONLY).

    %%ai [PROVIDER] [--model MODEL] [--replace | --print] [--var NAME ...]
    <prompt, any number of lines>

    %ai [PROVIDER] <one-line prompt>
    %ai status                     which providers are configured (keys masked)

Default: the answer streams into the cell's output and the code goes into a NEW cell below
(IPython's set_next_input payload). --replace puts it into this cell instead (the prompt is kept
as a comment on top); --print only shows it. Generated code is NEVER executed here: the user
reads it and runs the new cell with Shift+Enter.

--var NAME adds a short description of a notebook variable (type, shape, columns; never its
values) so the model can write code against it.
"""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from typing import Any

from IPython.core.magic import Magics, line_cell_magic, magics_class
from IPython.display import Markdown, display

from .config import AiConfig, ConfigError, load_config, mask
from .providers import Provider, ProviderError, make_provider

SYSTEM_PROMPT = (
    "You write Python code for one Jupyter notebook cell. Reply with exactly one ```python code block "
    "and nothing else. Explain only in brief # comments inside the code. Use only the variables you are "
    "told about, and the standard library, numpy, pandas and matplotlib unless the request names others. "
    "Never install packages, never read secrets or environment variables, never delete files."
)
_FENCE = re.compile(r"```[ \t]*(?:python|py|python3)?[ \t]*\n(.*?)(?:\n```|\Z)", re.DOTALL | re.IGNORECASE)


def extract_code(text: str) -> str:
    """The first fenced code block, else the whole answer; without trailing whitespace."""
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="%%ai", add_help=False, exit_on_error=False)
    parser.add_argument("provider", nargs="?")
    parser.add_argument("-m", "--model")
    where = parser.add_mutually_exclusive_group()
    where.add_argument("--replace", action="store_true")
    where.add_argument("--print", dest="print_only", action="store_true")
    parser.add_argument("-v", "--var", action="append", default=[])
    return parser


@magics_class
class AiMagics(Magics):
    def __init__(self, shell, ai_config: AiConfig | None = None, providers: dict[str, Provider] | None = None):
        super().__init__(shell)
        self._config_error = ""
        try:
            self.ai_config = ai_config or load_config()
        except ConfigError as exc:
            self.ai_config, self._config_error = AiConfig({}, ""), str(exc)
        self._providers: dict[str, Provider] = dict(providers or {})

    # -- the magic ----------------------------------------------------------------------

    @line_cell_magic
    def ai(self, line: str, cell: str | None = None):
        words = line.split()
        if cell is None and words[:1] == ["status"]:
            return self._status()
        if cell is None:
            # %ai [provider] prompt...: the first word is a provider only if one has that name.
            if words and words[0] in self.ai_config.providers:
                line, prompt = words[0], " ".join(words[1:])
            else:
                line, prompt = "", " ".join(words)
        else:
            prompt = cell
        try:
            args = _parser().parse_args(shlex.split(line))
        except (argparse.ArgumentError, ValueError) as exc:
            print(f"%%ai: {exc}. Usage: %%ai [provider] [--model M] [--replace|--print] [--var NAME]",
                  file=sys.stderr)
            return None
        if not prompt.strip():
            print("%%ai: write the request below the %%ai line.", file=sys.stderr)
            return None
        try:
            code = self.generate(prompt, args.provider, model=args.model, variables=args.var)
        except (ConfigError, ProviderError) as exc:
            print(f"%%ai: {exc}", file=sys.stderr)
            return None
        except KeyboardInterrupt:
            print("\n%%ai: interrupted; nothing was inserted.", file=sys.stderr)
            return None
        self._deliver(code, prompt, replace=args.replace, print_only=args.print_only)
        return None

    # -- steps ----------------------------------------------------------------------------

    def generate(self, prompt: str, provider: str | None = None, *, model: str | None = None,
                 variables: list[str] = (), stream: bool = True) -> str:
        if self._config_error:
            raise ConfigError(self._config_error)
        if not self.ai_config.providers:
            raise ConfigError("No AI provider has an API key. Add one to the Thebe configuration and redeploy.")
        chosen = self.ai_config.provider(provider)
        if model:
            from dataclasses import replace
            chosen = replace(chosen, model=model)
        key = f"{chosen.name}:{chosen.model}"
        if key not in self._providers:
            self._providers[key] = make_provider(chosen, self.ai_config)
        context = []
        for name in variables:
            if name not in self.shell.user_ns:
                raise ConfigError(f"--var {name}: no such variable in the notebook.")
            context.append(describe_variable(name, self.shell.user_ns[name]))
        request = prompt.strip()
        if context:
            request += "\n\nNotebook variables you can use:\n" + "\n".join(context)
        on_text = (lambda text: print(text, end="", flush=True)) if stream else None
        answer = self._providers[key].generate(SYSTEM_PROMPT, request, on_text)
        if stream:
            print(flush=True)
        return extract_code(answer)

    def _deliver(self, code: str, prompt: str, *, replace: bool, print_only: bool) -> None:
        if print_only:
            display(Markdown(f"```python\n{code}```"))
            return
        if replace:
            header = "".join(f"# ai: {line}\n" for line in prompt.strip().splitlines())
            self.shell.set_next_input(header + code, replace=True)
            print("%%ai: this cell now holds the generated code. Read it, then run it (Shift+Enter).")
        else:
            self.shell.set_next_input(code, replace=False)
            print("%%ai: code inserted in a new cell below. Read it, then run it (Shift+Enter).")

    def _status(self) -> None:
        if self._config_error:
            print(f"AI configuration error: {self._config_error}")
            return
        if not self.ai_config.providers:
            print("No AI provider has an API key.")
            return
        for name, provider in sorted(self.ai_config.providers.items()):
            default = " (default)" if name == self.ai_config.default else ""
            print(f"{name}{default}: {provider.api} {provider.model}, key {mask(provider.api_key)}")
        retries = self.ai_config.max_retries
        print(f"timeout {self.ai_config.timeout:.0f} s, {retries} {'retry' if retries == 1 else 'retries'}, "
              f"at most {self.ai_config.max_output_tokens} output tokens")
