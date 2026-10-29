"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = { tab: "overview", latest: null, tuningRevision: 0, personaRevision: 0, tuningDirty: false, personaDirty: false, tuningLoaded: false, personaLoaded: false, defaults: null, models: null, polling: false };
const titles = {
  overview: ["Overview", "A live look at replies and model readiness."],
  tuning: ["Tuning", "Make the next reply feel right. Changes stay on this computer."],
  training: ["Training", "Full-archive Llama training, with safe pause and recovery."],
  personality: ["Personality", "Fine-tune the voice and facts Aku can share."]
};
const emptyChart = $(".empty-chart").cloneNode(true);
let toastTimer;

function text(selector, value) { $(selector).textContent = String(value); }
function duration(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "N/A";
  return value < 1 ? `${Math.round(value * 1000)} ms` : `${Number(value).toFixed(value < 10 ? 2 : 1)} s`;
}
function compact(value) { return Number(value || 0).toLocaleString(); }
function keepLabel(value) { return ({ "5m": "5 minutes", "30m": "30 minutes", "2h": "2 hours", "-1": "Always" })[value] || value; }
function clock(value) { const date = new Date(value); return Number.isFinite(date.getTime()) ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "N/A"; }
function node(tag, content, className) { const result = document.createElement(tag); if (content !== undefined) result.textContent = content; if (className) result.className = className; return result; }
function enableForm(form, enabled) { [...form.querySelectorAll('input,select,textarea,button')].forEach((element) => { element.disabled = !enabled; }); }
function toast(message, error = false) {
  const element = $("#toast"); element.textContent = message; element.className = error ? "toast error" : "toast"; element.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { element.hidden = true; }, error ? 8000 : 4000);
}
async function api(path, options = {}) {
  const { timeout = 12000, ...requestOptions } = options;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(path, { ...requestOptions, cache: "no-store", credentials: "same-origin", signal: controller.signal, headers: { "X-Aku-Local": "1", ...(requestOptions.body ? { "Content-Type": "application/json" } : {}) } });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "The local request could not complete.");
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("The local request took too long. Try again shortly.");
    throw error;
  } finally { clearTimeout(timer); }
}
function send(path, body, method = "POST", timeout = 12000) { return api(path, { method, body: JSON.stringify(body), timeout }); }

