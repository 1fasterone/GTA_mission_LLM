"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const api = (path, opts) => fetch(path, opts).then((r) => r.json());

// ── Sidebar navigation ────────────────────────────────────────────────────────
function showSection(name) {
  $$(".panel").forEach((p) => p.classList.toggle("is-active", p.dataset.section === name));
  refreshNavActive();
}
// A nav is active when its section is showing AND (if it carries a pipeline) the
// active pipeline matches — so "New Unity Project" and "GTA5" don't both light up.
function refreshNavActive() {
  const active = document.querySelector(".panel.is-active")?.dataset.section || "prompt";
  $$(".nav").forEach((n) => {
    let on = n.dataset.section === active;
    if (on && n.dataset.pipeline) on = n.dataset.pipeline === PIPELINE;
    if (on && n.dataset.cat) on = n.dataset.cat === CLI_CAT;
    n.classList.toggle("is-active", on);
  });
}
$$(".nav").forEach((n) => n.addEventListener("click", () => {
  if (n.dataset.pipeline) setPipeline(n.dataset.pipeline);
  if (n.dataset.cat) setCliCategory(n.dataset.cat);
  if (n.dataset.section) showSection(n.dataset.section);
}));

// ── Tooltips ──────────────────────────────────────────────────────────────────
const tooltip = $("#tooltip");
document.addEventListener("mouseover", (e) => {
  const el = e.target.closest("[data-tip]");
  if (!el) return;
  tooltip.textContent = el.dataset.tip;
  tooltip.hidden = false;
  const r = el.getBoundingClientRect();
  tooltip.style.left = Math.min(r.left, window.innerWidth - 280) + "px";
  tooltip.style.top = r.bottom + 8 + "px";
});
document.addEventListener("mouseout", (e) => {
  if (e.target.closest("[data-tip]")) tooltip.hidden = true;
});

// ── Status lights ─────────────────────────────────────────────────────────────
function setLight(el, state) { if (el) el.className = "light light--" + state; }

function applyStatus(status) {
  const chatState = status.chat.ok ? "ok" : "err";
  setLight($("#pillChat").querySelector(".light"), chatState);
  setLight($("#lightChat"), chatState);
  setLight($("#lightInlineChat"), chatState);

  const rag = status.rag;
  let overall;
  if (rag.basic_mode) overall = "basic";
  else if (rag.ok) overall = "ok";
  else if (!rag.error && (rag.gta?.present || rag.unity?.present)) overall = "warn";
  else overall = "err";
  setLight($("#pillRag").querySelector(".light"), overall);
  setLight($("#lightRag"), overall);
  setLight($("#lightInlineRag"), overall);
  $("#pillRag").querySelector(".pill-label").textContent =
    rag.basic_mode ? "Basic" : "RAG";

  // Per-workspace lights + doc counts
  const wsRow = (kind) => document.querySelector(`.workspace-row[data-ws="${kind}"]`);
  const setWsLabel = (kind, sub, basic) => {
    const row = wsRow(kind);
    if (!row) return;
    const labelEl = row.querySelector(".ws-label");
    const base = kind === "gta" ? "GTA workspace" : "Unity workspace";
    if (basic) {
      labelEl.textContent = `${base} (Basic Mode)`;
    } else if (!sub?.present) {
      labelEl.textContent = `${base} — missing`;
    } else {
      const n = sub.doc_count ?? 0;
      labelEl.textContent = `${base} (${n} doc${n === 1 ? "" : "s"})`;
    }
  };

  if (rag.basic_mode) {
    setLight($("#lightWsGta"), "basic");
    setLight($("#lightWsUnity"), "basic");
    setWsLabel("gta",   null, true);
    setWsLabel("unity", null, true);
  } else {
    setLight($("#lightWsGta"),
      rag.gta?.present ? (rag.gta?.doc_count ? "ok" : "warn") : "err");
    setLight($("#lightWsUnity"),
      rag.unity?.present ? (rag.unity?.doc_count ? "ok" : "warn") : "err");
    setWsLabel("gta",   rag.gta,   false);
    setWsLabel("unity", rag.unity, false);
  }
}

async function refreshStatus() {
  try { applyStatus(await api("/api/status")); } catch (_) { /* server starting */ }
}

// ── Config load / save ────────────────────────────────────────────────────────
let CURRENT_CFG = null;

function showBackendBlocks(backend) {
  $$(".backend-block").forEach((b) => { b.hidden = b.dataset.backend !== backend; });
}

