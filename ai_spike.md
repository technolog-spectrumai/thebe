# AI spike: natural-language prompts → code in Thebe's notebooks

Branch `ai_spike`, 2026-09-24. **Exploratory only:** nothing here changes the deployed stack.
Production code, the installer, the images and `config.yaml` are untouched. The prototype lives in
`spike/`, and the user-facing description is [AI_MANUAL.md](AI_MANUAL.md).

Evidence labels used below:

- **[Verified]:** checked in this spike against the actual packages or code (versions given).
- **[Demonstrated]:** our prototype, run in a real IPython 9.17 / ipykernel 7.3 kernel (Thebe's
  versions) against local stand-ins for the OpenAI and Anthropic APIs; where stated, also in
  JupyterLab 4.6.4 driven by headless Chromium (`spike/browser_demo.py`).
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
| A1 | … jupyter-ai in full | Jupyternaut chat, agents, tools, MCP, plus the magics. |
| A2 | … only jupyter-ai's magics, hardened | `jupyter-ai-magic-commands` loaded by a Thebe wrapper that closes its unsafe defaults (§5.1). |
| B | Tiny custom JupyterLab extension | A ✨ button in the cell toolbar opens a prompt dialog and puts the code into a cell. |
| B′ | Thin cell button on top of C | A ✨ button sends **the whole cell text** as the prompt; the kernel magic does the work (prototyped). |
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
kernel's namespace. `spike/browser_demo.py` then shows it in JupyterLab 4.6.4 in Chromium: after
Shift+Enter on a `%%ai` cell, the saved notebook has the code in a new cell right below with
`execution_count: null` and no output (screenshot `spike/screenshots/cell-magic.png`). So "print,
insert below, replace this cell, never run" needs no frontend code at all.

### 3.2 JupyterLab extension points (options B and B′)

**[Verified]** In the JupyterLab 4.6.4 settings schemas and code:

- **Cell toolbar** (`@jupyterlab/cell-toolbar-extension:plugin`): its `toolbar` setting takes items
  `{name, command, args, icon, label, caption, disabled}`. The defaults are duplicate, move up/down,
  insert above/below and delete. An extension can add its own item with `jupyter.lab.toolbars`
  → `"Cell"` in its settings schema.
- **Notebook toolbar** (`@jupyterlab/notebook-extension:panel`, `toolbar`): same item format.
- **Context menus** (`@jupyterlab/application-extension:context-menu` and each plugin's
  `jupyter.lab.menus.context`), plus the command palette and keyboard shortcuts: all by command id.

So **placing** a button is pure configuration, but a button can only invoke an existing
**command**, and no core command sends a cell anywhere but the kernel. Is there a
settings-only way to make "the whole cell is the prompt" work? **No** [Verified]:

- JupyterLab 4.6 has a macro command, `apputils:run-all-enabled`, but its code checks
  `Array.isArray(commands)` and so cannot pass per-command arguments. A chain like
  "move to cell start → type `%%ai --replace` → run" is therefore impossible.
- No core command moves the cursor to the start of a cell. `notebook:replace-selection` inserts
  text only at the cursor.
- The only way from a toolbar button to the kernel is to run the cell, and a cell without `%%ai`
  runs as Python.

**The thin extension (B′)** is small because it reuses the magic. `spike/labextension/src/index.ts`
(about 50 lines) registers `thebe-ai:cell-prompt`:

1. It reads the active code cell's text. If the text does not already start with `%%ai`, it
   prepends `%%ai --replace` (optional command args choose the provider, or "below" instead of
   replace).
2. It runs the cell with `NotebookActions.run`.
3. The kernel magic does everything else: the API call, streaming into the output, and
   `set_next_input(replace=True)`. The prompt is kept as `# ai:` comments.

The schema adds a "✨ AI" item to the cell toolbar and `Ctrl Alt G`. Keys never reach the browser,
and the extension has no server part.

**[Demonstrated]** in JupyterLab 4.6.4 + Chromium (`spike/browser_demo.py`, run 3 times in a row).
The user types `print pi to the console` into an empty cell and clicks ✨ AI. The saved cell is then
`# ai: print pi to the console\nimport math\nprint(math.pi)\n`, and the output has no `3.14159`: the
code was not run (screenshot `spike/screenshots/cell-button.png`).

**Build facts** [Verified while building it]:

- JupyterLab 4.6 moved extension builds from `@jupyterlab/builder` to the new
  **`@jupyter/builder` 1.2.3** (Rspack). `@jupyterlab/builder@^4.6.0` is not published: the last
  versions are 4.5.11 and 4.6.0-alpha.5. That is a builder swap within one minor release: churn to
  track.
