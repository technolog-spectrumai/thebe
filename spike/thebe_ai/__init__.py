"""Thebe AI spike: prompt -> Python code in a notebook. NOT a production feature.

    %load_ext thebe_ai          # registers %ai / %%ai (magic.py)
    from thebe_ai import ask    # the plain-function variant: code = ask("...")

See ai_spike.md (research) and AI_MANUAL.md (what exists, what is only proposed).
"""

from __future__ import annotations

_magics = None


def load_ipython_extension(ipython) -> None:
    global _magics
    from .magic import AiMagics
    _magics = AiMagics(ipython)
    ipython.register_magics(_magics)


def ask(prompt: str, provider: str | None = None, *, model: str | None = None, insert: bool = False) -> str:
    """Option D: the same request as %%ai, as a function returning the code.

    insert=True also puts the code into a new cell below (like %%ai); it is never executed.
    """
    from IPython import get_ipython
    from .magic import AiMagics
    shell = get_ipython()
    magics = _magics if _magics is not None and _magics.shell is shell else AiMagics(shell)
    code = magics.generate(prompt, provider, model=model, stream=False)
    if insert and shell is not None:
        shell.set_next_input(code, replace=False)
    return code