async function loadConfig() {
  const cfg = await api("/api/config");
  CURRENT_CFG = cfg;
  const lm = cfg.lm_studio || {};
  const ol = cfg.ollama || {};
  const rag = cfg.anythingllm || {};

  // Backends page
  $("#cfgBackend").value = lm.backend || "lm_studio";
  $("#cfgLmUrl").value = lm.base_url || "";
  $("#cfgLmModel").value = lm.model || "";
  $("#cfgOlUrl").value = ol.base_url || "";
  $("#cfgOlModel").value = ol.model || "";
  $("#cfgRagUrl").value = rag.base_url || "";
  $("#cfgRagSlug").value = rag.workspace_slug || "";
  $("#cfgRagSlugUnity").value = rag.unity_workspace_slug || "";
  $("#cfgRagKey").placeholder = rag.api_key_set
    ? "•••• stored — leave blank to keep"
    : "no key set";
  $("#cfgBasicMode").checked = !!cfg.basic_mode;

  // LLM Settings page — map ranges to real config values
  const conceptT = lm.concept_temperature ?? 0.85;
  const schemaT  = lm.schema_temperature ?? 0.2;
  const maxTok   = lm.max_tokens ?? 8192;
  setSlider("Creativity", Math.round(((conceptT - 0.2) / 1.0) * 100));
  setSlider("Complexity", Math.round((1 - schemaT) * 100));
  setSlider("Detail",     Math.round(((maxTok - 4096) / (16384 - 4096)) * 100));

  showBackendBlocks($("#cfgBackend").value);
}

$("#cfgBackend").addEventListener("change", (e) => showBackendBlocks(e.target.value));

async function saveConfigPatch(patch, noteEl) {
  if (noteEl) noteEl.textContent = "Saving…";
  await api("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (noteEl) {
    noteEl.textContent = "Saved ✓";
    setTimeout(() => (noteEl.textContent = ""), 2500);
  }
  await loadConfig();
  await refreshStatus();
}

// Save Backends page
$("#btnSaveBackends").addEventListener("click", async () => {
  const patch = {
    lm_studio: {
      backend:  $("#cfgBackend").value,
      base_url: $("#cfgLmUrl").value.trim(),
      model:    $("#cfgLmModel").value.trim(),
    },
    ollama: {
      base_url: $("#cfgOlUrl").value.trim(),
      model:    $("#cfgOlModel").value.trim(),
    },
    anythingllm: {
      base_url:             $("#cfgRagUrl").value.trim(),
      workspace_slug:       $("#cfgRagSlug").value.trim(),
      unity_workspace_slug: $("#cfgRagSlugUnity").value.trim(),
    },
    basic_mode: $("#cfgBasicMode").checked,
  };
  const key = $("#cfgRagKey").value.trim();
  if (key) patch.anythingllm.api_key = key;
  await saveConfigPatch(patch, $("#saveBackendsNote"));
  $("#cfgRagKey").value = "";
});

// Save LLM Settings page — slider values back-mapped to config keys
$("#btnSaveLlm").addEventListener("click", async () => {
  const creativity = readSlider("Creativity") / 100;   // 0..1
  const complexity = readSlider("Complexity") / 100;   // 0..1
  const detail     = readSlider("Detail") / 100;       // 0..1
  const conceptT = +(0.2 + creativity * 1.0).toFixed(2);
  const schemaT  = +(1 - complexity).toFixed(2);
  const maxTok   = Math.round(4096 + detail * (16384 - 4096));
  const patch = {
    lm_studio: {
      concept_temperature: conceptT,
      schema_temperature:  schemaT,
      max_tokens:          maxTok,
    },
  };
  await saveConfigPatch(patch, $("#saveLlmNote"));
});

// ── Slider helpers ────────────────────────────────────────────────────────────
function readSlider(key) { return Number($(`#sld${key}`).value); }
function setSlider(key, value) {
  value = Math.max(0, Math.min(100, value));
  $(`#sld${key}`).value = value;
  $(`#val${key}`).textContent = value + "%";
}
["Creativity", "Complexity", "Detail"].forEach((k) => {
  const el = $(`#sld${k}`);
  el.addEventListener("input", () => $(`#val${k}`).textContent = el.value + "%");
});

// ── Test Connection ───────────────────────────────────────────────────────────
function renderTestResult(el, res, kind) {
  el.innerHTML = "";
  if (res.basic_mode) {
    el.className = "test-result ok";
    el.textContent = "Basic Mode — RAG skipped.";
    return;
  }
  if (res.ok) {
    el.className = "test-result ok";
    if (kind === "chat") {
      const n = (res.models || []).length;
      el.textContent = `Connected (${res.latency_ms}ms) — ${n} model(s)` +
        (n ? `: ${res.models.slice(0, 3).join(", ")}` : "");
    } else {
      el.textContent = `Workspaces ready (${res.workspaces.join(", ")}).`;
    }
    return;
  }
  el.className = "test-result err";
  const err = res.error
    || (kind === "rag"
        ? (res.gta?.error || res.unity?.error)
        : null)
    || { title: "Connection failed." };
  el.textContent = err.title;
  if (err.fix_steps) {
    const box = document.createElement("div");
    box.className = "fix-box";
    box.innerHTML = `<h4>${err.title}</h4><p>${err.likely_cause || ""}</p><ol>` +
      err.fix_steps.map((s) => `<li>${s}</li>`).join("") + "</ol>";
    el.appendChild(box);
  }
}
async function testConn(target, btn, resultEl, kind) {
  btn.disabled = true;
  resultEl.className = "test-result busy"; resultEl.textContent = "Testing…";
  try { renderTestResult(resultEl, await api(`/api/test/${target}`, { method: "POST" }), kind); }
  catch (e) { resultEl.className = "test-result err"; resultEl.textContent = "Request failed: " + e; }
  finally { btn.disabled = false; refreshStatus(); }
}
$("#btnTestChat").addEventListener("click", () => testConn("chat", $("#btnTestChat"), $("#resultChat"), "chat"));
$("#btnTestRag").addEventListener("click", () => testConn("rag", $("#btnTestRag"), $("#resultRag"), "rag"));

