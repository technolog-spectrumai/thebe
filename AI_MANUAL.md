# AI code generation in notebooks

You write what you want in plain language; an AI model (OpenAI or Anthropic) writes the Python
code, and the code appears in the notebook. **Nothing runs by itself:** you read the code, change
it if needed, and run it with **Shift+Enter**.

There are two ways to ask:

- **The ✨ AI button** in the toolbar of a cell (or **Ctrl+Alt+G**): the whole cell is the request,
  and the cell is replaced by the code.
- **The `%%ai` magic**: the request goes below a `%%ai` line, and the code goes into a new cell below.

Both need an `ai:` section in `config.yaml`. The technical side is in the README,
[AI code generation](README.md#ai-code-generation).

## 1. Turning it on

Add an `ai:` section to `config.yaml` (next to the password and the ports; the file is kept at mode
600) and deploy:

```yaml
ai:
  default: "claude"                # what a plain %%ai uses
  providers:
    claude:
      api: "anthropic"
      model: "claude-sonnet-5"
      api_key: "sk-ant-..."
    openai:
      api: "openai"
      model: "gpt-5.4-mini"
      api_key: "sk-..."
  budget:
    max_tokens: 1000000            # tokens per period, all providers together; 0 = no limit
    period: "month"                # month: starts again on the 1st (UTC); total: never
  timeout: 60                      # seconds to wait for one answer
  max_output_tokens: 2048          # the longest answer, in tokens
```

```bash
./run.sh check      # shows the section with the keys masked (…1234); changes nothing
./run.sh update     # or Deploy in the builder
```

| Setting | Meaning |
| --- | --- |
| `providers.<name>` | One entry per provider you use. The name (lowercase letters, digits, `-`, `_`) is what you type after `%%ai`. At most 8. |
| `api` | `anthropic` or `openai`. |
| `model` | The model name your account may use, e.g. `claude-sonnet-5`, `claude-opus-5`, `gpt-5.4-mini`, `gpt-5.5`. |
| `api_key` | The key, in quotes. Use a key created for Thebe, with a spending limit set in the provider's console. |
| `base_url` | Optional: a compatible endpoint or company proxy instead of the provider's own API. |
| `default` | The provider of a plain `%%ai` (default: the first one listed). |
| `budget.max_tokens` | How many tokens (input + output, all providers together) may be used per period. `0` = no limit. |
| `budget.period` | `month`: the count starts again on the 1st of each month (UTC). `total`: it never does. |
| `timeout` | Seconds to wait for one answer (5–600). |
| `max_output_tokens` | The longest answer, in tokens. |
| `enabled` | `false` switches AI off but keeps the section. |

- **Keys are entered only here.** There is no screen for them in JupyterLab, the dashboard or the
  builder, and they never reach notebooks or the browser (they stay in the AI gateway container).
- **To change a key, a model or the budget:** edit `config.yaml` and run `./run.sh update`. Kernels
  need no restart.
- **To turn AI off:** remove the section or set `enabled: false`, then update.
- If the section has a mistake, `run.sh` and the builder refuse to deploy and name it
  (e.g. `ai.budget.period must be one of: month, total.`). The builder never rewrites `config.yaml`
  while the section has a problem, so no key is lost.

## 2. Asking with the ✨ AI button

1. Type the request into a code cell, in plain language, without `%%ai`:

   ```text
   print pi to the console
   ```

2. Click **✨ AI** in the cell's toolbar (top right of the cell), or press **Ctrl+Alt+G**.
3. The answer streams into the cell's output. Then **the cell's text is replaced by the code**, with
   your request kept as comments on top:

   ```python
   # ai: print pi to the console
   import math
   print(math.pi)
   ```

4. **The code has not run.** The cell still shows the AI's answer and the run number of the
   request. Read the code, then press **Shift+Enter**.

- The button sends the **whole** cell: use it on a cell that holds only your request.
- A cell that already starts with `%%ai` (for example `%%ai openai --var df`) is run as it is, so
  its options apply.
- Your request stays in the `# ai:` comments if you want it back.
- The button does nothing on an empty cell or a Markdown cell.

## 3. Asking with `%%ai`

```python
%%ai
Read measurements.csv, drop rows with missing values and print summary statistics.
```

The answer is shown below the cell as it arrives; the code in it goes into **a new cell below**,
not run. A line at the end says how many tokens the answer took, how long, and how many are left:

```text
%%ai: code inserted in a new cell below. It has not run: read it, then press Shift+Enter.
[claude claude-sonnet-5: 312 + 540 tokens, 4.2 s; 987,148 of 1,000,000 tokens left this month (until 2026-10-01)]
```

Options (on the `%%ai` line):

| Option | Effect |
| --- | --- |
| `openai`, `claude`, … | The provider, by its name in `config.yaml`. |
| `--model NAME` (`-m`) | Another model of that provider, for this request. |
| `--replace` | This cell becomes the code (what the ✨ button does). |
| `--print` | Only show the code; insert nothing. |
| `--var NAME` (`-v`) | Tell the AI about a notebook variable: its type, shape, column names and types, or dictionary keys. **Never its values.** Repeat for several (at most 20). |

```python
%%ai openai --var df
Plot the monthly average of the "temperature" column, one line per station.
```

There is no option that runs the code, on purpose.

### Quick one-liners and status

```python
%ai claude convert the column "date" of df to datetime
%ai status
```

`%ai status` shows the providers (the default marked), the budget left, the number of answers and
their mean time ± standard deviation, the timeout and the longest answer.

### As a Python function

```python
from thebe_ai import ask
code = ask("a regex that matches Polish postal codes like 00-950")
ask("plot a sine wave", provider="openai", insert=True)   # also puts it in a new cell below
```

## 4. The token budget and the Statistics page

Every answer's input and output tokens (the providers' own counts) are added up for the current
period. When `budget.max_tokens` is reached, requests are refused until the period starts again:

