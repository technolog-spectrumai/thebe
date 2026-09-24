# AI code generation in notebooks — user manual (spike)

> **Read this first.** This describes an *exploratory spike* on the `ai_spike` branch. **Nothing in
> this manual is part of a deployed Thebe yet.** A deploy does not install it, `config.yaml` has no
> `ai:` section, and JupyterLab does not load it. The technical research behind it is in
> [ai_spike.md](ai_spike.md).

Every feature below carries one of three labels:

| Label | Meaning |
| --- | --- |
| **[Demonstrated]** | Exists as code in `spike/` and was shown working in the spike: unit tests plus a real IPython 9.17 / ipykernel 7.3 kernel (the versions in Thebe's image), with a local stand-in for the OpenAI and Anthropic APIs. Where marked *(browser)*, also clicked through in JupyterLab 4.6.4 in headless Chromium. **Not** run with real API keys, and not inside a deployed Thebe. |
| **[Feasible]** | The building blocks were checked (JupyterLab 4.6.4's own code, the SDKs), but the piece is not wired up. |
| **[Proposed]** | A design only. Nothing exists yet. |

## 1. What it is meant to do

You write what you want in plain language. The AI writes Python code, and that code appears in
the notebook: in a **new cell below**, or, with the ✨ AI button, **in place of your prompt**.
Nothing runs by itself: you read the code, change it if needed, and run it with **Shift+Enter**.

```python
%%ai
Load sales.csv from the workspace, sum revenue per region and plot it as a bar chart.
```

→ a new cell appears below, containing something like:

```python
import pandas as pd
import matplotlib.pyplot as plt

sales = pd.read_csv("sales.csv")
per_region = sales.groupby("region")["revenue"].sum()
per_region.plot(kind="bar")
plt.show()
```

## 2. Status at a glance

| Feature | Status |
| --- | --- |
| `%%ai` cell magic: prompt → code in a new cell below | **[Demonstrated]** |
| `%%ai openai` / `%%ai claude`: choosing the provider | **[Demonstrated]** |
| `--model`, `--replace`, `--print`, `--var NAME` | **[Demonstrated]** |
| `%ai status` (which providers are set up; keys masked) | **[Demonstrated]** |
| `%ai <one-line prompt>` | **[Demonstrated]** |
| `from thebe_ai import ask` (the same thing as a Python function) | **[Demonstrated]** |
| The answer streams into the cell output while it is written | **[Demonstrated]** (against the stand-in API) |
| Clear one-line errors (no key, key rejected, rate limit, timeout, no network) | **[Demonstrated]** |
| The new cell appearing in JupyterLab's notebook, not run | **[Demonstrated]** *(browser)* |
| ✨ AI button in the cell toolbar: the whole cell is the prompt, the cell becomes the code | **[Demonstrated]** *(browser)* in the spike; shipping it in Thebe's image is **[Proposed]** |
| `Ctrl+Alt+G`: the same as the ✨ AI button | **[Feasible]** — declared by the spike extension, not pressed in the browser demo |
| `%ai` available without `%load_ext thebe_ai` (kernel config) | **[Demonstrated]** *(browser)* in the spike; in Thebe **[Proposed]** |
| API keys in `config.yaml` → Thebe hands them to JupyterLab | **[Proposed]** |
| Chat sidebar, inline autocomplete, agents | Not planned (see §9 and ai_spike.md §5.1) |

## 3. Configuring API keys — **[Proposed]**

Keys would be set **once, before Thebe starts**, in `config.yaml` (the same file as the password and
ports; kept at mode 600). There would be **no screen for entering or changing keys** in JupyterLab
or the builder.

```yaml
# config.yaml — PROPOSED, not read by any Thebe version yet
ai:
  default: claude            # used by a plain %%ai
  timeout: 60                # seconds per request
  providers:
    claude:
      api: anthropic
      model: claude-sonnet-5
      api_key: "sk-ant-..."
    openai:
      api: openai
      model: gpt-5.4-mini
      api_key: "sk-..."
```

- You only need the providers you use. A provider without `api_key` is simply not offered.
- The model names are examples taken from the SDKs' own model lists. Choose the ones your account
  may use.
- **To change a key or model:** edit `config.yaml`, deploy again (`./run.sh update` or Deploy in the
  builder), then restart the notebook's kernel (*Kernel → Restart Kernel*).
- The keys would reach only the JupyterLab container, as a private file: never the dashboard,
  the package runner or the browser.

**Important:** code running in your notebooks runs as you, so it *can* read that key. Use a key
created for Thebe, with a spending limit set in the provider's console.

**In the spike today [Demonstrated]:** the prototype reads a JSON file named by `THEBE_AI_CONFIG`,
or the environment variables `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`. Models can be set with
`THEBE_AI_CLAUDE_MODEL` / `THEBE_AI_OPENAI_MODEL`.

## 4. Using it from a notebook

### Turning it on

```python
%load_ext thebe_ai        # [Demonstrated] — in the proposed MVP this line would not be needed
%ai status                # which providers are configured, keys shown as …1234
```

### The ✨ AI button: the whole cell is the prompt — **[Demonstrated]** *(browser, spike only)*

1. Type your request into an empty code cell, in plain language, without `%%ai`:

   ```text
   print pi to the console
   ```

2. Click **✨ AI** in the cell's toolbar (top right of the cell, next to the copy and move
   buttons).
3. The answer streams into the cell's output. When it is complete, **the cell's text is replaced
   by the code**, with your prompt kept at the top as a comment:

   ```python
   # ai: print pi to the console
   import math
   print(math.pi)
   ```

4. **The code has not run.** The cell still shows the AI's answer as its output and the old run
   number, but that belongs to the request, not to the new code. Read the code, then press
   **Shift+Enter**.

Good to know:

- The button sends the **entire** cell. Use it on a cell that contains only your request.
- If the cell already starts with `%%ai` (with options such as `openai` or `--var df`), the button
  just runs it as it is, so the options apply.
- Your prompt stays in the `# ai:` comment, so you can copy it back. Undo in the cell should also
  work, but the spike did not test it.
- The button does nothing on an empty cell or a Markdown cell.
- Behind the scenes the button only puts `%%ai --replace` in front of your text and runs the cell.
  The key stays in the kernel, and the browser never sees it.

### Asking for code — **[Demonstrated]**

```python
%%ai
Read measurements.csv, drop rows with missing values and print summary statistics.
```

- The answer is printed below the cell as it arrives, so you can watch it being written.
- The code (without the surrounding explanation) goes into **a new cell below**, and a note says so.
- **It is not executed.** Read it, then press **Shift+Enter** in the new cell.

### Choosing the provider or model — **[Demonstrated]**

```python
%%ai openai
Write a function that parses ISO 8601 dates with and without time zones, with tests.
```

```python
%%ai claude --model claude-opus-5
Refactor this into smaller functions: <paste code here>
```

### Letting the AI see your variables — **[Demonstrated]**

```python
%%ai --var df
Plot the monthly average of the "temperature" column, one line per station.
```

`--var NAME` sends a **description** of the variable: its type, shape, column names and types, or
dictionary keys. It **never sends the data values**. Give it several times for several variables:
`--var df --var stations`.

### Where the code goes — **[Demonstrated]**

| Option | Effect |
| --- | --- |
| *(none)* | New cell below the prompt cell. |
| `--replace` | The prompt cell becomes the code. Your prompt is kept at the top as `# ai: …` comments. |
| `--print` | The code is only shown in the output; nothing is inserted. |

There is no option to run the code automatically, on purpose.

### Quick one-liners — **[Demonstrated]**

```python
%ai claude convert the column "date" of df to datetime
```

The line form takes an optional provider and the prompt; the options above need `%%ai`.

### As a Python function — **[Demonstrated]**

```python
from thebe_ai import ask
code = ask("a regex that matches Polish postal codes like 00-950")
print(code)
ask("plot a sine wave", provider="openai", insert=True)   # also puts it in a new cell below
```

### Example workflows

1. **Explore a new file:** `%%ai --var df` + "show the columns with missing values and a histogram of
   each numeric column" → read the code, run it, then ask for the next step.
2. **Fix an error:** copy the traceback into `%%ai` with "fix this" and the failing code.
3. **Boilerplate:** "a function that downloads a URL with retries and a timeout" → `--print` if you
   only want to look.

## 5. What to expect

- **Answer time:** a few seconds to about a minute. Waiting is capped at `timeout` (60 s proposed).
  Busy services are retried a couple of times automatically.
- **Stopping [Demonstrated]:** *Kernel → Interrupt* (or ■) stops a request; nothing is inserted.
- **Undo:** the inserted cell is a normal cell. Delete it, or undo in the notebook.
- **Conversation:** there is none. Each `%%ai` is independent; the AI does not remember earlier cells.
- **Cost:** every request is billed by the provider, to the key you configured.

## 6. Limitations

- Not available in a deployed Thebe yet (see the labels above).
- After the ✨ AI button replaces a cell, its output and run number are still those of the
  request (see above).
- Produces code only: no chat, no autocomplete while typing, no automatic fixes.
- The AI sees only your prompt and the `--var` descriptions: not your files, other cells or outputs.
- Generated code can be wrong or unsafe. Read it before running it, especially anything that deletes
  or writes files.
- Your prompt and the variable descriptions are sent to OpenAI or Anthropic. Do not put
  confidential data into prompts.
- Requires internet access from the JupyterLab container (Thebe allows outbound traffic today).

## 7. Troubleshooting

The messages below are the prototype's own **[Demonstrated]** wording.

| Message | What to do |
| --- | --- |
| ``UsageError: Cell magic `%%ai` not found.`` | Run `%load_ext thebe_ai` first (in the spike). If this came from the ✨ AI button, the cell now starts with `%%ai --replace`: run the cell again after loading. |
| No ✨ AI in the cell toolbar | The spike extension is not installed in that JupyterLab (see §8). `%%ai` still works without it. |
| `No AI provider has an API key` | No key is configured (proposed: `config.yaml` → deploy → restart the kernel). |
| `No AI provider named 'x' is configured` | Use one of the names `%ai status` lists. |
| `the API key was rejected` | The key is wrong, revoked or for another provider. Replace it and redeploy. |
| `the model is not available to this key` | Check the model name and that your account may use it. |
| `rate limited (still after 2 retries)` | Wait a minute; check the plan's limits in the provider's console. |
| `no answer within 60 s` | Retry; for long requests raise `timeout`. |
| `cannot reach the API (network, proxy or DNS)` | Check the machine's internet connection. |
| `The anthropic package is not installed` | The SDK is missing from the kernel's environment (the MVP would add it to the image). |
| `--var df: no such variable` | Run the cell that defines it first. |

## 8. Trying the spike today — for the curious

The prototype runs outside Thebe, in a separate Python environment, without any API key:

```bash
python3 -m venv /tmp/ai
/tmp/ai/bin/pip install ipykernel jupyter_client openai anthropic
cd spike && /tmp/ai/bin/python -m unittest discover -s tests -v   # 11 tests, stand-in API
/tmp/ai/bin/python kernel_demo.py                                  # a real kernel runs %%ai
```

The browser demo (JupyterLab 4.6.4 + headless Chromium, with the ✨ AI button) needs Node to build
the extension once. The steps are in ai_spike.md §11. It then runs as `python browser_demo.py`,
checks both flows from the saved notebooks and stops JupyterLab again.

With real keys, in a local Jupyter of your own (**[Feasible]**, not tried in the spike):
`ANTHROPIC_API_KEY=… PYTHONPATH=…/spike jupyter lab`, then `%load_ext thebe_ai` in a notebook.

## 9. What about jupyter-ai?

[jupyter-ai](https://github.com/jupyterlab/jupyter-ai) is Project Jupyter's own AI extension (chat
assistant, agents and an `%%ai` magic). The spike checked whether Thebe could ship it safely
(details in ai_spike.md §5.1):

- **The full jupyter-ai (chat assistant "Jupyternaut"):** not planned. Its assistant can run cells
  and shell commands without asking, and it takes keys and extra code from files in the
  workspace.
- **Only its `%%ai` magic:** possible, but only through a Thebe wrapper
  (`spike/jupyter_ai_hardened.py`, **[Demonstrated]**). Installed as is, it reads a `.env` file
  from the notebook's folder before every request. The spike showed that such a file can send
  your key to a different server without any sign.

**If you use jupyter-ai yourself** (e.g. in the custom packages venv), never keep API keys in a
`.env` file in the workspace, and do not open notebooks from folders you do not trust while a key
is loaded.