// Per-workspace bootstrap
$$("[data-setup]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const target = btn.dataset.setup;
    const el = $("#resultRag");
    btn.disabled = true;
    el.className = "test-result busy"; el.textContent = `Creating ${target} workspace…`;
    try {
      const res = await api(`/api/setup-workspace?target=${target}`, { method: "POST" });
      el.className = res.ok ? "test-result ok" : "test-result err";
      el.textContent = res.ok
        ? `${target} workspace ready ✓`
        : `Failed: ${JSON.stringify(res.results[target] || res, null, 0)}`;
    } finally {
      btn.disabled = false; refreshStatus();
    }
  });
});

// ── Prompt page: nested GTA / Unity / Micro toggle ────────────────────────────
let MODES = { core: [], micro: {} };


async function loadModes() {
  MODES = await api("/api/modes");
  const sel = $("#genMicro");
  sel.innerHTML = "";
  Object.entries(MODES.micro).forEach(([kind, info]) => {
    const o = document.createElement("option");
    o.value = kind; o.textContent = info.display; sel.appendChild(o);
  });
}

let UNITY_TYPE = "compose"; // "compose" | "micro" | "project"
// Unity is the product; GTA is a secondary showcase reached from the sidebar.
let PIPELINE = "unity";     // "unity" | "gta"

const PIPELINE_COPY = {
  unity: {
    title: "New Unity Project",
    hint:  "Assemble a wired, playable Unity project from the micro library.",
    placeholder: "e.g. A top-down racing game with 6 rival cars, grandstands, and a cheering crowd",
  },
  gta: {
    title: "GTA5 Mission (Showcase)",
    hint:  "Generate a ScriptHookVDotNet mission script for GTA5 — the project's origin-story demo.",
    placeholder: "e.g. Infiltrate the docks at night and steal a drug shipment",
  },
};

function setPipeline(p) {
  PIPELINE = p;
  updatePromptVisibility();
}

function updatePromptVisibility() {
  const pipeline = PIPELINE;
  // Drive the CSS-scoped .gta-only / .unity-only rules — single source of
  // truth so the Backends/Library/Prompt pages all stay in sync.
  document.body.dataset.pipeline = pipeline;
  const copy = PIPELINE_COPY[pipeline];
  $("#pipelineTitle").textContent = copy.title;
  $("#pipelineHint").textContent  = copy.hint;
  $("#genPrompt").placeholder     = copy.placeholder;
  $("#unitySubRow").hidden = pipeline !== "unity";
  const microActive = pipeline === "unity" && UNITY_TYPE === "micro";
  $("#microSubFields").hidden = !microActive;
  updateVariants();
  updateFieldRelevance();
  refreshNavActive();
  // Library auto-follows the active pipeline.
  if (LIB_TAB !== pipeline) {
    LIB_TAB = pipeline;
    $$(".seg[data-lib]").forEach((s) =>
      s.classList.toggle("is-active", s.dataset.lib === pipeline));
    refreshLibraryIfVisible();
  }
}

// Grey out controls that can't influence the current run, with a tooltip
// explaining why — choices read as a logical progression instead of a wall.
function updateFieldRelevance() {
  const setDisabled = (el, off, why) => {
    el.disabled = off;
    const field = el.closest(".field");
    if (field) {
      field.classList.toggle("is-muted", off);
      if (off && why) field.dataset.tip = why;
      else field.removeAttribute("data-tip");
    }
  };
  // Seed only changes the output when a theme (or AI-enhance random theme) is in play.
  const themed = !!$("#genTheme").value || $("#cfgEnhance").checked;
  setDisabled($("#genSeed"), !themed,
    "Seed only affects themed runs — pick a Theme (or enable AI Enhancements) first.");
  // Variant is only meaningful once a generator with variants is chosen.
  const kind = $("#genMicro").value;
  const hasVariants = !!(MODES.micro[kind]?.variants?.length);
  setDisabled($("#genVariant"), !hasVariants,
    "This generator has a single form — no variant to pick.");
}

$$(".seg[data-utype]").forEach((seg) => {
  seg.addEventListener("click", () => {
    $$(".seg[data-utype]").forEach((s) => s.classList.remove("is-active"));
    seg.classList.add("is-active");
    UNITY_TYPE = seg.dataset.utype;
    updatePromptVisibility();
  });
});