```text
%%ai: The AI token budget is used up: 1,000,000 of 1,000,000 tokens this month. It starts again on
2026-10-01 (UTC). To continue now, raise ai.budget.max_tokens in config.yaml and run update.
```

- An answer is capped at what is left, so the last one of a period may be cut short; a line then
  says so.
- The count is exact once an answer is complete, so the total can go over the budget by a few
  tokens.
- A request the budget clearly cannot cover (a very long prompt) is refused before it is sent.

The **Statistics** page (the dashboard) shows:

- the **AI tokens** tile: the share of the budget left, the tokens left and the mean answer time;
- the **AI** card: tokens left and used (input / output), the budget, when it starts again, the
  number of answers and failures, and the **mean and standard deviation of the answer times**,
  overall and per provider. Times run from the request to the last token, and only successful
  answers count.

## 5. What to expect

- **Answer time:** a few seconds to about a minute, at most `timeout` per attempt. A busy service
  is retried twice automatically.
- **Stopping:** *Kernel → Interrupt* (■) stops waiting, and nothing is inserted. The answer is still
  received in the background and its tokens count.
- **Undo:** the inserted cell is a normal cell: delete it, or undo in the notebook.
- **No conversation:** each request stands alone; the AI does not remember earlier requests, and it
  sees only your request and the `--var` descriptions: not your files, other cells or outputs.
- **At most 4 answers at once** for the whole JupyterLab.
- **Cost:** every answer is billed by the provider to the key in `config.yaml`.

## 6. Safety

- Generated code can be wrong or unsafe. Read it before running it, especially anything that
  deletes or writes files or reaches the network. A traceback, file header or web page you paste
  into a request can steer the model.
- Your request and the `--var` descriptions are sent to OpenAI or Anthropic. Do not put
  confidential data into requests.
- Anyone with the JupyterLab password can use the AI up to the budget, but cannot read the API keys.

## 7. Troubleshooting

| Message or symptom | What to do |
| --- | --- |
| `AI is off in this Thebe: config.yaml has no ai: section, or the AI gateway is not running.` | Add the section (§1) and run `./run.sh update`. If it is there: `./run.sh status` should list `ai (AI gateway)` as running; `./run.sh logs ai` shows why it is not. |
| `The AI token budget is used up: …` | Wait for the date it names, or raise `ai.budget.max_tokens` and update. |
| `Too little of the AI token budget is left for this prompt: …` | Shorten the request, or raise the budget. |
| `No AI provider named 'x' is configured (configured: claude, openai).` | Use one of the names listed. |
| `claude (claude-sonnet-5): the API key was rejected. …` | The key is wrong, revoked or for the other provider. Fix `api_key` and update. |
| `…: the model is not available to this key. Check the model name.` | Check `model` (or `--model`) and that your account may use it. |
| `…: rate limited (still after 2 retries). …` | Wait a minute; check the plan's limits in the provider's console. |
| `…: no answer within 60 s.` | Try again; for long answers raise `timeout`. |
| `…: cannot reach the API (network, proxy or DNS).` | Check the machine's internet connection (or `base_url`). |
| `4 AI answers are already being written; wait for one to finish.` | Another notebook is asking; try again in a moment. |
| `The AI gateway refused this JupyterLab's token. Run restart.` | Run `./run.sh restart`. |
| `--var df: no such variable in the notebook. …` | Run the cell that defines it first. |
| `the answer was cut off at the output token limit …` | Raise `max_output_tokens`, ask for less, or check the budget. |
| ``UsageError: Cell magic `%%ai` not found.`` | Restart the kernel (*Kernel → Restart Kernel*). Kernels started before an update may not have it. |
| No ✨ AI in the cell toolbar | Reload the page after an update. In a JupyterLab terminal, `jupyter labextension list` should show `thebe-ai-cell … enabled OK`. |
| `run.sh` or the builder refuse to deploy, naming an `ai.` setting | Fix that line in `config.yaml`; `./run.sh check` lists every problem. |