- The build needs Node (22 here), TypeScript 5.9, 451 MB of `node_modules`, and a Python
  environment with JupyterLab 4.6 for `jupyter labextension build`. It produces a 52 KB
  prebuilt ("federated") extension. JupyterLab loads it from
  `share/jupyter/labextensions/<name>/` without Node or a rebuild.
- For Thebe this means building in a separate Docker build stage, `COPY`ing the 52 KB output into
  the JupyterLab image, and not keeping Node in the image.
- A UX wrinkle: after the replacement the cell still shows the magic's output and execution count
  `[1]`, although its new code has not run. The magic's note says so, but a production version
  should clear the output or make the note stand out.

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
| **notebook-intelligence** | 6.0.0, 2026-09-23 [Verified] | A Copilot-style assistant for JupyterLab (chat, agents). Its dependencies include `anthropic`, `openai`, `litellm`, `claude-agent-sdk`, `mcp`, `ollama` and `cryptography` (+53 packages) [Verified from its metadata]. It has an in-notebook "sparkle" inline-chat popover [Reported]. | **GPL-3.0** [Verified]. A feature-rich agent, much more than prompt → code, with a broad surface. The prompt goes into a popover, not the cell. |
| **jupyterlab-magic-wand** (jupyter-ai-contrib) | 0.6.0, 2025-09-17 [Verified] | "An in-cell AI assistant": asks about or changes a cell and shows a diff [Reported]. Built on `langchain`/`langgraph`, `jupyterlab-cell-diff` and `jupyterlab-eventlistener` [Verified from its metadata]. | Closest to "use this cell", but no release for a year, and a langchain stack. BSD-3. |
| **jupyter-ai-agents** (Datalayer) | 1.0.6, 2026-09-20 [Verified] | Agents that operate notebooks through MCP servers. Needs `jupyterlab>=4.6.1`, `jupyter-collaboration>=5`, `datalayer-core`, `agent-runtimes[all]` and `python-dotenv` [Verified from its metadata]. | Agents that run code; the opposite of "never run". BSD-3. |
| **mito-ai** | 0.1.69, 2026-07-21 [Verified] | An AI chat for JupyterLab (Mito). Its dependencies include `litellm`, `mcp`, `google-genai` and `analytics-python` (a telemetry client) [Verified from its metadata]. | A custom Saga Inc. licence ("see LICENSE.txt"), plus telemetry. Not a fit. |
| **jupyterlite-ai** | 0.20.0, 2026-09-18 [Verified] | Chat and completions that run in the browser. Keys go through `jupyter-secrets-manager` in the frontend [Verified from its metadata]. | Keys in the browser and entered at runtime: against Thebe's rules. BSD-3. |
| **pretzelai** | 4.2.11, 2024-08-29 [Verified] | A **fork** of JupyterLab with AI features. | Replaces JupyterLab itself and has had no release for two years. |
| **jupyterlab-ai-assistant** | 1.1.1, 2025-03-20 [Verified] | An Ollama-only assistant. | Wrong providers. |

None of these offers "the whole cell text is the prompt, the code replaces the cell, nothing
runs" with static server-side keys. B′ (§3.2) does exactly that in about 50 lines on top of C.


## 5. The options in detail

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

### 5.1 Can jupyter-ai be made safe for Thebe?

The question was whether Thebe could ship jupyter-ai 3.x in a locked-down configuration. The
answer depends on which part.

**A1: the full stack (Jupyternaut chat, agents, MCP). Not by configuration.** [Verified in the
3.2.0 sources, `jupyter-ai-jupyternaut` 0.1.0 and the persona/MCP modules]:

- Jupyternaut's toolkits run shell commands, run cells or whole notebooks (through
  `execute_command`), restart kernels and edit notebook cells. There is no approval step, and its
  system prompt tells it to act rather than ask. That is the opposite of "never run generated code".
- Personas are Python files, and the persona manager also loads them from the workspace
  (`.jupyter/personas/`). Any `.py` placed there runs inside the **Jupyter server** process.
- MCP servers listed in a workspace `.jupyter/mcp_settings.json` are started as local commands.
- The model/secrets settings UI writes keys into a `.env` file in the workspace root. That is
  runtime key editing in a file every notebook can read.
- The bundled MCP server listens on `localhost:3001` without authentication, so any process in the
  container can drive JupyterLab through it.

Each of these could be patched out, but that means maintaining a fork of a 0.x stack that changes
every month. **Not recommended.**

**A2: only the magics, loaded by a Thebe wrapper. Yes, with caveats.** The stock
`jupyter-ai-magic-commands` 0.0.4 is **not** safe as installed. Before every call it loads
`os.getcwd()/.env` with `override=True`, i.e. from the notebook's folder in the workspace.