$("#genMicro").addEventListener("change", () => { updateVariants(); updateFieldRelevance(); });
$("#genTheme").addEventListener("change", updateFieldRelevance);
$("#cfgEnhance").addEventListener("change", updateFieldRelevance);

function updateVariants() {
  const kind = $("#genMicro").value;
  const sel = $("#genVariant");
  const info = MODES.micro[kind];
  // Generator tip comes from the server (single source — never drifts from the registry).
  const microTip = $("#microTip");
  if (microTip) microTip.dataset.tip = (info && info.help) || "Pick a subsystem to generate on its own.";
  sel.innerHTML = '<option value="">(auto)</option>';
  if (info && info.variants.length) {
    info.variants.forEach((v) => {
      const o = document.createElement("option");
      o.value = v; o.textContent = v;
      sel.appendChild(o);
    });
  }
  const vtip = $("#variantTip");
  if (vtip) vtip.dataset.tip = kind
    ? "Leave (auto) for the default, or pick a variant."
    : "Pick a generator first.";
}

function currentMode() {
  if (PIPELINE === "gta") return "gta";
  if (UNITY_TYPE === "micro" && $("#genMicro").value) return $("#genMicro").value;
  return "unity";
}

async function loadThemes() {
  const themes = await api("/api/themes");
  const sel = $("#genTheme");
  themes.forEach((t) => {
    const o = document.createElement("option");
    o.value = t.name; o.textContent = t.display; sel.appendChild(o);
  });
}

// ── Custom Args parsing ───────────────────────────────────────────────────────
function parseExtraArgs(raw) {
  // very small parser: --flag, --key=value, --key value
  const tokens = (raw || "").trim().split(/\s+/).filter(Boolean);
  const out = {};
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    if (!t.startsWith("--")) continue;
    const body = t.slice(2);
    if (body.includes("=")) {
      const [k, v] = body.split("=", 2);
      out[k] = v;
    } else if (i + 1 < tokens.length && !tokens[i + 1].startsWith("--")) {
      out[body] = tokens[++i];
    } else {
      out[body] = true;
    }
  }
  return out;
}

// ── Generate ──────────────────────────────────────────────────────────────────
$("#btnGenerate").addEventListener("click", async () => {
  const prompt = $("#genPrompt").value.trim();
  const status = $("#genStatus"), consoleEl = $("#genConsole"), outputEl = $("#genOutput");
  if (!prompt) { status.className = "test-result err"; status.textContent = "Enter a prompt."; return; }

  consoleEl.textContent = ""; outputEl.hidden = true; outputEl.innerHTML = "";
  status.className = "test-result busy"; status.textContent = "Running…";
  $("#btnGenerate").disabled = true;

  const enhance = $("#cfgEnhance").checked;
  const body = {
    mode:    currentMode(),
    prompt,
    theme:   $("#genTheme").value || (enhance ? "random" : null),
    variant: $("#genVariant").value || null,
    seed:    $("#genSeed").value ? parseInt($("#genSeed").value, 10) : null,
    // Compose Game mode assembles the micro library into a wired project.
    compose: PIPELINE === "unity" && UNITY_TYPE === "compose",
    extra_args: parseExtraArgs($("#genExtraArgs").value),
  };

  const { run_id } = await api("/api/generate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  const es = new EventSource(`/api/generate/${run_id}/stream`);
  es.onmessage = (ev) => {
    const { line } = JSON.parse(ev.data);
    consoleEl.textContent += line + "\n";
    consoleEl.scrollTop = consoleEl.scrollHeight;
  };
  es.addEventListener("done", (ev) => {
    es.close();
    $("#btnGenerate").disabled = false;
    const res = JSON.parse(ev.data);
    outputEl.hidden = false;
    if (res.ok) {
      status.className = "test-result ok"; status.textContent = "Done ✓";
      renderOutputSuccess(outputEl, res);
      refreshLibraryIfVisible();
    } else {
      status.className = "test-result err";
      const err = res.error || {};
      status.textContent = err.title || "Generation failed.";
      outputEl.innerHTML = `<div class="fix-box"><h4>${err.title}</h4>` +
        `<p>${err.likely_cause || ""}</p><ol>` +
        (err.fix_steps || []).map((s) => `<li>${s}</li>`).join("") + "</ol></div>";
    }
  });
  es.onerror = () => {
    es.close(); $("#btnGenerate").disabled = false;
    if (status.classList.contains("busy")) {
      status.className = "test-result err"; status.textContent = "Stream interrupted.";
    }
  };
});

function renderOutputSuccess(el, res) {
  el.innerHTML = `<strong>Output:</strong> <code>${res.output}</code>
    <div class="row">
      <button class="btn btn--tiny" data-copy="${escapeAttr(res.output)}">Copy path</button>
      <button class="btn btn--tiny" data-open="${escapeAttr(res.output)}">Open file location</button>
    </div>`;
  if (res.mode === "gta") {
    const hint = document.createElement("div");
    hint.className = "hint-box";
    hint.innerHTML = `<h4>What now? ScriptHookVDotNet3.dll required</h4>
      <p>GTA5 mods need both <strong>ScriptHookV</strong> and
      <strong>ScriptHookVDotNet3.dll</strong> installed in your
      <code>Grand Theft Auto V/</code> directory. Drop the generated
      <code>.3.cs</code> file into <code>Grand Theft Auto V/scripts/</code>.
      The mod is auto-compiled on the fly &mdash; no Visual Studio needed.</p>
      <ol>
        <li>Install <strong>ScriptHookV</strong> from dev-c.com.</li>
        <li>Install <strong>ScriptHookVDotNet</strong> (drops three DLLs incl. <code>ScriptHookVDotNet3.dll</code>) in the game root.</li>
        <li>Create <code>Grand Theft Auto V/scripts/</code> if it doesn't exist.</li>
        <li>Copy the generated <code>.3.cs</code> in there. Launch the game.</li>
      </ol>`;
    el.appendChild(hint);
  }
  bindCopyOpen(el);
}

function escapeAttr(s) { return String(s).replace(/"/g, "&quot;"); }

function bindCopyOpen(scope) {
  scope.querySelectorAll("[data-copy]").forEach((b) => {
    b.addEventListener("click", () => {
      navigator.clipboard.writeText(b.dataset.copy);
      const orig = b.textContent; b.textContent = "Copied ✓";
      setTimeout(() => (b.textContent = orig), 1500);
    });
  });
  scope.querySelectorAll("[data-open]").forEach((b) => {
    b.addEventListener("click", async () => {
      const res = await api("/api/open", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: b.dataset.open }),
      });
      if (!res.ok && res.detail) alert("Open failed: " + res.detail);
    });
  });
}