async function selectTab(tab) {
  if (!titles[tab]) tab = "overview";
  state.tab = tab;
  $$(".page").forEach((page) => { page.hidden = page.id !== `page-${tab}`; });
  $$(".nav-item").forEach((button) => { const active = button.dataset.tab === tab; button.classList.toggle("active", active); if (active) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current"); });
  text("#page-title", titles[tab][0]); text("#page-description", titles[tab][1]);
  history.replaceState(null, "", `#${tab}`);
  try {
    if (tab === "training") await refreshTraining();
    if (tab === "tuning" && !state.tuningLoaded) await loadTuning();
    if (tab === "personality" && !state.personaLoaded) await loadPersona();
  } catch (error) { toast(error.message, true); }
}
$$('[data-tab]').forEach((button) => button.addEventListener("click", () => selectTab(button.dataset.tab)));
$(".brand").addEventListener("click", (event) => { event.preventDefault(); selectTab("overview"); });

function renderStatus(data) {
  state.latest = data;
  const allPaused = data.bot.paused_servers === data.bot.servers;
  const label = !data.bot.connected ? "Reconnecting" : allPaused ? "Paused" : data.bot.paused_servers ? "Partly paused" : "Connected";
  $("#connection-badge").replaceChildren(node("span", undefined, `status-dot${data.bot.connected && !allPaused ? " online" : ""}`), document.createTextNode(label));
  text("#sidebar-status", label); $("#sidebar-state").className = `status-dot${data.bot.connected && !allPaused ? " online" : ""}`;
  text("#pause-button", data.bot.paused_servers ? "Resume replies" : "Pause replies"); $("#pause-button").disabled = false;
  for (const [selector, value] of [["#last-reply", data.metrics.last_reply_seconds], ["#typical-reply", data.metrics.typical_reply_seconds]]) {
    text(selector, value === null ? "Waiting" : duration(value)); $(selector).classList.toggle("waiting", value === null);
  }
  text("#reply-count", compact(data.metrics.counts.reply)); text("#queue-count", data.metrics.queued);
  text("#active-count", data.metrics.active ? `${data.metrics.active} in progress` : "Nothing in progress");
  const minutes = Math.floor(data.metrics.uptime_seconds / 60);
  text("#uptime", minutes < 1 ? "Session just started" : `Online for ${minutes < 60 ? `${minutes} min` : `${Math.floor(minutes / 60)} hr ${minutes % 60} min`}`);
  text("#model-name", data.model.name);
  const modelState = data.model.warming ? "Loading" : !data.model.available ? "Unavailable" : data.model.loaded ? "Ready" : "Idle";
  text("#model-badge", modelState); $("#model-badge").className = `pill ${modelState === "Ready" ? "success" : modelState === "Idle" ? "muted" : "warning"}`;
  text("#model-caption", data.model.warming ? "Preparing the model for your next reply." : !data.model.available ? "The local model is not responding." : data.model.loaded ? "Loaded and ready to reply." : "The next reply will need to load the model.");
  text("#gpu-percent", data.model.loaded ? `${data.model.gpu_percent}%` : "Not loaded");
  text("#vram", data.model.loaded ? `${data.model.vram_gb} GB` : "Not loaded"); text("#keep-ready", keepLabel(data.tuning.keep_alive));
  $("#warm-button").disabled = data.model.warming || data.metrics.active > 0; text("#warm-button", data.model.warming ? "Loading model…" : "Load model");
  text("#rate-limited-count", data.metrics.counts.cooldown ? `${compact(data.metrics.counts.cooldown)} cooldown${data.metrics.counts.cooldown === 1 ? "" : "s"}` : "");
  text("#footer-status", `Updated ${clock(Date.now())}${data.bot.gateway_ms === null ? "" : ` · Discord ${data.bot.gateway_ms} ms`}`);
  renderActivity(data.metrics.recent); renderChart(data.metrics.recent);
  if (state.tuningLoaded && !state.tuningDirty && state.tuningRevision !== data.revision) fillTuning(data.tuning, data.revision, data.models);
  if (state.personaLoaded && !state.personaDirty && state.personaRevision !== data.revision && state.tab === "personality") loadPersona().catch(() => {});
}
async function refreshStatus() {
  if (state.polling) return;
  state.polling = true;
  try { renderStatus(await api("/api/status")); $("#connection-error").hidden = true; }
  catch { $("#connection-error").hidden = false; text("#sidebar-status", "Disconnected"); $("#sidebar-state").className = "status-dot offline"; }
  finally { state.polling = false; }
}
function renderActivity(events) {
  const body = $("#activity-body");
  if (!events.length) { const row = node("tr"); const cell = node("td", "Activity will appear after the next mention.", "empty-cell"); cell.colSpan = 7; row.append(cell); body.replaceChildren(row); return; }
  const labels = { reply: "Replied", ready: "Ready", cooldown: "Cooldown", server_limit: "Server limit", paused: "Paused", busy: "Busy", cancelled: "Cancelled", model_error: "Model error", empty: "Empty ping", too_long: "Too long", empty_reply: "Empty reply", error: "Error" };
  body.replaceChildren(...events.slice(0, 12).map((event) => {
    const row = node("tr");
    const name = event.source === "warm" ? "Model load" : event.source === "preview" ? "Local test" : `Reply ${event.id}`;
    [name, clock(event.at), duration(event.queue_seconds), duration(event.load_seconds), duration(event.generation_seconds), duration(event.total_seconds)].forEach((value) => row.append(node("td", value)));
    const cell = node("td"); const type = ["reply", "ready"].includes(event.outcome) ? "" : ["error", "model_error", "empty_reply"].includes(event.outcome) ? " error" : " warning";
    cell.append(node("span", labels[event.outcome] || "Unavailable", `result${type}`)); row.append(cell); return row;
  }));
}
function svgNode(tag, attributes = {}, content) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", tag);
  Object.entries(attributes).forEach(([name, value]) => element.setAttribute(name, String(value)));
  if (content !== undefined) element.textContent = content;
  return element;
}
function renderChart(events) {
  const data = events.filter((event) => ["reply", "ready"].includes(event.outcome)).slice(0, 24).reverse();
  if (!data.length) { $("#timing-chart").replaceChildren(emptyChart.cloneNode(true)); return; }
  const width = 650, height = 205, left = 39, bottom = 24, top = 12, plot = height - top - bottom;
  const max = Math.max(.1, ...data.map((event) => event.total_seconds));
  const ceiling = max < 1 ? Math.ceil(max * 10) / 10 : Math.ceil(max);
  const svg = svgNode("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Recent reply time, divided into waiting, model loading, and generation" });
  for (let i = 0; i < 4; i++) {
    const y = top + plot * i / 3;
    svg.append(svgNode("line", { x1: left, x2: width, y1: y, y2: y, class: "chart-grid" }));
    svg.append(svgNode("text", { x: 0, y: y + 4, class: "chart-tick" }, `${(ceiling * (1 - i / 3)).toFixed(ceiling < 2 ? 1 : 0)}s`));
  }
  const step = (width - left) / Math.max(data.length, 12);
  data.forEach((event, index) => {
    const group = svgNode("g"); group.append(svgNode("title", {}, `Event ${event.id}: ${duration(event.total_seconds)} total`));
    let y = top + plot;
    const queue = event.queue_seconds || 0, load = event.load_seconds || 0;
    const generate = Math.max(0, event.total_seconds - queue - load);
    [[queue, "queue"], [load, "load"], [generate, "generation"]].forEach(([value, type]) => {
      const size = Math.max(0, Math.min(plot, value / ceiling * plot)); y -= size;
      group.append(svgNode("rect", { x: left + index * step + step * .17, y, width: step * .60, height: Math.max(value > 0 ? 1 : 0, size), rx: 2, class: `chart-bar-${type}` }));
    });
    svg.append(group);
  });
  svg.append(svgNode("text", { x: left, y: height - 3, class: "chart-tick" }, clock(data[0].at)));
  svg.append(svgNode("text", { x: width - 3, y: height - 3, class: "chart-tick", "text-anchor": "end" }, "Latest"));
  $("#timing-chart").replaceChildren(svg);
}