**[Demonstrated]** `spike/jupyter_ai_demo.py` runs two real kernels. Keys and the API address are
set in the kernel environment, as Thebe would set them. A `.env` in the notebook folder sets
`ANTHROPIC_API_BASE` to another server:

```text
stock     … requests=['EVIL server from .env (with the key)']
hardened  … requests=['configured API (with the key)']
stock sends the key to the .env's server: True; hardened stays on the configured API: True
```

So any file in the workspace, whether a cloned repository, an unpacked download or a copied
directory, can redirect the key to a server of its choosing, silently.

`spike/jupyter_ai_hardened.py` (about 50 lines) is loaded **instead of** the stock extension:

1. It takes keys only from Thebe's static config (the same JSON as `thebe_ai`) and turns them
   into aliases (`%%ai claude`, `%%ai openai`) and a default model.
2. It points the magic's `.env` path at an empty file. This patches a **module global**
   (`dotenv_path`), i.e. it depends on jupyter-ai internals.
3. It makes litellm use its bundled price list, instead of downloading one at import.
4. It restores IPython's own error handling. 0.0.4 installs a catch-all exception handler that also
   copies every traceback into a notebook variable `Err`.

What still speaks against A2, compared with our own C:

- **Weight:** +36 packages via litellm, against about 11 for one official SDK.
- **Maturity:** version 0.0.4, and the hardening depends on its internals, so every upgrade must
  be re-checked.
- **Missing features:** no streaming, and no timeout or retry settings.
- **Code quality of `-f code`:** it inserted the model's prose and code fences into the cell in
  the demo; our `extract_code()` keeps only the code.
- **Variables:** `{var}` interpolation sends a variable's *values*. Our `--var` sends only a
  description.
- **A server part after all:** the magics depend on `jupyter-ai-litellm`, which enables itself as
  a Jupyter **server** extension with its own REST handlers. Importing it added 4.8 s to JupyterLab's
  startup (measured). A2 should disable it in the image
  (`jupyter server extension disable jupyter_ai_litellm`; not tried in the spike).

**Verdict:** do not ship jupyter-ai in full. If the team prefers "use the upstream project" over
"own 400 lines", A2 is acceptable: the magics package alone plus the hardened loader, pinned
exactly, with `jupyter_ai_demo.py` turned into a regression test that runs on every upgrade.
Otherwise C is smaller, safer by construction and already covers its features.

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

### B′. Thin cell button on top of the magic (prototyped)

- **Approach:** §3.2. The ✨ AI button (and `Ctrl Alt G`) sends the whole cell text through
  `%%ai --replace`. There is no dialog, no server endpoint and no AI code in the browser.
- **Pros:**
  - Discoverability of B, but with the engine, keys and error handling all in C.
  - About 50 lines of TypeScript and a settings schema. Demonstrated in the real browser.
  - If the extension breaks after an upgrade, `%%ai` still works by hand.
- **Cons:**
  - A Node build stage in the Docker build, and builder churn (`@jupyter/builder` in 4.6).
  - It depends on `NotebookActions.run` and the cell shared model, which are stable public APIs,
    but frontend APIs nonetheless.
  - A cell of Python code sent by mistake becomes a prompt. That is harmless: the text survives in
    the `# ai:` comment (editor undo was not tested).
- **Effort:** about 1 day on top of C (Docker build stage, a Playwright test from
  `browser_demo.py`), then a rebuild check per JupyterLab minor.

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

| | A1: jupyter-ai (full) | A2: jupyter-ai magics, hardened | B: custom dialog extension | B′: thin cell button on C | C: magic | D: helper |
| --- | --- | --- | --- | --- | --- | --- |
| Implementation effort | Low to install; high to make safe (fork) | Low (install + 50-line loader) | High (TS + build + tests) | ~1 day on top of C | Low (spike exists) | Trivial with C |
| Fragility across upgrades | High (0.x, big deps, JupyterLab-coupled) | Medium (patches internals of a 0.0.x package) | High (frontend APIs) | Medium (small; builder churn) | **Low** (IPython magic API, kernel payload) | **Low** |
| UX | Richest (chat, agents) | Good for keyboard users; no streaming | Good (button + dialog) | **Good**: write the prompt in the cell, click ✨ | Good for keyboard users | Fine for scripting |
| Discoverability | High | Low–medium | High | **High** | Low–medium | Low |
| Security | **Unacceptable as shipped** (unapproved code/shell tools, workspace persona code, `.env` keys, open MCP port) | Acceptable **only** hardened; stock leaks the key via a workspace `.env` (demonstrated) | Medium (frontend, maybe a server endpoint) | Narrow (no server part; keys stay in the kernel) | **Narrow** (kernel-only; no auto-exec) | Narrow |
| Maintenance | Track jupyter-ai + litellm | Re-verify the hardening on every upgrade | Own TS extension | ~50 lines TS + build stage | ~400 lines of Python | Included |
| Static key config | Fights the `.env`/UI design | Via the loader | Via C | Via C | **Native** | Native |
| Dependencies | +96 to +118 | +36 | Node at build time | Node at build time | +10 / +11 per SDK | — |

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