// ── Library ───────────────────────────────────────────────────────────────────
let LIB_TAB = "unity";
$$(".seg[data-lib]").forEach((s) => {
  s.addEventListener("click", () => {
    $$(".seg[data-lib]").forEach((x) => x.classList.remove("is-active"));
    s.classList.add("is-active");
    LIB_TAB = s.dataset.lib;
    renderLibrary();
  });
});

function refreshLibraryIfVisible() {
  if ($('.panel[data-section="library"]').classList.contains("is-active")) renderLibrary();
}

async function renderLibrary() {
  const list = $("#libList");
  list.innerHTML = '<div class="help">Loading…</div>';
  const items = await api("/api/library/" + LIB_TAB);
  if (!items.length) { list.innerHTML = '<div class="help">Nothing here yet — run a generation.</div>'; return; }
  list.innerHTML = "";
  items.forEach((it) => {
    const path = it.path || it.schema;
    const row = document.createElement("div");
    row.className = "lib-row";
    const date = new Date(it.mtime * 1000).toLocaleString();
    const kind = it.kind && it.kind !== "project" ? `<span class="kind">${it.kind}</span>` : "";
    row.innerHTML = `
      <div class="title">${it.title}${kind}<div class="lib-meta">${date}</div></div>
      <div class="stars" data-path="${escapeAttr(path)}"></div>
      <div class="lib-meta">${shortPath(path)}</div>
      <div class="lib-actions">
        <button class="btn btn--tiny" data-copy="${escapeAttr(path)}">Share</button>
        <button class="btn btn--tiny" data-open="${escapeAttr(path)}">Open</button>
      </div>`;
    list.appendChild(row);
    renderStars(row.querySelector(".stars"), it.rating, path);
    attachConceptPreview(row.querySelector(".title"), it);
  });
  bindCopyOpen(list);
}

const CONCEPT_PREVIEW_LEN = 200;
function attachConceptPreview(titleCell, item) {
  const concept = (item.concept || "").trim();
  const userPrompt = (item.user_prompt || "").trim();
  if (!concept && !userPrompt) return;
  const block = document.createElement("div");
  block.className = "lib-concept";
  const prompt = userPrompt ? `<em>"${escapeText(userPrompt)}"</em><br>` : "";
  if (concept.length <= CONCEPT_PREVIEW_LEN) {
    block.innerHTML = prompt + escapeText(concept);
  } else {
    const short = concept.slice(0, CONCEPT_PREVIEW_LEN).trim();
    block.innerHTML = prompt +
      `<span class="concept-text">${escapeText(short)}…</span> ` +
      `<span class="more" data-action="expand">Show more</span>`;
    block.addEventListener("click", (e) => {
      const a = e.target.closest("[data-action]");
      if (!a) return;
      if (a.dataset.action === "expand") {
        block.innerHTML = prompt + `<span class="concept-text">${escapeText(concept)}</span> ` +
          `<span class="more" data-action="collapse">Show less</span>`;
      } else {
        block.innerHTML = prompt + `<span class="concept-text">${escapeText(short)}…</span> ` +
          `<span class="more" data-action="expand">Show more</span>`;
      }
    });
  }
  titleCell.appendChild(block);
}

