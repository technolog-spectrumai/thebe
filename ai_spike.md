# AI spike: natural-language prompts → code in Thebe's notebooks

Branch `ai_spike`, 2026-09-24. **Exploratory only:** nothing here changes the deployed stack.
Production code, the installer, the images and `config.yaml` are untouched. The prototype lives in
`spike/`, and the user-facing description is [AI_MANUAL.md](AI_MANUAL.md).

Evidence labels used below:

- **[Verified]:** checked in this spike against the actual packages or code (versions given).
- **[Demonstrated]:** our prototype, run in a real IPython 9.17 / ipykernel 7.3 kernel (Thebe's
  versions) against local stand-ins for the OpenAI and Anthropic APIs.
- **[Reported]:** from project documentation or READMEs, not reproduced here.

## 1. Problem statement

In a Thebe notebook the user wants to type a request in plain language ("load sales.csv, sum
revenue per region, bar chart") and get Python code back into the notebook workflow. They then
review it and run it themselves.

Constraints for Thebe:

- **JupyterLab:** a pinned, self-hosted JupyterLab 4.6 in Docker. Kernels run as the user's uid and
  may use the custom packages venv.
- **API keys:** OpenAI and/or Anthropic keys supplied by the user in Thebe's YAML configuration
  **before startup**. No runtime key editing and no secrets UI.
- **No surprises:** generated code is never executed automatically.
- **Maintenance:** Thebe is maintained by one person, so the solution must survive JupyterLab
  upgrades with little work, and the smallest robust option wins.

## 2. Options researched

| | Option | In one sentence |
| --- | --- | --- |
| A | Existing third-party extension | Install something like jupyter-ai (chat, `%%ai` magic) or notebook-intelligence (Copilot-style assistant). |
| B | Tiny custom JupyterLab extension | A ✨ button in the cell toolbar opens a prompt dialog and puts the code into a cell. |
| C | IPython line/cell magic | `%%ai [provider]` + prompt in a cell → code in a new cell below. |
| D | Plain Python helper | `code = ask("…")`, optionally inserting a cell. |

Also looked at, and rejected below: custom syntax (`$$$-openai`), extending the shell `!` syntax, and
IPython input transformers.

## 3. Findings

### 3.1 How code gets back into the notebook (all options)

The IPython kernel can ask the frontend to put text into a cell. `get_ipython().set_next_input(text,
replace=False)` adds a `set_next_input` payload to the `execute_reply`. **[Verified]** JupyterLab
4.6.4's notebook code (`jlab_core` bundle) does the following with it:

- `replace: false`: it inserts a **new code cell directly below** the executed cell with that
  source, metadata `trusted: false`, **not executed**.
- `replace: true`: it replaces the source of the executed cell.
- In a *console* it replaces the prompt's input instead.

**[Demonstrated]** `spike/kernel_demo.py` shows ipykernel 7.3 sending exactly this payload from our
magic. Execution never happens: after `%%ai`, `import math` from the generated code is not in the
kernel's namespace. So "print, insert below, replace this cell, never run" needs no frontend code
at all.

### 3.2 JupyterLab extension points (option B)

**[Verified]** In the JupyterLab 4.6.4 settings schemas:

- **Cell toolbar** (`@jupyterlab/cell-toolbar-extension:plugin`): its `toolbar` setting takes items
  `{name, command, args, icon, label, caption, disabled}`. The defaults are duplicate, move up/down,
  insert above/below and delete.
- **Notebook toolbar** (`@jupyterlab/notebook-extension:panel`, `toolbar`): same item format.
- **Context menus** (`@jupyterlab/application-extension:context-menu` and each plugin's
  `jupyter.lab.menus.context`), plus the command palette and keyboard shortcuts: all by command id.

So **placing** a button is pure configuration, but a button can only invoke an existing
**command**. No core command both asks for text and inserts a cell with given source, and none calls
an AI. A prompt button therefore needs a small TypeScript extension that registers a command:

1. `InputDialog.getText()` from `@jupyterlab/apputils` for the prompt;
2. get the code: either call a server endpoint, or (simpler) insert a cell `%%ai\n<prompt>` and run
   it, letting the kernel magic (option C) do the work;
3. `NotebookActions`/the shared model to insert or replace the cell.

That is roughly 100–200 lines of TypeScript, built with the JupyterLab extension template (Node and
jlpm at build time) and shipped as a prebuilt ("federated") extension in a wheel, so installing it
needs no Node. [Reported, see §4]

### 3.3 IPython magics (option C) vs custom syntax

- **Magics are the supported mechanism.** `@magics_class` / `@line_cell_magic`, registered by
  `load_ipython_extension`, need no parser changes and work in notebooks, consoles and the IPython
  terminal. `%load_ext`, kernel config (`c.InteractiveShellApp.extensions`) or an IPython startup
  file can load them. **[Demonstrated]**
- **`$$$-openai` custom syntax** would need an input transformer (`input_transformers_cleanup`).
  It is invisible to linters and formatters, confusing next to real Python, and breaks
  if IPython's transformer API changes. `%%ai` gives the same brevity with a documented API.
- **Extending `!`** is wrong: `!cmd` is system-shell escape, handled by IPython's transformer; hijacking
  it changes shell semantics users rely on (`!pip list`, `!ls`) and needs the same parser hack.
- **Jupyter AI chose the same form:** `%%ai provider:model`, `-f code`, `set_next_input`
  [Verified in the jupyter-ai-magic-commands 0.0.4 source].

### 3.4 Provider integration (options B–D)

**[Verified]** openai 3.19.2 and anthropic 1.8.0 (current on PyPI):

| | OpenAI | Anthropic |
| --- | --- | --- |
| Call | `client.responses.stream(model, instructions, input, max_output_tokens)` (Responses API; `chat.completions` still exists) | `client.messages.stream(model, max_tokens, system, messages)` → `text_stream` |
| Client options | `OpenAI(api_key, base_url, timeout, max_retries)` | `Anthropic(api_key, base_url, timeout, max_retries)` |
| Retries | built in, default 2, with backoff; honours `retry-after` | same |
| Default timeout | 600 s read, far too long interactively; the spike uses 60 s | same |
| Errors | `AuthenticationError`, `RateLimitError`, `APITimeoutError`, `APIConnectionError`, `NotFoundError`, `BadRequestError`, `APIStatusError` | same names, plus `OverloadedError` |
| Weight on top of JupyterLab | +10 packages (httpx, pydantic, …) | +11 |

**[Demonstrated]** `spike/thebe_ai/providers.py` puts both behind one method,
`generate(system, prompt, on_text)`:

- **Streaming:** both SDKs stream through the same interface, tested against stand-in servers that
  speak each API's streaming format.
- **Error messages:** errors become one-line messages without the key. A 429 is retried
  `max_retries` times, then "rate limited…"; a 401 becomes "the API key was rejected"; an
  unreachable host becomes "cannot reach the API".
- **Interrupt:** *Kernel → Interrupt* stops a request in flight, and nothing is inserted
  (`spike/interrupt_demo.py`).
- **Models:** plain config strings; examples are taken from the SDKs' own model lists
  (`claude-sonnet-5`, `claude-opus-5`, `gpt-5.4-mini`, `gpt-5.5`). Nothing is hard-wired except
  spike defaults.

**The alternative is litellm** (as jupyter-ai 3 uses): one API for 100+ providers, but pulling
jupyter-ai's magics brings **+96 packages**. For exactly two providers the two official SDKs are
smaller, better typed, and maintained by the providers.

### 3.5 Dependency weight [Verified: pip dry-run with jupyterlab==4.6.4, which alone is 62 packages]

| Install | Packages on top of JupyterLab |
| --- | --- |
| `openai` / `anthropic` | +10 / +11 |
| `jupyter-ai-magic-commands` 0.0.4 (v3 magics) | +36 |
| `jupyter-ai[magics]` 3.2.0 | +96 |
| `jupyter-ai[jupyternaut]` 3.2.0 (chat assistant) | +118 |
| `jupyter-ai-magics` 2.31.7 (old v2 magics, langchain) | +45 |
| `notebook-intelligence` 6.0.0 | +53 |

## 4. Existing libraries and extensions

| Project | Version (PyPI, 2026-09-24) | What it offers | Fit for Thebe |
| --- | --- | --- | --- |
| **jupyter-ai** (Project Jupyter) | 3.2.0 [Verified] | v3 is a meta-package of modules: `jupyterlab-chat`, a persona manager, a router, tools, an MCP server, an ACP client, and optionally the `jupyternaut` assistant and `magics`, both via `jupyter-ai-litellm` (litellm ≥ 1.94) [Verified from its metadata] | Most complete; heavy (+96 to +118 packages) and a young 0.x module stack. |
| **jupyter-ai-magic-commands** | 0.0.4 [Verified] | v3's `%ai` / `%%ai` over litellm: `-f code` inserts via `set_next_input(replace=False)`; aliases; keeps 2 exchanges of history. Keys come from `os.getcwd()/.env` (reloaded on every call) or env vars. It installs a catch-all custom exception handler. There is no streaming and no timeout setting. [Verified in source] | The same UX idea as option C, but early, tied to litellm, and it assumes a workspace `.env`. |
| **jupyter-ai-magics** (v2) | 2.31.7 [Verified] | The older `%%ai provider:model` over langchain 0.3 (+45 packages) | Superseded by v3; don't start new work on it. |
| **notebook-intelligence** | 6.0.0 [Verified] | A Copilot-style assistant for JupyterLab (chat, agents). Its dependencies include `anthropic`, `openai`, `litellm`, `claude-agent-sdk`, `mcp`, `ollama` and `cryptography` (+53 packages) [Verified from its metadata] | Feature-rich agent; much more than prompt → code, with a broad surface. |

Further candidates (mito-ai, jupyterlite-ai, pretzelai and small GPT-magic packages), plus the
maintenance status and key configuration of each project above, are still being checked against
current sources; see the update below.


## 5. The four options in detail

### A. Existing third-party extension

- **Approach:** add e.g. `jupyter-ai[magics]` or `jupyter-ai[jupyternaut]` to
  `stack/jupyter/requirements.txt` and the lock, and set keys as environment variables of the
  jupyterlab container.
- **Pros:**
  - Richest UX: a chat sidebar, and in v3 personas, agents and tools.
  - Many providers, and no code to write.
- **Cons:**
  - The heaviest dependency tree, from +96 to +118 packages via litellm, with a fast-moving 0.x
    stack. The magics package is 0.0.4, and the v2 to v3 rewrite changed everything.
  - Key handling assumes a **`.env` file in the kernel's working directory**, i.e. in the notebook
    workspace, and a settings UI that writes it. That is the runtime key editing we want to avoid,
    and the file is readable by every notebook.
  - The v3 magics install a catch-all exception handler (`set_custom_exc((BaseException,), …)`) in
    every kernel that loads them. **[Verified in source]**
  - Chat and agent features that can edit files and run code widen the attack surface considerably.
  - Harder to pin reproducibly, and upgrades are tied to jupyter-ai's JupyterLab compatibility.
- **Effort:** half a day to install and pin, plus ongoing work to track upgrades.

### B. Tiny custom JupyterLab button/modal extension

- **Approach:** a TypeScript extension registers a `thebe-ai:prompt` command (input dialog → run
  `%%ai` in the kernel, or call a server handler → insert the code). The cell-toolbar setting adds
  the button, and a context-menu entry and shortcut come for free through settings.
- **Pros:**
  - Best discoverability: a ✨ on every cell.
  - Can reuse option C as its engine, so no key ever reaches the browser.
- **Cons:**
  - A Node/TypeScript build chain enters a Python-only repository, and the extension must be
    rebuilt against each JupyterLab minor. Frontend APIs (the notebook model, shared model,
    toolbars) changed between 3.x and 4.x and will change again.
  - Needs frontend tests (Galata or Playwright).
- **Effort:** 2–4 days for a solid first version with tests and a build pipeline, then roughly one
  day per JupyterLab minor to check.

### C. IPython line/cell magic (prototyped)

- **Approach:** `%%ai [provider] [--model M] [--replace|--print] [--var NAME]` and `%ai status`, in
  a small package installed into the JupyterLab image and auto-loaded through kernel config.
  Insertion uses `set_next_input` (§3.1).
- **Pros:**
  - The smallest amount of code (about 400 lines in the prototype, comments included) and no frontend build.
  - It rides on IPython's stable, decades-old magic API, and works in notebooks, consoles and
    `%run` scripts.
  - Keys stay server-side, and code is never executed by default.
- **Cons:**
  - Discoverability: the user has to know `%%ai`, so the manual matters.
  - The prompt lives in a cell, and the AI sees only what it is told (`--var`).
  - `set_next_input` inserts below the *executed* cell, not at an arbitrary position.
- **Effort:** 1–2 days to productionize the spike (config wiring, image, tests), then very little
  maintenance.

### D. Plain Python helper

- **Approach:** `from thebe_ai import ask; code = ask("…", insert=True)`, sharing C's code path
  (prototyped).
- **Pros:**
  - Scriptable: loops, building prompts from data, and it can be tested like any function.
- **Cons:**
  - Clumsier for interactive use, since multi-line prompts need string quoting.
- **Effort:** it comes with C (a few lines).

### Comparison

| | A: jupyter-ai | B: custom button | C: magic | D: helper |
| --- | --- | --- | --- | --- |
| Implementation effort | Low (install) | High (TS + build + tests) | Low (spike exists) | Trivial with C |
| Fragility across Jupyter upgrades | Medium–high (0.x, big deps, JupyterLab-coupled) | High (frontend APIs) | **Low** (IPython magic API, kernel payload) | **Low** |
| UX | Best (chat, many features) | Good (button + dialog) | Good for keyboard users | Fine for scripting |
| Discoverability | High | High | Low–medium | Low |
| Security | Widest surface (agents, tools, `.env` in the workspace, settings UI) | Medium (adds a frontend and maybe a server endpoint) | **Narrow** (kernel-only; no auto-exec) | Narrow |
| Maintenance | Track jupyter-ai + litellm | Own TS extension | ~400 lines of Python | Included |
| Static key config | Workable via env vars; fights the `.env`/UI design | Via C | **Native** | Native |

## 6. Key management (static, startup-only)

The proposal is not implemented, and it follows Thebe's existing secrets pattern:

1. `config.yaml` gets an `ai:` section (providers, models, keys, timeout). The file is already mode
   600 and gitignored.
2. On install/update the installer writes `APP_DIR/secrets/thebe_ai.json` (mode 600), exactly like
   `jupyter_password` and `deps_token`.
3. `compose.yaml` mounts it as a Compose **secret** into the **jupyterlab** service only: not
   `stats`, not `deps`, and not the browser. The kernel reads `/run/secrets/thebe_ai` once, when
   the extension loads (the spike reads `THEBE_AI_CONFIG`/env vars the same way).
4. To change keys: edit `config.yaml` → `update` → restart the kernel. There is no UI or endpoint
   that sets keys.
5. `%ai status` shows only the last four characters, error messages never contain the key, and the
   config object hides it from `repr` (tested). Keys must never go into logs or cell outputs.

**Limitation, stated plainly:** kernels run user code as the user's uid, so a notebook can always
read a key available to the kernel (the secret file or process memory). This is inherent to any
kernel-side design, jupyter-ai included, and the only real mitigation is key hygiene. Use dedicated
keys with **spend limits** and project scoping in the provider consoles, and rotate them via
`config.yaml`. A server-side proxy (the kernel talks to a Thebe endpoint that holds the key) would
hide the key from notebooks, but not the spending power. It is listed under later evolution.

## 7. Security considerations

- **No auto-execution:** the code lands in an untrusted cell and the user runs it. There is
  deliberately no `--run` in the MVP.
- **Data leaving the machine:** the prompt and any `--var` descriptions go to OpenAI or Anthropic.
  `--var` sends type, shape and column names only, never values (tested). The manual warns against
  confidential prompts.
- **Prompt injection:** code generated from untrusted text, such as a pasted traceback or a CSV
  header, can be malicious. This is mitigated by review before running, and by the system prompt
  forbidding installs, secrets access and deletions (advisory, not enforcement).
- **Keys:** see §6. The jupyterlab container already has outbound internet, so nothing new is opened.
- **Supply chain:** two first-party SDKs (about 11 packages each) pinned in the image lock, versus
  about 100 packages for litellm-based stacks.
- **No new network listener:** option C adds no endpoint. B and A add frontend code, and A adds
  server handlers (chat, MCP).

## 8. Recommended MVP

**Option C (IPython magic) with D alongside, built on the two official SDKs.** It is essentially the
prototype in `spike/`, productionized:

1. Package `thebe_ai` into the JupyterLab image (not the custom venv), with `openai` and `anthropic`
   pinned in `stack/jupyter/requirements.txt`/lock.
2. Auto-load it through the kernelspec or an IPython startup file, so `%%ai` works without
   `%load_ext`.
3. Add the `ai:` section in `config.yaml` → `secrets/thebe_ai.json` → Compose secret for jupyterlab
   (§6), plus builder and `run.sh check` validation of that section. Keys are never shown back.
4. Defaults: timeout 60 s, 2 retries, 2048 output tokens, and insert below. Keep `--replace`,
   `--print` and `--var`, with no execution.
5. Tests: the spike's unit tests, plus the kernel payload test.

Estimated at 1–2 days. The cell-button (B) is **not** straightforward enough to justify for the
first version: placing a button is configuration, but the command behind it needs a TypeScript
extension with a build chain and per-release maintenance.

## 9. What NOT to implement yet

- Runtime key entry or editing, a settings or secrets UI, and key storage in the workspace (`.env`).
- Auto-execution of generated code, or a `--run` flag.
- Custom syntax (`$$$…`), `!` extensions or input transformers.
- A chat sidebar, agents, tool use, MCP, or notebook-wide context (reading other cells, files or
  outputs).
- Inline completion (ghost text while typing).
- The TypeScript cell button (B), and jupyter-ai in the image.
- More providers (local models, Bedrock, …) beyond the two requested.
- Cost tracking, per-user quotas or usage dashboards.

## 10. Possible later evolution

1. **Cell button on top of the magic:** a small prebuilt extension whose command opens a dialog,
   inserts `%%ai\n<prompt>` below and runs it. The kernel magic stays the single engine, and the
   browser never sees keys.
2. **Context options:** `--cell` (include the current or previous cell's source), and
   `--error` (include the last traceback) for "fix this".
3. **Conversation mode:** keep the last N exchanges per kernel (jupyter-ai keeps 2), off by
   default.
4. **Server-side key proxy:** a jupyter-server extension that holds the key, so kernels send
   prompts without seeing it. It needs auth and rate limiting.
5. **Inline completion provider** (JupyterLab 4.1+ `IInlineCompletionProvider`), if wanted.
6. **Revisit jupyter-ai** once its v3 packages reach 1.x and static key configuration is first-class.
7. **Usage line:** tokens and a cost estimate after each request, from the SDK usage fields.

## 11. Spike artefacts

| Path | What |
| --- | --- |
| `spike/thebe_ai/config.py` | Static config loader: JSON file or env vars; keys masked in reprs. |
| `spike/thebe_ai/providers.py` | OpenAI/Anthropic behind one `generate()`; streaming, timeouts, retries, error mapping. |
| `spike/thebe_ai/magic.py` | `%ai` / `%%ai`: options, `--var` descriptions, code extraction, `set_next_input`. |
| `spike/thebe_ai/__init__.py` | `load_ipython_extension`, and `ask()` (option D). |
| `spike/tests/test_thebe_ai.py` | 11 tests: both real SDKs against stand-in streaming servers, errors, retries, the magic, the helper. |
| `spike/kernel_demo.py` | A real ipykernel runs `%%ai`; prints the `set_next_input` payloads. |
| `spike/interrupt_demo.py` | Kernel interrupt during a slow request: nothing inserted. |

Run them (outside Thebe, in a separate venv):
`pip install ipykernel jupyter_client openai anthropic`, then
`cd spike && python -m unittest discover -s tests`, `python kernel_demo.py`,
`python interrupt_demo.py`.

**Not done in the spike:**

- no request to the real OpenAI or Anthropic APIs (no keys);
- no browser click-through of the inserted cell in JupyterLab (the behaviour is verified from
  JupyterLab 4.6.4's code, §3.1);
- no TypeScript extension.
