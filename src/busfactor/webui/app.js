// busfactor web dashboard — front-end controller.
//
// Modern web-platform features in play:
//  • Custom elements (<s7-status>, <s7-card>) for self-updating UI pieces
//  • EventSource / Server-Sent Events for the live telemetry feed
//  • The View Transitions API for buttery value swaps
//  • Native <dialog>.showModal() for the write/confirm flow
//  • Optional chaining, top-level await, structuredClone, Intl, etc.

const $ = (sel, root = document) => root.querySelector(sel);

const api = {
  async state() {
    const r = await fetch("/api/state");
    if (!r.ok) throw new Error(`state ${r.status}`);
    return r.json();
  },
  async control(action, extra = {}) {
    return postJSON("/api/control", { action, ...extra });
  },
  async write(payload) {
    return postJSON("/api/write", payload);
  },
};

async function postJSON(url, body) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `${r.status}`);
  return data;
}

// Run a DOM mutation inside a View Transition when supported.
function transition(fn) {
  if (document.startViewTransition) document.startViewTransition(fn);
  else fn();
}

/* ----------------------------------------------------------- custom elements */

class S7Status extends HTMLElement {
  connectedCallback() {
    this.innerHTML = `<span class="dot"></span><span class="state">…</span><span class="addr"></span>`;
  }
  set state(value) {
    this.dataset.state = value;
    $(".state", this).textContent =
      { connected: "Connected", connecting: "Connecting", error: "Error", disconnected: "Offline" }[value] || value;
  }
  set address(value) {
    $(".addr", this).textContent = value ? `· ${value}` : "";
  }
}
customElements.define("s7-status", S7Status);

class S7Card extends HTMLElement {
  connectedCallback() {
    if (this._built) return;
    this._built = true;
    this.innerHTML = `
      <div class="row"><span class="label"></span><span class="chip"></span></div>
      <div class="value">—</div>
      <div class="meta"><span class="addr"></span><span class="raw"></span></div>`;
    this.addEventListener("click", () => {
      if (this.dataset.readonly === "true") {
        toast("Writes are disabled — cycle write mode (W)", "warn");
        return;
      }
      openEditor(this._reading);
    });
  }

  describe(v) {
    this._reading = v;
    this.dataset.type = v.type;
    $(".label", this).textContent = v.label;
    $(".chip", this).textContent = v.area;
    $(".addr", this).textContent = `${v.spec}` + (v.bit != null ? `.${v.bit}` : "");
  }

  update(r) {
    this._reading = { ...this._reading, ...r };
    const valEl = $(".value", this);
    const changed = r.changed || valEl.textContent !== r.value;
    valEl.textContent = r.value;
    $(".raw", this).textContent = r.raw_hex || "";
    this.dataset.value = r.value;
    this.dataset.error = String(Boolean(r.error));
    if (r.changed) {
      this.dataset.changed = "true";
      this.classList.remove("just"); // restart animation
      void this.offsetWidth;
      setTimeout(() => (this.dataset.changed = "false"), 950);
    }
  }

  setReadonly(ro) {
    this.dataset.readonly = String(ro);
  }
}
customElements.define("s7-card", S7Card);

/* ----------------------------------------------------------------- app state */

const els = {
  status: $("#status"),
  cards: $("#cards"),
  hex: $("#hex"),
  hexLabel: $("#hex-label"),
  log: $("#log"),
  polls: $("#metric-polls b"),
  extra: $("#metric-extra"),
  vars: $("#metric-vars b"),
  interval: $("#metric-interval b"),
  pause: $("#btn-pause"),
  reconnect: $("#btn-reconnect"),
  writemode: $("#btn-writemode"),
  cmd: $("#cmd"),
  dialog: $("#edit-dialog"),
};

const cards = new Map(); // spec -> <s7-card>
let writeMode = "disabled";

const WRITE_MODE_UI = {
  disabled: { ico: "🔒", lbl: "read-only", cls: "mode-disabled" },
  confirm: { ico: "✓", lbl: "confirm", cls: "mode-confirm" },
  allowed: { ico: "✎", lbl: "write", cls: "mode-allowed" },
};

function setWriteMode(mode) {
  writeMode = mode;
  const ui = WRITE_MODE_UI[mode] || WRITE_MODE_UI.disabled;
  els.writemode.className = `ctl ${ui.cls}`;
  $(".ico", els.writemode).textContent = ui.ico;
  $(".lbl", els.writemode).textContent = ui.lbl;
  for (const card of cards.values()) card.setReadonly(mode === "disabled");
}

function setPaused(paused) {
  els.pause.dataset.on = String(paused);
  $(".ico", els.pause).textContent = paused ? "▶" : "⏸";
  $(".lbl", els.pause).textContent = paused ? "Resume" : "Pause";
}

/* --------------------------------------------------------------------- build */

async function boot() {
  let meta;
  try {
    meta = await api.state();
  } catch (e) {
    log("Failed to load state", "err");
    return;
  }
  els.status.address = meta.address;
  els.vars.textContent = meta.variables.length;
  els.interval.textContent = `${meta.poll_interval}s`;
  setWriteMode(meta.write_mode);

  els.cards.replaceChildren();
  cards.clear();
  for (const v of meta.variables) {
    const card = document.createElement("s7-card");
    els.cards.append(card);
    card.describe(v);
    card.setReadonly(meta.write_mode === "disabled");
    cards.set(v.spec, card);
  }

  connectStream();
  wireControls();
  log("Dashboard ready", "ok");
}