function escapeText(s) {
  const d = document.createElement("div");
  d.textContent = String(s);
  return d.innerHTML;
}

function shortPath(p) {
  const parts = p.replace(/\\/g, "/").split("/");
  return parts.slice(-3).join("/");
}

function renderStars(el, rating, path) {
  el.innerHTML = "";
  for (let i = 1; i <= 5; i++) {
    const s = document.createElement("span");
    s.className = "star" + (i <= rating ? " on" : "");
    s.textContent = "★";
    s.addEventListener("click", async () => {
      const next = (rating === i) ? 0 : i;   // click same star to clear
      await api("/api/ratings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, rating: next }),
      });
      rating = next;
      renderStars(el, next, path);
    });
    el.appendChild(s);
  }
}

// ── Tutorial ──────────────────────────────────────────────────────────────────
async function loadTutorial() {
  const tut = await api("/api/tutorial");
  $("#tutTitle").textContent = tut.title;
  $("#tutIntro").textContent = tut.intro;
  $("#tutSteps").innerHTML = (tut.steps || []).map((s) =>
    `<li><h4>${s.title}</h4><p>${s.body}</p>` +
    (s.cta ? `<button class="btn btn--tiny" data-goto="${s.cta.section}">${s.cta.label}</button>` : "") +
    `</li>`).join("");
  $("#tutTips").innerHTML = (tut.tips || []).map((t) => `<li>${t}</li>`).join("");
  $$("#tutSteps [data-goto]").forEach((b) => b.addEventListener("click", () => showSection(b.dataset.goto)));

  const hide = localStorage.getItem("tutorial.hide") === "1";
  $("#tutHide").checked = hide;
  $("#tutHide").addEventListener("change", (e) =>
    localStorage.setItem("tutorial.hide", e.target.checked ? "1" : "0"));
  if (!hide && !localStorage.getItem("tutorial.seen")) {
    showSection("tutorial");
    localStorage.setItem("tutorial.seen", "1");
  }
}

// ── Errors page ───────────────────────────────────────────────────────────────
let ERRORS = [];
function renderErrors(filter) {
  const list = $("#errorList");
  const q = (filter || "").toLowerCase();
  list.innerHTML = "";
  ERRORS.filter((e) =>
    !q || (e.title + e.symptom + e.likely_cause + e.fix_steps.join(" ")).toLowerCase().includes(q)
  ).forEach((e) => {
    const card = document.createElement("div");
    card.className = "error-card";
    card.innerHTML = `<h3>${e.title}</h3><p class="sym">${e.symptom}</p>` +
      `<p><strong>Cause:</strong> ${e.likely_cause}</p><ol>` +
      e.fix_steps.map((s) => `<li>${s}</li>`).join("") + "</ol>";
    list.appendChild(card);
  });
}
$("#errorSearch").addEventListener("input", (e) => renderErrors(e.target.value));

async function loadErrors() {
  ERRORS = await api("/api/errors"); renderErrors("");
}

// ── Pipeline Builder (full CLI explorer) ─────────────────────────────────────
let CLI_CATEGORIES = { unity: [], gta: [], shared: [] };
let CLI_CAT = "unity";
let CLI_CMD = null;      // currently selected command object
let CLI_RUN_ES = null;   // active EventSource, if a run is streaming

async function loadCliCommands() {
  const res = await api("/api/cli/commands");
  CLI_CATEGORIES = res.categories;
  renderCliList();
}

function setCliCategory(cat) {
  CLI_CAT = cat;
  $$(".pb-toolbar .seg[data-cat]").forEach((s) => s.classList.toggle("is-active", s.dataset.cat === cat));
  renderCliList();
}
$$(".pb-toolbar .seg[data-cat]").forEach((seg) => {
  seg.addEventListener("click", () => setCliCategory(seg.dataset.cat));
});
$("#cliSearch").addEventListener("input", renderCliList);

function renderCliList() {
  const list = $("#cliCommandList");
  const q = $("#cliSearch").value.trim().toLowerCase();
  const cmds = (CLI_CATEGORIES[CLI_CAT] || []).filter((c) =>
    !q || c.name.includes(q) || (c.help || "").toLowerCase().includes(q));

  list.innerHTML = "";
  if (!cmds.length) {
    list.innerHTML = '<div class="pb-empty">No commands match.</div>';
    return;
  }
  let lastGroup = null;
  cmds.forEach((c) => {
    if (c.group !== lastGroup) {
      lastGroup = c.group;
      const label = document.createElement("div");
      label.className = "pb-group-label";
      label.textContent = c.group;
      list.appendChild(label);
    }
    const btn = document.createElement("button");
    btn.className = "pb-cmd-item" + (CLI_CMD && CLI_CMD.name === c.name ? " is-active" : "");
    btn.innerHTML = `<span class="pb-cmd-name">${c.name}${c.slow ? '<span class="pb-slow-tag">slow</span>' : ""}</span>` +
      `<span class="pb-cmd-blurb">${escapeText(c.help || "")}</span>`;
    btn.addEventListener("click", () => selectCliCommand(c));
    list.appendChild(btn);
  });
}