Estimated at 1–2 days.

**Then, as a separate small step, the thin cell button B′** (about 1 day): it is the one frontend
piece worth its cost, because it turns "the whole cell is the prompt" into one click and keeps all
logic in C. Build it in a Docker build stage (Node never enters the runtime image), and turn
`spike/browser_demo.py` into its test. It is optional: C stands on its own, and B′ can be
dropped for an upgrade if `@jupyter/builder` breaks.

**jupyter-ai:** not in full (§5.1). The hardened magics profile (A2) is the fallback if the team
would rather depend on upstream than own the magic, but it brings +36 packages and relies on
patching internals.

## 9. What NOT to implement yet

- Runtime key entry or editing, a settings or secrets UI, and key storage in the workspace (`.env`).
- Auto-execution of generated code, or a `--run` flag.
- Custom syntax (`$$$…`), `!` extensions or input transformers.
- A chat sidebar, agents, tool use, MCP, or notebook-wide context (reading other cells, files or
  outputs).
- Inline completion (ghost text while typing).
- A prompt dialog, a server endpoint for the button, or any AI call from the browser (B). The
  thin button B′ comes after C, if at all.
- jupyter-ai in full (Jupyternaut, personas, MCP, the secrets UI), or its magics without the
  hardened loader.
- More providers (local models, Bedrock, …) beyond the two requested.
- Cost tracking, per-user quotas or usage dashboards.

## 10. Possible later evolution

1. **Cell button on top of the magic (B′):** prototyped and demonstrated (§3.2). Later variants:
   a second toolbar item for "insert below" (`mode: "below"` is already supported), and a
   provider picker.
2. **Context options:** `--cell` (include the current or previous cell's source), and
   `--error` (include the last traceback) for "fix this".
3. **Conversation mode:** keep the last N exchanges per kernel (jupyter-ai keeps 2), off by
   default.
4. **Server-side key proxy:** a jupyter-server extension that holds the key, so kernels send
   prompts without seeing it. It needs auth and rate limiting.
5. **Inline completion provider** (JupyterLab 4.1+ `IInlineCompletionProvider`), if wanted.
6. **Revisit jupyter-ai** once its v3 packages reach 1.x, static key configuration is first-class,
   and tools need approval.
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
| `spike/labextension/` | B′: the ✨ AI cell-toolbar button (TypeScript, settings schema, `@jupyter/builder`). |
| `spike/browser_demo.py` | JupyterLab 4.6.4 + headless Chromium: the button flow and the `%%ai` flow, checked from the saved notebook. |
| `spike/screenshots/` | `cell-button.png`, `cell-magic.png` from that run. |
| `spike/jupyter_ai_hardened.py` | A2: loads jupyter-ai's magics under Thebe's rules. |
| `spike/jupyter_ai_demo.py` | Stock vs. hardened jupyter-ai magics with a hostile `.env` in the notebook folder. |

Run them (outside Thebe, in a separate venv):
`pip install ipykernel jupyter_client openai anthropic`, then
`cd spike && python -m unittest discover -s tests`, `python kernel_demo.py`,
`python interrupt_demo.py`.

- **jupyter-ai demo:** also `pip install jupyter-ai-magic-commands==0.0.4`, then
  `python jupyter_ai_demo.py`.
- **Browser demo:** also `pip install jupyterlab==4.6.4 playwright`, and build the extension:
  `cd labextension && npm install && npm run build`, with that venv's `bin` on `PATH` (the script
  runs `tsc`, then `jupyter labextension build .`). Copy `thebe_ai_cell/labextension` to
  `<venv>/share/jupyter/labextensions/thebe-ai-cell`, then run `python browser_demo.py`.

**Not done in the spike:**

- no request to the real OpenAI or Anthropic APIs (no keys);
- nothing inside a deployed Thebe (no image, compose or `config.yaml` changes, on purpose);
- no Playwright/Galata test wired into CI, and no Docker build stage for the extension;
- jupyter-ai's Jupyternaut and MCP findings (§5.1) come from reading the 3.2.0 sources, and were not
  exploited in a running server.
