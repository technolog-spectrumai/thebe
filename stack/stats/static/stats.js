// Statistics page: polls /api/stats every 3 s and updates the page in place.
// Data only ever reaches the DOM through textContent, createElement and CSSOM properties
// (never innerHTML), so nothing in the API response can inject markup.
(() => {
  "use strict";

  const POLL_MS = 3000;
  const REQUEST_TIMEOUT_MS = 10000;
  const DASH = "—";
  // Meter colours: caution from 75 %, warn from 90 %.
  const USAGE_LEVELS = { caution: 75, warn: 90 };
  const CPU_TEMP_LEVELS = { caution: 85, warn: 95 };
  const GPU_TEMP_LEVELS = { caution: 80, warn: 90 };
  const KERNEL_STATE_TONES = {
    idle: "ok",
    busy: "caution",
    starting: "idle",
    restarting: "caution",
    autorestarting: "caution",
    dead: "warn",
  };
  const KIND_LABELS = { physical: "Physical", tailscale: "Tailnet", virtual: "Virtual" };

  const $ = (id) => document.getElementById(id);
  const isNum = (value) => typeof value === "number" && Number.isFinite(value);
  const pick = (object, path) =>
    path.split(".").reduce((node, key) => (node == null ? undefined : node[key]), object);

  // ---- formatting -------------------------------------------------------------------

  const BYTE_UNITS = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];

  // [number, unit] with 3 significant figures: "8.21" GiB, "15.3" GiB, "476" GiB.
  function bytesParts(bytes) {
    if (!isNum(bytes)) return null;
    let value = Math.abs(bytes);
    let unit = 0;
    while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
      value /= 1024;
      unit += 1;
    }
    const digits = unit === 0 || value >= 100 ? 0 : value >= 10 ? 1 : 2;
    return [(Math.sign(bytes) * value).toFixed(digits), BYTE_UNITS[unit]];
  }

  const joinParts = (parts) => (parts ? `${parts[0]} ${parts[1]}` : DASH);

  // "8.2 / 15.3 GiB": both values in the unit of the larger one.
  function bytesPair(used, total) {
    if (!isNum(used) || !isNum(total)) return DASH;
    const [totalText, unit] = bytesParts(total);
    const factor = 1024 ** BYTE_UNITS.indexOf(unit);
    const digits = unit === "B" ? 0 : 1;
    return `${(used / factor).toFixed(digits)} / ${Number(totalText).toFixed(digits)} ${unit}`;
  }

  function formatDuration(seconds) {
    if (!isNum(seconds)) return DASH;
    const s = Math.max(0, Math.floor(seconds));
    const days = Math.floor(s / 86400);
    const hours = Math.floor((s % 86400) / 3600);
    const minutes = Math.floor((s % 3600) / 60);
    if (days) return `${days} d ${hours} h`;
    if (hours) return `${hours} h ${minutes} min`;
    if (minutes) return `${minutes} min`;
    return `${s} s`;
  }

  function formatAgo(seconds) {
    if (!isNum(seconds)) return DASH;
    const s = Math.max(0, Math.round(seconds));
    if (s < 5) return "just now";
    if (s < 60) return `${s} s ago`;
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    return `${Math.floor(s / 86400)} d ago`;
  }

  function parseTime(iso) {
    const ms = typeof iso === "string" ? Date.parse(iso) : NaN;
    return Number.isNaN(ms) ? null : ms;
  }

  function formatClock(iso) {
    const ms = parseTime(iso);
    return ms === null ? "" : new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function formatDateTime(iso) {
    const ms = parseTime(iso);
    return ms === null
      ? DASH
      : new Date(ms).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  // Formatters for [data-format]: a string, a [number, unit] pair, or null for "—".
  const FORMATS = {
    text: (v) => (v == null || v === "" ? null : String(v)),
    integer: (v) => (isNum(v) ? String(Math.round(v)) : null),
    percent: (v) => (isNum(v) ? [v >= 100 ? "100" : v.toFixed(1), "%"] : null),
    bytes: (v) => bytesParts(v),
    rate: (v) => {
      const parts = bytesParts(v);
      return parts && [parts[0], `${parts[1]}/s`];
    },
    celsius: (v) => (isNum(v) ? [v.toFixed(0), "°C"] : null),
    load: (v) => (isNum(v) ? v.toFixed(2) : null),
  };

  // ---- DOM helpers ------------------------------------------------------------------

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function writeValue(node, formatted) {
    if (formatted == null) {
      node.textContent = DASH;
    } else if (Array.isArray(formatted)) {
      node.replaceChildren(formatted[0], el("span", "unit", formatted[1]));
    } else {
      node.textContent = formatted;
    }
  }

  function levelFor(value, levels) {
    if (!isNum(value)) return "";
    if (value >= levels.warn) return "warn";
    if (value >= levels.caution) return "caution";
    return "";
  }

  const worstLevel = (...levels) => (levels.includes("warn") ? "warn" : levels.includes("caution") ? "caution" : "");

  function setMeter(meter, value, levels = USAGE_LEVELS) {
    const fill = meter.querySelector(".meter__fill");
    const pct = isNum(value) ? Math.min(100, Math.max(0, value)) : 0;
    // CSSOM property writes are allowed by the page's CSP; style="" attributes are not.
    fill.style.width = `${pct}%`;
    const level = levelFor(value, levels);
    fill.classList.toggle("meter__fill--caution", level === "caution");
    fill.classList.toggle("meter__fill--warn", level === "warn");
    if (isNum(value)) meter.setAttribute("aria-valuenow", String(Math.round(pct)));
    else meter.removeAttribute("aria-valuenow");
  }

  function makeMeter(label, small) {
    const meter = el("div", small ? "meter meter--sm" : "meter");
    meter.setAttribute("role", "meter");
    meter.setAttribute("aria-label", label);
    meter.setAttribute("aria-valuemin", "0");
    meter.setAttribute("aria-valuemax", "100");
    meter.append(el("div", "meter__fill"));
    return meter;
  }

  function setPill(pill, tone, text) {
    pill.className = `pill pill--${tone}`;
    pill.textContent = text;
  }

  function pill(tone, text, small = true) {
    const node = el("span", `pill pill--${tone}${small ? " pill--sm" : ""}`, text);
    return node;
  }

  function numCell(text, extraClass) {
    return el("td", extraClass ? `num ${extraClass}` : "num", text);
  }

  // ---- generic bindings -----------------------------------------------------------------

  function applyBindings(data) {
    for (const node of document.querySelectorAll("[data-bind]")) {
      const format = FORMATS[node.dataset.format || "text"] || FORMATS.text;
      writeValue(node, format(pick(data, node.dataset.bind)));
    }
    for (const meter of document.querySelectorAll("[data-meter]")) {
      setMeter(meter, pick(data, meter.dataset.meter));
    }
    for (const note of document.querySelectorAll("[data-error]")) {
      const message = pick(data, note.dataset.error);
      note.hidden = !message;
      const text = note.querySelector(".note__text");
      if (text) text.textContent = message || "";
    }
  }

  // ---- tiles ------------------------------------------------------------------------

  function tile(id) {
    const root = $(id);
    const part = (name) => root.querySelector(`[data-part="${name}"]`);
    return {
      value: part("value"),
      unit: part("unit"),
      rx: part("rx"),
      rxUnit: part("rx-unit"),
      tx: part("tx"),
      txUnit: part("tx-unit"),
      meta: part("meta"),
      meter: root.querySelector(".meter"),
      icon: root.querySelector(".tile__icon"),
    };
  }

  const tiles = {
    cpu: tile("tile-cpu"),
    memory: tile("tile-memory"),
    disk: tile("tile-disk"),
    uptime: tile("tile-uptime"),
    gpu: tile("tile-gpu"),
    kernels: tile("tile-kernels"),
    network: tile("tile-network"),
  };

  function setTile(t, { value = null, unit = "", meta = "", percent, levels = USAGE_LEVELS, tone = "" }) {
    if (t.value) {
      t.value.textContent = value == null ? DASH : String(value);
      t.unit.textContent = value == null ? "" : unit;
    }
    t.meta.textContent = meta || " ";
    if (t.meter) setMeter(t.meter, percent, levels);
    t.icon.classList.toggle("tile__icon--caution", tone === "caution");
    t.icon.classList.toggle("tile__icon--warn", tone === "warn");
  }

  function uptimeParts(seconds) {
    if (!isNum(seconds)) return [null, ""];
    if (seconds < 3600) return [Math.floor(seconds / 60), "min"];
    if (seconds < 48 * 3600) return [Math.floor(seconds / 3600), "h"];
    return [Math.floor(seconds / 86400), "d"];
  }

  function renderTiles(data) {
    const host = data.host || {};
    const cpu = data.cpu || {};
    const memory = data.memory || {};
    const disk = data.disk || {};
    const network = data.network || {};
    const kernels = data.kernels || {};
    const gpu = data.gpu || {};

    setTile(tiles.cpu, {
      value: isNum(cpu.percent) ? Math.round(cpu.percent) : null,
      unit: "%",
      percent: cpu.percent,
      meta: [
        isNum(cpu.temperature_c) && `${Math.round(cpu.temperature_c)} °C`,
        isNum(cpu.cores) && `${cpu.cores} threads`,
      ]
        .filter(Boolean)
        .join(" · "),
      tone: worstLevel(levelFor(cpu.percent, USAGE_LEVELS), levelFor(cpu.temperature_c, CPU_TEMP_LEVELS)),
    });

    setTile(tiles.memory, {
      value: isNum(memory.percent) ? Math.round(memory.percent) : null,
      unit: "%",
      percent: memory.percent,
      meta: isNum(memory.total) ? bytesPair(memory.used, memory.total) : memory.error ? "unavailable" : "",
      tone: levelFor(memory.percent, USAGE_LEVELS),
    });

    setTile(tiles.disk, {
      value: isNum(disk.percent) ? Math.round(disk.percent) : null,
      unit: "%",
      percent: disk.percent,
      meta: isNum(disk.free) ? `${joinParts(bytesParts(disk.free))} free` : disk.error ? "unavailable" : "",
      tone: levelFor(disk.percent, USAGE_LEVELS),
    });

    const [uptimeValue, uptimeUnit] = uptimeParts(host.uptime_seconds);
    setTile(tiles.uptime, {
      value: uptimeValue,
      unit: uptimeUnit,
      meta: host.boot_time ? `since ${formatDateTime(host.boot_time)}` : "",
    });

    const devices = gpu.available && Array.isArray(gpu.devices) ? gpu.devices : [];
    if (devices.length) {
      const first = devices[0];
      // "NVIDIA GeForce RTX 3050 Laptop GPU" -> "RTX 3050 Laptop" to fit a phone-width tile.
      const name = String(first.name || "GPU")
        .replace(/^NVIDIA\s+(GeForce\s+)?/i, "")
        .replace(/\s+GPU$/i, "");
      setTile(tiles.gpu, {
        value: isNum(first.utilization_percent) ? Math.round(first.utilization_percent) : null,
        unit: "%",
        percent: first.utilization_percent,
        meta: [
          isNum(first.temperature_c) && `${Math.round(first.temperature_c)} °C`,
          devices.length > 1 ? `${devices.length} GPUs` : name,
        ]
          .filter(Boolean)
          .join(" · "),
        tone: worstLevel(
          levelFor(first.utilization_percent, USAGE_LEVELS),
          levelFor(first.temperature_c, GPU_TEMP_LEVELS),
        ),
      });
    } else {
      setTile(tiles.gpu, { meta: "not visible" });
    }

    if (kernels.available) {
      const busy = isNum(kernels.busy) ? kernels.busy : 0;
      setTile(tiles.kernels, {
        value: kernels.count,
        meta: kernels.count ? `${busy} busy · ${kernels.count - busy} idle` : "none running",
        tone: busy ? "caution" : "",
      });
    } else {
      setTile(tiles.kernels, { meta: "JupyterLab unavailable", tone: "warn" });
    }

    const totals = network.totals || {};
    const rx = FORMATS.rate(totals.rx_rate);
    const tx = FORMATS.rate(totals.tx_rate);
    tiles.network.rx.textContent = rx ? rx[0] : DASH;
    tiles.network.rxUnit.textContent = rx ? rx[1] : "";
    tiles.network.tx.textContent = tx ? tx[0] : DASH;
    tiles.network.txUnit.textContent = tx ? tx[1] : "";
    const tailnet = (network.interfaces || []).find((item) => item.kind === "tailscale");
    setTile(tiles.network, {
      meta: tailnet
        ? `${tailnet.name}: ↓ ${joinParts(FORMATS.rate(tailnet.rx_rate))} · ↑ ${joinParts(FORMATS.rate(tailnet.tx_rate))}`
        : network.error
          ? "unavailable"
          : "physical interfaces",
    });
  }

  // ---- detail cards -----------------------------------------------------------------

  function renderHostSummary(data) {
    const host = data.host || {};
    const parts = [
      host.kernel_release && `Linux ${host.kernel_release}`,
      isNum(host.uptime_seconds) && `up ${formatDuration(host.uptime_seconds)}`,
    ].filter(Boolean);
    $("host-summary").textContent = parts.length ? parts.join(" · ") : host.error || DASH;
  }

  function renderCores(cpu) {
    const container = $("cpu-cores");
    const cores = Array.isArray(cpu.per_core) ? cpu.per_core : [];
    // Rows are built once per thread count and then updated, so meters animate smoothly.
    if (container.childElementCount !== cores.length) {
      container.replaceChildren(
        ...cores.map((_, index) => {
          const row = el("div", "core");
          row.setAttribute("role", "listitem");
          row.append(el("span", "core__label", String(index)), makeMeter(`Thread ${index} usage`, true), el("span", "core__value", DASH));
          return row;
        }),
      );
    }
    cores.forEach((value, index) => {
      const row = container.children[index];
      setMeter(row.children[1], value);
      row.children[2].textContent = isNum(value) ? `${Math.round(value)}%` : DASH;
    });
  }

  function renderInterfaces(network) {
    const body = $("net-rows");
    const interfaces = Array.isArray(network.interfaces) ? network.interfaces : [];
    if (!interfaces.length) {
      const cell = el("td", "table__empty", network.error ? "Interface counters are unavailable." : "No interfaces found.");
      cell.colSpan = 5;
      const row = el("tr");
      row.append(cell);
      body.replaceChildren(row);
      return;
    }
    body.replaceChildren(
      ...interfaces.map((item) => {
        const row = el("tr", item.kind === "tailscale" ? "is-highlight" : "");
        const name = el("th");
        name.scope = "row";
        const stack = el("div", "cell-stack");
        stack.append(
          el("span", "mono strong", item.name),
          pill(item.kind === "tailscale" ? "accent" : "idle", KIND_LABELS[item.kind] || item.kind),
        );
        name.append(stack);
        row.append(
          name,
          numCell(joinParts(FORMATS.rate(item.rx_rate))),
          numCell(joinParts(FORMATS.rate(item.tx_rate))),
          numCell(joinParts(FORMATS.bytes(item.rx_bytes)), "hide-sm"),
          numCell(joinParts(FORMATS.bytes(item.tx_bytes)), "hide-sm"),
        );
        return row;
      }),
    );
  }

  function renderKernels(kernels, generatedAt) {
    const statePill = $("kernels-pill");
    const error = $("kernels-error");
    const table = $("kernels-table");
    const empty = $("kernels-empty");

    if (!kernels.available) {
      setPill(statePill, "warn", "Unavailable");
      error.hidden = false;
      error.querySelector(".note__text").textContent = kernels.error || "JupyterLab did not answer.";
      table.hidden = true;
      empty.hidden = true;
      return;
    }

    error.hidden = true;
    const count = isNum(kernels.count) ? kernels.count : 0;
    setPill(statePill, kernels.busy ? "caution" : "ok", `${count} running`);
    table.hidden = count === 0;
    empty.hidden = count !== 0;

    const now = parseTime(generatedAt);
    $("kernel-rows").replaceChildren(
      ...(kernels.items || []).map((item) => {
        const row = el("tr");
        const name = el("th");
        name.scope = "row";
        name.append(el("span", "strong", item.name || "kernel"), el("span", "cell-sub", String(item.id || "").slice(0, 8)));
        const state = el("td");
        state.append(pill(KERNEL_STATE_TONES[item.execution_state] || "idle", item.execution_state || "unknown"));
        const last = parseTime(item.last_activity);
        row.append(
          name,
          state,
          numCell(isNum(item.connections) ? String(item.connections) : DASH, "hide-sm"),
          numCell(now !== null && last !== null ? formatAgo((now - last) / 1000) : DASH),
        );
        return row;
      }),
    );
  }

  function buildDevice() {
    const device = el("article", "device");
    const head = el("div", "device__head");
    head.append(el("p", "device__name"), el("span", "caption mono"));

    const usage = (label) => {
      const block = el("div", "usage");
      const header = el("div", "usage__head");
      header.append(el("span", "usage__label", label), el("span", "usage__value", DASH));
      block.append(header, makeMeter(`GPU ${label.toLowerCase()}`, false));
      return block;
    };

    const rows = el("ul", "rows");
    for (const label of ["Memory", "Temperature", "Power"]) {
      const row = el("li", "row");
      row.append(el("span", "row__key", label), el("span", "row__value", DASH));
      rows.append(row);
    }
    device.append(head, usage("Utilisation"), usage("Memory"), rows);
    return device;
  }

  function updateDevice(device, gpu) {
    const [head, utilisation, memory, rows] = device.children;
    head.children[0].textContent = gpu.name || "GPU";
    head.children[1].textContent = `#${gpu.index}`;

    writeValue(utilisation.querySelector(".usage__value"), FORMATS.percent(gpu.utilization_percent));
    setMeter(utilisation.querySelector(".meter"), gpu.utilization_percent);
    writeValue(memory.querySelector(".usage__value"), FORMATS.percent(gpu.memory_percent));
    setMeter(memory.querySelector(".meter"), gpu.memory_percent);

    const [memoryRow, temperatureRow, powerRow] = rows.children;
    memoryRow.lastChild.textContent = bytesPair(gpu.memory_used, gpu.memory_total);
    const temperature = temperatureRow.lastChild;
    temperature.textContent = isNum(gpu.temperature_c) ? `${Math.round(gpu.temperature_c)} °C` : DASH;
    const level = levelFor(gpu.temperature_c, GPU_TEMP_LEVELS);
    temperature.classList.toggle("value--caution", level === "caution");
    temperature.classList.toggle("value--warn", level === "warn");
    powerRow.lastChild.textContent = isNum(gpu.power_w)
      ? isNum(gpu.power_limit_w)
        ? `${gpu.power_w.toFixed(1)} / ${gpu.power_limit_w.toFixed(0)} W`
        : `${gpu.power_w.toFixed(1)} W`
      : "not reported";
  }

  function renderGpu(gpu) {
    const list = $("gpu-devices");
    const empty = $("gpu-empty");
    const devices = gpu.available && Array.isArray(gpu.devices) ? gpu.devices : [];
    if (!devices.length) {
      setPill($("gpu-pill"), "idle", "Not visible");
      list.hidden = true;
      list.replaceChildren();
      empty.hidden = false;
      $("gpu-reason").textContent = gpu.error || "";
      return;
    }
    setPill($("gpu-pill"), "ok", devices.length === 1 ? "1 device" : `${devices.length} devices`);
    empty.hidden = true;
    list.hidden = false;
    if (list.childElementCount !== devices.length) {
      list.replaceChildren(...devices.map(buildDevice));
    }
    devices.forEach((device, index) => updateDevice(list.children[index], device));
  }

  function render(data) {
    applyBindings(data);
    renderHostSummary(data);
    renderTiles(data);
    renderCores(data.cpu || {});
    renderInterfaces(data.network || {});
    renderKernels(data.kernels || {}, data.generated_at);
    renderGpu(data.gpu || {});
  }

  // ---- connection state and polling ---------------------------------------------------

  const jupyterUrl = document.body.dataset.jupyterUrl || "";
  if (/^https?:\/\//.test(jupyterUrl)) $("kernels-open").hidden = false;

  let lastUpdate = null;

  function setConnection(state, message) {
    const livePill = $("live-pill");
    const label = $("live-label");
    livePill.classList.remove("pill--idle", "pill--ok", "pill--warn", "pill--live");
    if (state === "live") {
      livePill.classList.add("pill--ok", "pill--live");
      label.textContent = "Live";
      $("updated-at").textContent = `Updated ${formatClock(lastUpdate)}`;
    } else {
      livePill.classList.add("pill--warn");
      label.textContent = message;
      $("updated-at").textContent = lastUpdate ? `Last update ${formatClock(lastUpdate)}` : "";
    }
    $("dashboard").classList.toggle("is-stale", state !== "live");
  }

  let timer = 0;
  let inFlight = false;

  async function poll() {
    if (inFlight) return;
    inFlight = true;
    window.clearTimeout(timer);
    const controller = new AbortController();
    const abortTimer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      let data;
      try {
        const response = await fetch("/api/stats", {
          cache: "no-store",
          credentials: "same-origin",
          headers: { Accept: "application/json" },
          signal: controller.signal,
        });
        if (response.status === 401) {
          setConnection("lost", "Sign-in required: reload the page");
          return;
        }
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        data = await response.json();
      } catch (error) {
        setConnection("lost", "Connection lost");
        return;
      }
      try {
        render(data);
        lastUpdate = data.generated_at;
        setConnection("live");
      } catch (error) {
        console.error("rendering /api/stats failed", error);
        setConnection("lost", "Display error");
      }
    } finally {
      window.clearTimeout(abortTimer);
      inFlight = false;
      timer = window.setTimeout(poll, POLL_MS);
    }
  }

  // Refresh at once when a backgrounded tablet tab becomes visible again.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) poll();
  });

  poll();
})();