function selectCliCommand(cmd) {
  CLI_CMD = cmd;
  renderCliList();
  $("#cliCmdName").textContent = "python main.py " + cmd.name;
  $("#cliCmdHelp").textContent = cmd.help || "";
  $("#cliAbout").textContent = cmd.help || "";
  $("#btnCliRun").disabled = false;
  renderCliForm(cmd);
  renderCliOptionsRef(cmd);
  updateCliPreview();
}

function cliFieldId(opt) { return "cliOpt_" + opt.name; }

function renderCliForm(cmd) {
  const form = $("#cliForm");
  form.innerHTML = "";
  if (!cmd.options.length) {
    form.innerHTML = '<div class="pb-empty">This command takes no arguments.</div>';
    return;
  }
  cmd.options.forEach((opt) => {
    const wrap = document.createElement("label");
    wrap.className = "field" + (opt.kind === "bool" ? " field--bool" : "");
    const label = opt.positional ? `${opt.name} (positional)` : (opt.flag || opt.name);
    let control = "";
    const id = cliFieldId(opt);
    if (opt.kind === "bool") {
      const checked = opt.default ? "checked" : "";
      control = `<input id="${id}" type="checkbox" ${checked} />`;
      wrap.innerHTML = control + `<span>${escapeText(label)}${opt.help ? ` <i class="tip" data-tip="${escapeAttr(opt.help)}">?</i>` : ""}</span>`;
    } else if (opt.kind === "choice") {
      const choices = opt.choices || [];
      const optsHtml = ['<option value="">(auto)</option>']
        .concat(choices.map((c) => `<option value="${escapeAttr(c)}" ${opt.default === c ? "selected" : ""}>${escapeText(c)}</option>`))
        .join("");
      wrap.innerHTML = `<span>${escapeText(label)}${opt.help ? ` <i class="tip" data-tip="${escapeAttr(opt.help)}">?</i>` : ""}</span>` +
        `<select id="${id}">${optsHtml}</select>`;
    } else if (opt.kind === "multi") {
      wrap.innerHTML = `<span>${escapeText(label)} (one per line)${opt.help ? ` <i class="tip" data-tip="${escapeAttr(opt.help)}">?</i>` : ""}</span>` +
        `<textarea id="${id}" class="pb-multi" placeholder="key=value"></textarea>`;
    } else {
      const type = opt.kind === "int" || opt.kind === "float" ? "number" : "text";
      const step = opt.kind === "float" ? ' step="any"' : "";
      const val = (opt.default !== null && opt.default !== undefined) ? ` value="${escapeAttr(opt.default)}"` : "";
      wrap.innerHTML = `<span>${escapeText(label)}${opt.help ? ` <i class="tip" data-tip="${escapeAttr(opt.help)}">?</i>` : ""}</span>` +
        `<input id="${id}" type="${type}"${step}${val} placeholder="${opt.positional ? "" : escapeAttr(opt.help || "")}" />`;
    }
    form.appendChild(wrap);
    form.querySelectorAll(`#${CSS.escape(id)}`).forEach((el) =>
      el.addEventListener("input", updateCliPreview));
    form.querySelectorAll(`#${CSS.escape(id)}`).forEach((el) =>
      el.addEventListener("change", updateCliPreview));
  });
}

function renderCliOptionsRef(cmd) {
  const el = $("#cliOptionsRef");
  el.innerHTML = cmd.options.filter((o) => o.help).map((o) => `
    <div class="pb-opt-ref-item">
      <span class="flag">${escapeText(o.positional ? o.name : (o.flag + (o.short ? ", " + o.short : "")))}</span>
      <div class="desc">${escapeText(o.help)}</div>
    </div>`).join("");
}

// Read current form values into {optName: value} keyed exactly like the
// server's Opt.name — sent verbatim to /api/cli/run.
function readCliArgs(cmd) {
  const args = {};
  cmd.options.forEach((opt) => {
    const el = document.getElementById(cliFieldId(opt));
    if (!el) return;
    if (opt.kind === "bool") {
      args[opt.name] = el.checked;
    } else if (opt.kind === "multi") {
      args[opt.name] = el.value.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    } else if (opt.kind === "int") {
      args[opt.name] = el.value === "" ? null : parseInt(el.value, 10);
    } else if (opt.kind === "float") {
      args[opt.name] = el.value === "" ? null : parseFloat(el.value);
    } else {
      args[opt.name] = el.value === "" ? null : el.value;
    }
  });
  return args;
}