function tuningOutputs() {
  const elements = $("#tuning-form").elements;
  text("#max-tokens-output", `${elements.max_tokens.value} tokens`); text("#temperature-output", Number(elements.temperature.value).toFixed(2));
  const cooldown = Number(elements.cooldown_seconds.value); text("#cooldown-output", cooldown === 0 ? "Off" : `${cooldown} second${cooldown === 1 ? "" : "s"}`);
}
function modelSelection(model) {
  const adapter = state.models.adapters.find((entry) => entry.model === model);
  if (adapter) return { base_id: adapter.base_id, adapter_id: adapter.id };
  const base = state.models.bases.find((entry) => entry.model === model);
  return base ? { base_id: base.id, adapter_id: null } : null;
}
function renderAdapters(adapterId = null) {
  const baseId = $("#tune-base").value;
  const base = state.models.bases.find((entry) => entry.id === baseId);
  const none = node("option", "None (base model only)"); none.value = "";
  const options = state.models.adapters.map((entry) => {
    const matching = entry.base_id === baseId;
    const owner = state.models.bases.find((item) => item.id === entry.base_id);
    const option = node("option", matching ? entry.label : `${entry.label} (requires ${owner?.label || "another base"})`);
    option.value = entry.id; option.disabled = !matching || !entry.available;
    return option;
  });
  $("#tune-adapter").replaceChildren(none, ...options);
  const selected = state.models.adapters.find((entry) => entry.id === adapterId && entry.base_id === baseId);
  $("#tune-adapter").value = selected ? selected.id : "";
  $("#tune-model").value = selected ? selected.model : base.model;
  text("#adapter-help", selected ? `${selected.label} was trained for ${base.label}.` : state.models.adapters.some((entry) => entry.base_id === baseId) ? "Use the base model alone, or choose its trained style above." : "No LoRA is installed for this base. Aku style 1 needs a separate training run to work with a different base model.");
}
function fillTuning(tuning, revision, models = null) {
  if (models) state.models = models;
  if (!state.models) return;
  const form = $("#tuning-form"), selected = modelSelection(tuning.model);
  $("#tune-base").replaceChildren(...state.models.bases.map((entry) => {
    const option = node("option", entry.label + (entry.available ? "" : " (unavailable)")); option.value = entry.id; option.disabled = !entry.available && entry.id !== selected?.base_id; return option;
  }));
  if (selected) $("#tune-base").value = selected.base_id;
  renderAdapters(selected?.adapter_id);
  Object.entries(tuning).forEach(([name, value]) => { const input = form.elements.namedItem(name); if (input) { if (input.type === "checkbox") input.checked = value; else input.value = value; } });
  state.tuningRevision = revision; state.tuningDirty = false; state.tuningLoaded = true; tuningOutputs(); enableForm(form, true);
}
$("#tune-base").addEventListener("change", () => renderAdapters());
$("#tune-adapter").addEventListener("change", () => renderAdapters($("#tune-adapter").value || null));
async function loadTuning() {
  enableForm($("#tuning-form"), false);
  const data = await api("/api/settings"); state.defaults = data.defaults; fillTuning(data.tuning, data.revision, data.models);
}
$("#tuning-form").addEventListener("input", () => { state.tuningDirty = true; tuningOutputs(); text("#tuning-save-state", "Unsaved changes. Apply them to the next reply when ready."); });
$("#tuning-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget;
  const tuning = {};
  for (const name of ["model", "keep_alive"]) tuning[name] = form.elements.namedItem(name).value;
  for (const name of ["temperature", "max_tokens", "context_tokens", "cooldown_seconds", "requests_per_minute", "memory_turns", "memory_minutes"]) tuning[name] = Number(form.elements.namedItem(name).value);
  tuning.preload = form.elements.preload.checked;
  enableForm(form, false);
  try { const data = await send("/api/settings", { revision: state.tuningRevision, tuning, selection: { base_id: $("#tune-base").value, adapter_id: $("#tune-adapter").value || null } }, "PUT"); state.defaults = data.defaults; fillTuning(data.tuning, data.revision, data.models); toast("Settings saved. They apply to the next reply."); text("#tuning-save-state", "Saved locally. No restart needed."); await refreshStatus(); }
  catch (error) { toast(error.message, true); }
  finally { enableForm(form, true); }
});
$("#restore-tuning").addEventListener("click", () => {
  if (!state.defaults) return;
  fillTuning(state.defaults, state.tuningRevision); state.tuningDirty = true; text("#tuning-save-state", "Defaults restored in this form. Save to apply them.");
});
$("#pause-button").addEventListener("click", async () => {
  if (!state.latest) return;
  try { const paused = !state.latest.bot.paused_servers; await send("/api/pause", { paused }); toast(paused ? "Replies paused. Temporary conversations were cleared." : "Replies resumed."); await refreshStatus(); }
  catch (error) { toast(error.message, true); }
});
$("#warm-button").addEventListener("click", async () => {
  try { await send("/api/warm", {}); toast("Loading the model. You can watch its status here."); await refreshStatus(); }
  catch (error) { toast(error.message, true); }
});
$("#preview-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const prompt = $("#preview-prompt").value.trim(); if (!prompt) return;
  const button = $("#preview-button"), result = $("#preview-result"); button.disabled = true; text("#preview-button", "Generating…"); result.hidden = false; result.textContent = "Waiting for the local model…";
  try { const data = await send("/api/preview", { prompt }, "POST", 245000); result.textContent = data.reply; }
  catch (error) { result.textContent = error.message; toast(error.message, true); }
  finally { button.disabled = false; text("#preview-button", "Try a reply"); refreshStatus(); }
});

