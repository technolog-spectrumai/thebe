// Dependencies page: requirements editor, pip job control, live log and installed packages.
// Talks to /api/dependencies/* (proxied by the dashboard to the deps runner). Data only ever
// reaches the DOM through textContent, createElement and CSSOM properties (never innerHTML).
//
// Polling: while a job runs, the log every 1 s (its answer says when the job has ended) and
// the state every 5 s; otherwise the state every 5 s. Nothing is polled while the tab is hidden.
(() => {
  "use strict";

  const API = "/api/dependencies";
  const STATE_POLL_MS = 5000;
  const LOG_POLL_MS = 1000;
  const REQUEST_TIMEOUT_MS = 15000;
  const CACHE_CLEAR_TIMEOUT_MS = 130000;
  // Chunks fetched per poll when catching up with a long log (64 KB each).
  const LOG_CHUNKS_PER_POLL = 16;
  // Keep the pane responsive: beyond this, the oldest text is dropped from the view.
  const LOG_MAX_CHARS = 500000;
  const GIB = 1024 ** 3;
  const FREE_LEVELS = { caution: 10 * GIB, warn: 5 * GIB };
  const DASH = "—";

  const STATUS = {
    idle: ["idle", "Idle"],
    running: ["caution", "Installing…"],
    succeeded: ["ok", "Succeeded"],
    failed: ["warn", "Failed"],
    cancelled: ["idle", "Cancelled"],
    interrupted: ["caution", "Interrupted"],
  };
  const ACTIONS = { install: "Install / update", reset: "Reset & reinstall" };

  const $ = (id) => document.getElementById(id);
  const isNum = (value) => typeof value === "number" && Number.isFinite(value);

  const editor = $("requirements");
  const logPane = $("log");
  const buttons = {
    install: $("btn-install"),
    reset: $("btn-reset"),
    cancel: $("btn-cancel"),
    clearCache: $("btn-clear-cache"),
    save: $("btn-save"),
  };

  // ---- formatting -------------------------------------------------------------------

  const BYTE_UNITS = ["B", "KiB", "MiB", "GiB", "TiB"];

  function bytesParts(bytes) {
    if (!isNum(bytes)) return null;
    let value = Math.abs(bytes);
    let unit = 0;
    while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
      value /= 1024;
      unit += 1;
    }
    const digits = unit === 0 || value >= 100 ? 0 : value >= 10 ? 1 : 2;
    return [value.toFixed(digits), BYTE_UNITS[unit]];
  }

  const bytesText = (bytes) => {
    const parts = bytesParts(bytes);
    return parts ? `${parts[0]} ${parts[1]}` : DASH;
  };

  function parseTime(iso) {
    const ms = typeof iso === "string" ? Date.parse(iso) : NaN;
    return Number.isNaN(ms) ? null : ms;
  }

  function formatDateTime(iso) {
    const ms = parseTime(iso);
    return ms === null
      ? DASH
      : new Date(ms).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function formatDuration(seconds) {
    if (!isNum(seconds)) return DASH;
    const s = Math.max(0, Math.round(seconds));
    const hours = Math.floor(s / 3600);
    const minutes = Math.floor((s % 3600) / 60);
    const rest = s % 60;
    if (hours) return `${hours} h ${minutes} min`;
    if (minutes) return `${minutes} min ${rest} s`;
    return `${rest} s`;
  }

  // ---- DOM helpers ------------------------------------------------------------------

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function setPill(pill, tone, text) {
    pill.classList.remove("pill--idle", "pill--ok", "pill--caution", "pill--warn", "pill--accent", "pill--live");
    pill.classList.add(`pill--${tone}`);
    const label = pill.querySelector("span:last-child");
    (label || pill).textContent = text;
  }

  function setNote(note, tone, message) {
    note.hidden = !message;
    note.classList.remove("note--warn", "note--caution", "note--info");
    if (tone) note.classList.add(`note--${tone}`);
    note.querySelector(".note__text").textContent = message || "";
  }

  function tile(id) {
    const root = $(id);
    return {
      value: root.querySelector('[data-part="value"]'),
      unit: root.querySelector('[data-part="unit"]'),
      meta: root.querySelector('[data-part="meta"]'),
      icon: root.querySelector(".tile__icon"),
      meter: root.querySelector(".meter"),
    };
  }

  const tiles = { venv: tile("tile-venv"), cache: tile("tile-cache"), free: tile("tile-free") };

  function setTile(t, bytes, meta, tone = "") {
    const parts = bytesParts(bytes);
    t.value.textContent = parts ? parts[0] : DASH;
    t.unit.textContent = parts ? parts[1] : "";
    t.meta.textContent = meta || " ";
    t.icon.classList.toggle("tile__icon--caution", tone === "caution");
    t.icon.classList.toggle("tile__icon--warn", tone === "warn");
  }

  // ---- API --------------------------------------------------------------------------

  // Every write sends JSON plus X-Requested-With, which the dashboard requires (CSRF guard).
  async function api(method, path, body, timeoutMs = REQUEST_TIMEOUT_MS) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), timeoutMs);
    const init = {
      method,
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal: controller.signal,
    };
    if (method !== "GET") {
      init.headers["Content-Type"] = "application/json";
      init.headers["X-Requested-With"] = "thebe";
      init.body = JSON.stringify(body || {});
    }
    try {
      const response = await fetch(path, init);
      let data = null;
      try {
        data = await response.json();
      } catch (error) {
        data = null;
      }
      return { status: response.status, data: data && typeof data === "object" ? data : {} };
    } catch (error) {
      const message = error && error.name === "AbortError" ? "The request timed out." : "The dashboard is not reachable.";
      return { status: 0, data: { error: message } };
    } finally {
      window.clearTimeout(timer);
    }
  }

  // ---- page state -------------------------------------------------------------------

  let state = null; // last successful /api/dependencies answer
  let savedText = null; // requirements as the runner has them
  let connection = "connecting"; // connecting | ok | unavailable | signin
  let pending = false; // a button's request is in flight
  let stateLoadedAt = 0; // Date.now() of the last successful state
  let logJobId = null;
  let logOffset = 0;
  let logLength = 0;

  const isRunning = () => Boolean(state && state.job && state.job.status === "running");
  const isDirty = () => savedText !== null && editor.value !== savedText;

  // Answers every call site treats the same way. Returns true when it handled the result.
  function handleConnection(result) {
    if (result.status === 401) {
      setConnection("signin", "Sign-in required: reload the page.");
      return true;
    }
    if (result.status === 0 || result.status === 502 || result.status === 503) {
      const reason = result.data.error || "The dependency runner did not answer.";
      setConnection("unavailable", reason);
      return true;
    }
    return false;
  }

  function setConnection(next, message) {
    connection = next;
    const banner = $("deps-unavailable");
    if (next === "ok") {
      banner.hidden = true;
    } else if (next !== "connecting") {
      banner.hidden = false;
      $("deps-unavailable-title").textContent =
        next === "signin" ? "Sign-in required" : "Package runner unavailable";
      // The proxy's generic 503 text only repeats the title; show other reasons (timeouts, 502s).
      const reason = /^dependency runner unavailable\.?$/i.test(String(message || "").trim()) ? "" : `${capitalize(message)} `;
      $("deps-unavailable-text").textContent =
        next === "signin"
          ? message
          : `${reason}Packages that are already installed keep working in notebooks. ` +
            "Check the deps container with: ./setup-jupyterlab-tailscale.sh status";
      setPill($("job-pill"), "warn", next === "signin" ? "Signed out" : "Unavailable");
      if (state === null) $("deps-summary").textContent = "The package runner is not reachable.";
    }
    $("deps").classList.toggle("is-stale", next !== "ok");
    updateControls();
  }

  function capitalize(text) {
    const value = String(text || "").trim();
    const sentence = value.charAt(0).toUpperCase() + value.slice(1);
    return /[.!?]$/.test(sentence) ? sentence : `${sentence}.`;
  }

  function updateControls() {
    const ready = connection === "ok" && state !== null;
    const running = isRunning();
    buttons.install.disabled = !ready || running || pending;
    buttons.reset.disabled = !ready || running || pending;
    buttons.cancel.hidden = !running;
    buttons.cancel.disabled = !ready || pending || (state && state.job.message === "Cancelling…");
    buttons.clearCache.disabled = !ready || running || pending;
    buttons.save.disabled = !ready || running || pending || !isDirty();
    editor.disabled = state === null;
    // Before the first state the greyed-out example text would look like saved requirements.
    editor.placeholder = state === null ? "" : editor.dataset.placeholder || "";

    let editorState = "";
    if (savedText !== null) {
      if (isDirty()) editorState = running ? "Unsaved changes (saving is possible after the job)" : "Unsaved changes";
      else editorState = savedText.trim() ? "Saved" : "Empty: Install / update only prepares the environment";
    }
    $("req-state").textContent = editorState;
  }

  // ---- rendering --------------------------------------------------------------------

  function renderJob() {
    const job = (state && state.job) || {};
    const [tone, label] = STATUS[job.status] || STATUS.idle;
    const pill = $("job-pill");
    setPill(pill, tone, job.status === "running" && job.action === "reset" ? "Reinstalling…" : label);
    pill.classList.toggle("pill--live", job.status === "running");

    $("job-id").textContent = job.id ? `#${String(job.id).slice(0, 8)}` : "";
    $("job-action").textContent = ACTIONS[job.action] || DASH;
    $("job-started").textContent = formatDateTime(job.started_at);
    $("job-finished").textContent = job.status === "running" ? "running…" : formatDateTime(job.finished_at);
    const started = parseTime(job.started_at);
    const finished = job.status === "running" ? Date.now() : parseTime(job.finished_at);
    $("job-duration").textContent = started !== null && finished !== null ? formatDuration((finished - started) / 1000) : DASH;

    let message = job.message || (job.id ? "" : "No job has run yet.");
    if (isNum(job.exit_code) && job.status === "failed" && !/status/.test(message)) {
      message = `${message} (exit status ${job.exit_code})`;
    }
    $("job-message").textContent = message;
    $("log-state").textContent = !job.id ? "No job has run yet" : job.status === "running" ? "Following live output" : `Output of the last job (${label.toLowerCase()})`;
  }

  function renderSizes(sizes) {
    const measuredMs = parseTime(sizes.computed_at);
    const measured =
      measuredMs === null
        ? "measuring…"
        : `measured ${new Date(measuredMs).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
    setTile(tiles.venv, sizes.venv_bytes, isNum(sizes.venv_bytes) ? measured : "measuring…");
    setTile(tiles.cache, sizes.cache_bytes, isNum(sizes.cache_bytes) ? measured : "measuring…");

    const free = sizes.free_bytes;
    const total = sizes.total_bytes;
    const tone = !isNum(free) ? "" : free < FREE_LEVELS.warn ? "warn" : free < FREE_LEVELS.caution ? "caution" : "";
    const usedPct = isNum(free) && isNum(total) && total > 0 ? ((total - free) / total) * 100 : null;
    setTile(tiles.free, free, isNum(total) ? `of ${bytesText(total)}${usedPct !== null ? ` · ${Math.round(usedPct)}% used` : ""}` : "", tone);
    const fill = tiles.free.meter.querySelector(".meter__fill");
    // CSSOM property writes are allowed by the page's CSP; style="" attributes are not.
    fill.style.width = `${usedPct === null ? 0 : Math.min(100, Math.max(0, usedPct))}%`;
    fill.classList.toggle("meter__fill--caution", tone === "caution");
    fill.classList.toggle("meter__fill--warn", tone === "warn");
    if (usedPct !== null) tiles.free.meter.setAttribute("aria-valuenow", String(Math.round(usedPct)));

    const note = $("disk-note");
    if (tone === "warn") {
      setNote(note, "warn", `Only ${bytesText(free)} free. A CUDA build of torch needs about 3 GB to download and more on disk.`);
    } else if (tone === "caution") {
      setNote(note, "caution", `${bytesText(free)} free. Large packages such as torch with CUDA need several GB.`);
    } else {
      setNote(note, null, "");
    }
  }

  function renderInstalled(installed) {
    const items = Array.isArray(installed) ? installed : [];
    const count = $("installed-count");
    setPill(count, items.length ? "accent" : "idle", items.length === 1 ? "1 package" : `${items.length} packages`);
    count.hidden = false;
    $("installed-table").hidden = items.length === 0;
    $("installed-empty").hidden = items.length !== 0;
    $("installed-rows").replaceChildren(
      ...items.map((item) => {
        const row = el("tr");
        const name = el("th", "mono");
        name.scope = "row";
        name.textContent = String(item.name || "");
        row.append(name, el("td", "num", String(item.version || DASH)));
        return row;
      }),
    );
  }

  function renderState(data) {
    state = data;
    const summary = [data.python && `Python ${data.python}`];
    if (isNum(data.constraints_count)) summary.push(`${data.constraints_count} image packages pinned`);
    $("deps-summary").textContent = summary.filter(Boolean).join(" · ");

    // Keep the user's unsaved edits; otherwise show what the runner has.
    const requirements = typeof data.requirements === "string" ? data.requirements : "";
    const wasDirty = isDirty();
    savedText = requirements;
    if (!wasDirty && editor.value !== requirements) editor.value = requirements;

    renderJob();
    renderSizes(data.sizes || {});
    renderInstalled(data.installed);
    updateControls();
  }

  // Option names (--index-url, -r) in monospace: in the body font their two hyphens can read as
  // one dash, and they are exactly what has to be typed.
  function appendMessage(parent, message) {
    for (const part of String(message).split(/(\s+)/)) {
      if (/^--?[A-Za-z][A-Za-z0-9-]*$/.test(part)) parent.append(el("code", "mono", part));
      else if (part) parent.append(part);
    }
  }

  function showErrors(errors) {
    const box = $("req-errors");
    const list = $("req-error-list");
    if (!errors || !errors.length) {
      box.hidden = true;
      list.replaceChildren();
      editor.removeAttribute("aria-invalid");
      editor.setAttribute("aria-describedby", "req-help");
      return;
    }
    list.replaceChildren(
      ...errors.map((error) => {
        const item = el("li");
        if (isNum(error.line)) item.append(el("span", "mono strong", `Line ${error.line}: `));
        appendMessage(item, error.message || "invalid");
        return item;
      }),
    );
    box.hidden = false;
    editor.setAttribute("aria-invalid", "true");
    editor.setAttribute("aria-describedby", "req-errors req-help");
  }

  function showAction(tone, message) {
    setNote($("action-note"), tone, message);
  }

  // ---- log ----------------------------------------------------------------------------

  function resetLog(jobId) {
    logJobId = jobId;
    logOffset = 0;
    logLength = 0;
    logPane.replaceChildren();
  }

  function appendLog(text) {
    if (!text) return;
    // Follow the output only when the reader has not scrolled up.
    const atBottom = logPane.scrollHeight - logPane.scrollTop - logPane.clientHeight < 24;
    logLength += text.length;
    if (logLength > LOG_MAX_CHARS) {
      const kept = (logPane.textContent + text).slice(-Math.floor(LOG_MAX_CHARS * 0.8));
      logPane.textContent = kept;
      logLength = kept.length;
    } else {
      logPane.append(text);
    }
    if (atBottom) logPane.scrollTop = logPane.scrollHeight;
  }

  // Reads the log from logOffset. Resolves to the runner's "running" flag, or null on failure.
  async function pollLog() {
    let running = null;
    for (let chunk = 0; chunk < LOG_CHUNKS_PER_POLL; chunk += 1) {
      const result = await api("GET", `${API}/log?offset=${logOffset}`);
      if (result.status !== 200) {
        handleConnection(result);
        return null;
      }
      const data = result.data;
      if (data.job_id !== logJobId) {
        // Another job started (for example from a second tablet): start over from its beginning.
        resetLog(data.job_id);
        if (data.offset !== 0) continue;
      } else if (data.reset) {
        resetLog(data.job_id);
      }
      appendLog(typeof data.text === "string" ? data.text : "");
      const next = isNum(data.next_offset) ? data.next_offset : logOffset;
      const advanced = next > logOffset;
      logOffset = next;
      running = Boolean(data.running);
      if (!advanced || data.text.length === 0) break;
    }
    return running;
  }

  // ---- polling --------------------------------------------------------------------------

  let timer = 0;
  let ticking = false;

  function schedule(ms) {
    window.clearTimeout(timer);
    if (!document.hidden) timer = window.setTimeout(tick, ms);
  }

  async function refreshState() {
    const result = await api("GET", API);
    if (result.status !== 200) {
      if (!handleConnection(result)) setConnection("unavailable", result.data.error || `HTTP ${result.status}`);
      return false;
    }
    try {
      renderState(result.data);
    } catch (error) {
      console.error("rendering /api/dependencies failed", error);
      setConnection("unavailable", "The page could not display the runner's answer.");
      return false;
    }
    stateLoadedAt = Date.now();
    setConnection("ok");
    const jobId = state.job ? state.job.id : null;
    if (jobId !== logJobId) resetLog(jobId);
    return true;
  }

  async function tick() {
    if (ticking) return;
    ticking = true;
    window.clearTimeout(timer);
    try {
      if (connection === "ok" && isRunning()) {
        const running = await pollLog();
        if (running === false) {
          await refreshState();
          await pollLog(); // the last lines written after the previous poll
        } else if (running && Date.now() - stateLoadedAt >= STATE_POLL_MS) {
          // The job's step ("Running pip"), a cancel from another tab and the sizes live in
          // the state, not in the log.
          await refreshState();
        } else if (running) {
          renderJob(); // duration
        }
      } else {
        if (await refreshState()) await pollLog();
      }
    } finally {
      ticking = false;
      schedule(connection === "ok" && isRunning() ? LOG_POLL_MS : STATE_POLL_MS);
    }
  }

  // ---- actions --------------------------------------------------------------------------

  async function withPending(fn) {
    pending = true;
    updateControls();
    try {
      return await fn();
    } finally {
      pending = false;
      updateControls();
    }
  }

  async function save() {
    const text = editor.value;
    const result = await api("PUT", `${API}/requirements`, { text });
    if (result.status === 200) {
      savedText = typeof result.data.requirements === "string" ? result.data.requirements : text;
      // The runner normalises line endings and adds a final newline; adopt that text only
      // when the user has not typed in the meantime.
      if (editor.value === text && editor.value !== savedText) editor.value = savedText;
      showErrors([]);
      return true;
    }
    if (handleConnection(result)) return false;
    if (result.status === 400 && Array.isArray(result.data.errors)) {
      showErrors(result.data.errors);
      return false;
    }
    showAction("warn", result.data.error || `Saving failed (HTTP ${result.status}).`);
    return false;
  }

  async function startJob(action) {
    if (
      action === "reset" &&
      !window.confirm(
        "Reset & reinstall deletes every custom package and installs the requirements again from scratch. Continue?",
      )
    ) {
      return;
    }
    await withPending(async () => {
      showAction(null, "");
      if (isDirty() && !(await save())) return;
      const result = await api("POST", `${API}/jobs`, { action });
      if (result.status === 202 && result.data.job) {
        state.job = result.data.job;
        resetLog(result.data.job.id);
        renderJob();
        return;
      }
      if (handleConnection(result)) return;
      showAction("warn", result.data.error ? capitalize(result.data.error) : `Starting the job failed (HTTP ${result.status}).`);
      await refreshState();
    });
    tick();
  }

  async function cancelJob() {
    await withPending(async () => {
      const result = await api("POST", `${API}/cancel`, {});
      if (result.status === 200 && result.data.job) {
        state.job = result.data.job;
        renderJob();
        return;
      }
      if (handleConnection(result)) return;
      showAction("warn", result.data.error ? capitalize(result.data.error) : `Cancelling failed (HTTP ${result.status}).`);
    });
    tick();
  }

  async function clearCache() {
    await withPending(async () => {
      showAction(null, "");
      const result = await api("POST", `${API}/cache/clear`, {}, CACHE_CLEAR_TIMEOUT_MS);
      if (result.status === 200) {
        showAction("info", "Download cache cleared.");
        return;
      }
      if (handleConnection(result)) return;
      showAction("warn", result.data.error ? capitalize(result.data.error) : `Clearing the cache failed (HTTP ${result.status}).`);
    });
    // The runner re-measures in the background; show the new size shortly after.
    window.setTimeout(tick, 1500);
    tick();
  }

  buttons.install.addEventListener("click", () => startJob("install"));
  buttons.reset.addEventListener("click", () => startJob("reset"));
  buttons.cancel.addEventListener("click", cancelJob);
  buttons.clearCache.addEventListener("click", clearCache);
  buttons.save.addEventListener("click", () => withPending(save));

  editor.addEventListener("input", updateControls);
  editor.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      if (!buttons.save.disabled) buttons.save.click();
    }
  });

  window.addEventListener("beforeunload", (event) => {
    if (isDirty()) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  // Refresh at once when a backgrounded tablet tab becomes visible again.
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) window.clearTimeout(timer);
    else tick();
  });

  tick();
})();