// Mirrors server _build_argv() so the preview matches exactly what runs.
function buildArgvPreview(cmd, args) {
  const positionals = [];
  const flags = [];
  cmd.options.forEach((opt) => {
    const raw = args[opt.name];
    if (opt.positional) {
      if (raw === null || raw === undefined || raw === "") return;
      if (opt.name === "targets") positionals.push(...String(raw).split(/\s+/).filter(Boolean));
      else positionals.push(String(raw));
      return;
    }
    const kind = opt.kind;
    if (kind === "bool") {
      if (raw === null || raw === undefined) return;
      if (opt.flag.includes("/")) {
        const [onFlag, offFlag] = opt.flag.split("/");
        if (opt.default !== null && opt.default !== undefined && !!raw === !!opt.default) return;
        flags.push(raw ? onFlag : offFlag);
      } else if (raw) {
        flags.push(opt.flag);
      }
      return;
    }
    if (kind === "multi") {
      (raw || []).forEach((item) => { if (item) flags.push(opt.flag, item); });
      return;
    }
    if (raw === null || raw === undefined || raw === "") return;
    if (opt.default !== null && opt.default !== undefined && String(raw) === String(opt.default)) return;
    flags.push(opt.flag, String(raw));
  });
  return [cmd.name, ...positionals, ...flags];
}

function shellQuote(tok) {
  return /[\s"']/.test(tok) ? `"${tok.replace(/"/g, '\\"')}"` : tok;
}

function updateCliPreview() {
  if (!CLI_CMD) return;
  const args = readCliArgs(CLI_CMD);
  const argv = buildArgvPreview(CLI_CMD, args);
  $("#cliPreview").textContent = "python main.py " + argv.map(shellQuote).join(" ");
}

$("#btnCliCopy").addEventListener("click", () => {
  navigator.clipboard.writeText($("#cliPreview").textContent);
  const btn = $("#btnCliCopy");
  const orig = btn.textContent;
  btn.textContent = "Copied ✓";
  setTimeout(() => (btn.textContent = orig), 1500);
});

$("#btnCliRun").addEventListener("click", async () => {
  if (!CLI_CMD) return;
  const args = readCliArgs(CLI_CMD);
  const status = $("#cliStatus"), consoleEl = $("#cliConsole");
  consoleEl.textContent = "";
  status.className = "test-result busy"; status.textContent = "Running…";
  $("#btnCliRun").disabled = true;
  $("#btnCliStop").hidden = false;

  let run_id;
  try {
    ({ run_id } = await api("/api/cli/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: CLI_CMD.name, args }),
    }));
  } catch (e) {
    status.className = "test-result err"; status.textContent = "Failed to start: " + e;
    $("#btnCliRun").disabled = false; $("#btnCliStop").hidden = true;
    return;
  }

  const es = new EventSource(`/api/cli/run/${run_id}/stream`);
  CLI_RUN_ES = es;
  es.onmessage = (ev) => {
    const { line } = JSON.parse(ev.data);
    consoleEl.textContent += line + "\n";
    consoleEl.scrollTop = consoleEl.scrollHeight;
  };
  es.addEventListener("done", (ev) => {
    es.close(); CLI_RUN_ES = null;
    $("#btnCliRun").disabled = false; $("#btnCliStop").hidden = true;
    const res = JSON.parse(ev.data);
    if (res.ok) {
      status.className = "test-result ok"; status.textContent = "Done ✓";
    } else {
      status.className = "test-result err";
      status.textContent = res.error?.title || `Exited with code ${res.returncode ?? "?"}`;
    }
  });
  es.onerror = () => {
    es.close(); CLI_RUN_ES = null;
    $("#btnCliRun").disabled = false; $("#btnCliStop").hidden = true;
    if (status.classList.contains("busy")) {
      status.className = "test-result err"; status.textContent = "Stream interrupted.";
    }
  };
});

$("#btnCliStop").addEventListener("click", async () => {
  if (CLI_RUN_ES) {
    // The run_id isn't tracked separately from the ES URL, so pull it back out.
    const m = /\/api\/cli\/run\/([a-f0-9]+)\/stream/.exec(CLI_RUN_ES.url);
    if (m) await api(`/api/cli/run/${m[1]}/stop`, { method: "POST" });
  }
});

// ── Init ──────────────────────────────────────────────────────────────────────
async function loadMeta() {
  try {
    const meta = await api("/api/meta");
    if (meta.name) {
      $("#brandName").textContent = meta.name;
      $("#docTitle").textContent = meta.name;
    }
    if (meta.tagline) $("#brandTagline").textContent = meta.tagline;
  } catch { /* keep the static fallback */ }
}

(async function init() {
  await loadMeta();
  await loadConfig();
  await loadModes();
  await loadThemes();
  await loadErrors();
  await loadTutorial();
  await loadCliCommands();
  // Set body[data-pipeline] to match the segmented control's default (Unity)
  // so CSS-scoped .gta-only/.unity-only rules take effect on first paint.
  updatePromptVisibility();
  await refreshStatus();
  await renderLibrary();
  setInterval(refreshStatus, 8000);
})();