/* ----------------------------------------------------------------- live feed */

function connectStream() {
  const source = new EventSource("/api/stream");
  source.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    applySnapshot(msg);
  };
  source.onerror = () => els.status.state = "connecting";
}

function applySnapshot(s) {
  els.status.state = s.connection_state;
  setPaused(s.paused);
  if (s.write_mode && s.write_mode !== writeMode) setWriteMode(s.write_mode);
  els.polls.textContent = s.poll_count;

  if (s.status_extra) {
    const parts = Object.entries(s.status_extra).map(([k, v]) => `${k}: ${v}`);
    els.extra.textContent = parts.join("  ");
  }

  if (s.error) log(s.error, "err");

  if (s.groups?.length) {
    els.hexLabel.textContent = s.groups.map((g) => g.label).join(" · ");
    els.hex.textContent = s.groups
      .map((g) => `  ─── ${g.label} ───\n${g.hex_dump}`)
      .join("\n\n");
  }

  if (s.readings?.length) {
    transition(() => {
      for (const r of s.readings) cards.get(r.spec)?.update(r);
    });
    for (const r of s.readings) {
      if (r.changed) log(`${r.label} → ${r.value}`, "ok");
    }
  }
}

/* ------------------------------------------------------------------ controls */

function wireControls() {
  els.pause.addEventListener("click", async () => {
    const action = els.pause.dataset.on === "true" ? "resume" : "pause";
    try { await api.control(action); } catch (e) { toast(e.message, "err"); }
  });

  els.reconnect.addEventListener("click", async () => {
    log("Reconnecting…", "warn");
    try {
      await api.control("reconnect");
      toast("Reconnected", "ok");
    } catch (e) {
      toast(`Reconnect failed: ${e.message}`, "err");
    }
  });

  els.writemode.addEventListener("click", async () => {
    try {
      const r = await api.control("write_mode");
      setWriteMode(r.status.write_mode);
      toast(`Write mode: ${r.status.write_mode}`, r.status.write_mode === "allowed" ? "ok" : "warn");
    } catch (e) { toast(e.message, "err"); }
  });

  els.cmd.addEventListener("keydown", (e) => {
    if (e.key === "Enter") runCommand(els.cmd.value.trim());
  });

  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea") || els.dialog.open) return;
    if (e.key === " ") { e.preventDefault(); els.pause.click(); }
    else if (e.key.toLowerCase() === "c") els.reconnect.click();
    else if (e.key.toLowerCase() === "w") els.writemode.click();
    else if (e.key === "/") { e.preventDefault(); els.cmd.focus(); }
  });
}

async function runCommand(text) {
  if (!text) return;
  const parts = text.split(/\s+/);
  const cmd = parts[0].toLowerCase();
  try {
    if (cmd === "set" && parts.length >= 3) {
      await api.write({ spec: parts[1], value: parts.slice(2).join(" ") });
      toast(`set ${parts[1]}`, "ok");
    } else if (cmd === "write" && parts.length >= 4) {
      await api.write({ raw: { db: parts[1], offset: parts[2], bytes: parts.slice(3).join(" ") } });
      toast(`write DB${parts[1]}`, "ok");
    } else {
      toast("Usage: set <spec> <value>  |  write <db> <offset> <hex…>", "warn");
      return;
    }
    els.cmd.value = "";
  } catch (e) {
    toast(e.message, "err");
    log(`Command failed: ${e.message}`, "err");
  }
}

/* -------------------------------------------------------------- write dialog */

function openEditor(reading) {
  $("#edit-title").textContent = `Write · ${reading.label}`;
  $("#edit-meta").textContent =
    `${reading.spec}` + (reading.bit != null ? `.${reading.bit}` : "") +
    `  ·  ${reading.type}  ·  now: ${reading.value}`;
  const input = $("#edit-input");
  input.value = reading.value === "—" ? "" : reading.value;
  $("#edit-hint").textContent = hintFor(reading.type) +
    (writeMode === "confirm" ? "  ·  confirm mode" : "");

  els.dialog.returnValue = "";
  els.dialog.showModal();
  input.focus();
  input.select();

  els.dialog.onclose = async () => {
    if (els.dialog.returnValue !== "confirm") return;
    const value = input.value.trim();
    try {
      const r = await api.write({ spec: reading.spec, value });
      toast(`✓ ${r.result.description} [${r.result.bytes_hex}]`, "ok");
      log(`${r.result.description} [${r.result.bytes_hex}]`, "ok");
    } catch (e) {
      toast(`Write failed: ${e.message}`, "err");
      log(`Write failed: ${e.message}`, "err");
    }
  };
}

function hintFor(type) {
  if (type === "Bit") return "Values: 0/1, true/false, on/off";
  if (["Byte", "Word", "DWord"].includes(type)) return "Decimal or hex (0xFF)";
  if (type === "Real") return "Floating point, e.g. 3.14";
  return "Enter a value";
}

/* --------------------------------------------------------------- log + toast */

function log(text, kind = "") {
  const li = document.createElement("li");
  const time = new Date().toLocaleTimeString([], { hour12: false });
  li.innerHTML = `<time>${time}</time><span class="${kind}">${escapeHTML(text)}</span>`;
  els.log.prepend(li);
  while (els.log.children.length > 80) els.log.lastElementChild.remove();
}

function toast(text, kind = "") {
  const div = document.createElement("div");
  div.className = `toast ${kind}`;
  div.textContent = text;
  $("#toasts").append(div);
  setTimeout(() => div.remove(), 3600);
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

boot();