async function loadPersona() {
  const form = $("#persona-form"); enableForm(form, false);
  const data = await api("/api/persona");
  for (const name of ["name", "description"]) form.elements.namedItem(name).value = data.persona[name];
  for (const name of ["style_rules", "public_facts"]) form.elements.namedItem(name).value = data.persona[name].join("\n");
  state.personaRevision = data.revision; state.personaDirty = false; state.personaLoaded = true; enableForm(form, true);
}
$("#persona-form").addEventListener("input", () => { state.personaDirty = true; });
$("#persona-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const form = event.currentTarget, persona = {};
  for (const name of ["name", "description"]) persona[name] = form.elements.namedItem(name).value.trim();
  for (const name of ["style_rules", "public_facts"]) persona[name] = form.elements.namedItem(name).value.split("\n").map((line) => line.trim()).filter(Boolean);
  enableForm(form, false);
  try { const data = await send("/api/persona", { revision: state.personaRevision, persona }, "PUT"); state.personaRevision = data.revision; state.personaDirty = false; toast("Personality saved. Temporary conversations were cleared."); refreshStatus(); }
  catch (error) { toast(error.message, true); }
  finally { enableForm(form, true); }
});
$("#forget-button").addEventListener("click", async () => {
  try { await send("/api/forget", {}); toast("Temporary conversations cleared."); refreshStatus(); }
  catch (error) { toast(error.message, true); }
});


let trainingPolling = false;
function trainingTime(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "Waiting";
  if (seconds < 60) return "< 1 min";
  if (seconds < 3600) return `${Math.ceil(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} hours`;
}
function renderTraining(data) {
  const labels = { not_started: "Not prepared", ready: "Ready to start", preparing: "Preparing messages", loading: "Loading model", training: "Training", checkpointing: "Saving checkpoint", pausing: "Saving before pause", paused: "Paused", validating: "Checking held-out messages", exporting: "Exporting model", completed: "Training complete", failed: "Worker stopped", interrupted: "Interrupted", unavailable: "Status unavailable", stopping: "Saving before stop", stopped: "Stopped" };
  const offline = !data.worker_online && !["ready", "not_started", "completed", "stopped"].includes(data.state) && !data.container_running;
  const pendingPause = data.worker_online && data.desired === "paused" && data.state !== "paused";
  const pendingStop = (data.worker_online || data.container_running) && data.desired === "stopped";
  text("#training-state", data.launch_pending ? "Starting training" : pendingStop ? "Stop requested" : pendingPause ? "Pause requested" : offline ? "Worker offline" : labels[data.state] || "Checking");
  const percent = data.total_steps ? Math.min(100, 100 * data.step / data.total_steps) : 0;
  text("#training-percent", `${percent.toFixed(1)}%`);
  $("#training-progress").value = percent;
  text("#training-steps", `${compact(data.step)} / ${compact(data.total_steps)} steps`);
  let detail = data.state === "ready" ? "Prepared and stopped. Choose Start training when you are ready." : data.state === "completed" ? "Training is complete. Your existing bot model stays selected until you import and choose the new model." : offline ? "The worker is not running. Choose Start training to recover the latest complete checkpoint." : data.state === "paused" ? "Progress saved. GPU memory released. Resume when ready." : pendingPause ? "Finishing the current safe step and saving progress. Wait for Paused before stopping Docker." : data.state === "preparing" ? `${compact(data.prepared_examples)} messages tokenized. No training steps yet.` : data.state === "validating" ? "Checking messages that were kept out of training. Validation does not advance the step count." : data.state === "exporting" ? "Saving the final adapter and merged model. The final training checkpoint is already safe." : "Learning from the prepared archive. Progress is saved regularly.";
  if (data.state === "stopped") detail = "Progress saved and worker closed. Start training will recover the latest checkpoint.";
  if (data.container_running && !data.worker_online) detail = "The worker is starting up. Its first progress update will appear shortly.";
  if (data.launch_pending) detail = "Start requested. The local launcher is preparing the background worker.";
  if (pendingStop) detail = "Finishing the current safe step and saving progress, then closing the worker. Wait for Stopped.";
  if (data.launcher_error) detail += ` Launcher: ${data.launcher_error}. Use the WSL fallback below if needed.`;
  if (data.error_code) detail += ` Error type: ${data.error_code}. Recovery keeps your checkpoints.`;
  text("#training-detail", detail);
  text("#training-eta", data.state === "completed" ? "Finished" : data.state === "paused" ? "Paused" : data.state === "stopped" ? "Stopped" : trainingTime(data.eta_seconds));
  text("#training-examples", compact(data.train_examples));
  text("#training-validation", `${compact(data.validation_examples)} held out for validation`);
  text("#training-checkpoint", data.last_checkpoint_step === undefined ? "None yet" : `Step ${compact(data.last_checkpoint_step)}`);
  text("#training-checkpoint-age", data.last_checkpoint_at ? `Saved ${new Date(data.last_checkpoint_at * 1000).toLocaleTimeString()}` : "Every 250 steps or 5 minutes");
  text("#training-loss", Number.isFinite(data.loss) ? data.loss.toFixed(3) : "Waiting");
  text("#training-validation-loss", Number.isFinite(data.validation_loss) ? `Held-out loss ${data.validation_loss.toFixed(3)}` : "Held-out results appear during training");
  $("#training-start").disabled = !data.can_start;
  $("#training-stop").disabled = !data.can_stop;
  text("#training-start", data.step > 0 ? "Start / recover" : "Start training");
  text("#training-launcher", data.launcher_online ? "Local launcher online" : "Local launcher offline");
  $("#training-pause").disabled = !data.can_pause;
  $("#training-resume").disabled = !data.can_resume;
  const points = (data.recent || []).filter((p) => Number.isFinite(p.loss));
  const canvas = $("#training-chart"), context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
  if (points.length > 1) {
    const lo = Math.min(...points.map((p) => p.loss)), hi = Math.max(...points.map((p) => p.loss));
    const first = points[0].step, last = points.at(-1).step;
    context.strokeStyle = "#85e6c1"; context.lineWidth = 2; context.beginPath();
    points.forEach((p, i) => { const x = 12 + (p.step - first) / Math.max(1, last - first) * (canvas.width - 24); const y = canvas.height - 12 - (p.loss - lo) / Math.max(.01, hi - lo) * (canvas.height - 24); if (i) context.lineTo(x, y); else context.moveTo(x, y); });
    context.stroke();
    text("#training-chart-note", `Steps ${compact(first)}–${compact(last)} · loss ${lo.toFixed(3)}–${hi.toFixed(3)} · ${compact(data.truncated_messages)} messages shortened to fit the training window`);
  } else text("#training-chart-note", "The curve appears after a few training updates.");
}
async function refreshTraining() {
  if (trainingPolling) return;
  trainingPolling = true;
  try { renderTraining(await api("/api/training")); }
  catch (error) { text("#training-state", "Connection interrupted"); text("#training-detail", error.message); for (const action of ["start", "stop", "pause", "resume"]) $(`#training-${action}`).disabled = true; }
  finally { trainingPolling = false; }
}
for (const action of ["start", "stop", "pause", "resume"]) {
  $(`#training-${action}`).addEventListener("click", async () => {
    for (const action of ["start", "stop", "pause", "resume"]) $(`#training-${action}`).disabled = true;
    try { renderTraining(await send("/api/training/control", { action })); toast({ start: "Start requested. Launching the background worker.", stop: "Stop requested. Saving before the worker closes.", pause: "Pause requested. Saving at the next safe step.", resume: "Resume requested. Loading the saved checkpoint." }[action]); }
    catch (error) { toast(error.message, true); await refreshTraining(); }
  });
}

enableForm($("#tuning-form"), false); enableForm($("#persona-form"), false);
window.addEventListener("hashchange", () => selectTab(location.hash.slice(1) || "overview"));
selectTab(location.hash.slice(1) || "overview");
refreshStatus();
setInterval(() => { if (!document.hidden) { refreshStatus(); if (state.tab === "training") refreshTraining(); } }, 3000);
