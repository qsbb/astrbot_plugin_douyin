const $ = (selector) => document.querySelector(selector);
let bridge = null;
let ready = false;
let busy = false;
let pauseBusy = false;
let snapshot = null;
let frame = null;
let gesture = null;
let settingsDirty = false;
let lastStatusAt = 0;
let statusBusy = false;
let controlRevision = 0;
let statusError = null;
let frameError = null;
let autoFrameBlocked = false;
let pendingBrowserAction = null;
let pendingWrite = null;
let unsubscribe = null;
let timer = null;
let lastResult = null;
let lastNotice = null;
let receiptRows = [];
let pageHidden = false;
const initialText = Object.fromEntries([...document.querySelectorAll("[data-i18n]")].map((node) => [node.dataset.i18n, node.textContent]));
const WRITES = new Set(["set_like", "post_comment", "share_video", "send_message"]);
const STARTUP_ERRORS = new Set(["PLAYWRIGHT_NOT_INSTALLED", "BROWSER_NOT_INSTALLED", "BROWSER_DEPENDENCIES_MISSING", "BROWSER_LAUNCH_FAILED", "BROWSER_PROFILE_UNWRITABLE", "PROFILE_IN_USE"]);
const RUNTIME_PENDING = new Set(["idle", "checking", "installing_browser", "installing_dependencies", "verifying"]);
const OPERATIONS = {
  browse: [["limit", "number", 1], ["dwell_seconds", "number", 3]],
  search: [["query", "text", ""], ["limit", "number", 5]],
  watch: [["video_ref", "text", ""], ["depth", "depth", "metadata"], ["question", "text", ""]],
  read_comments: [["video_ref", "text", ""], ["limit", "number", 20], ["cursor", "text", ""]],
  resolve_contact: [["query", "text", ""], ["limit", "number", 10]],
  read_inbox: [["conversation_ref", "text", ""], ["limit", "number", 20]],
  set_like: [["video_ref", "text", ""], ["liked", "boolean", true]],
  post_comment: [["video_ref", "text", ""], ["text", "textarea", ""], ["reply_to", "text", ""], ["mentions", "json", "[]"]],
  share_video: [["video_ref", "text", ""], ["target_ref", "text", ""]],
  send_message: [["conversation_ref", "text", ""], ["text", "textarea", ""]],
  task: [["task_id", "text", ""], ["cancel", "boolean", false]],
};
const KEY_NAMES = new Set(["Enter", "Tab", "Backspace", "Delete", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown"]);

function t(key, fallback = "") {
  return bridge?.t(`pages.manager.${key}`, fallback || initialText[key] || key) || fallback || initialText[key] || key;
}

function renderNotice() {
  if (!lastNotice) return;
  const node = $("#notice");
  node.textContent = typeof lastNotice.message === "function" ? lastNotice.message() : lastNotice.message;
  node.className = `notice ${lastNotice.kind}`;
  node.setAttribute("role", lastNotice.kind === "error" ? "alert" : "status");
}

function notice(message, kind = "", resultCode = null) {
  lastNotice = { message, kind, resultCode };
  renderNotice();
}

function outcome(result) {
  if (result?.code === "MANUAL_INPUT_DISPATCHED") return t("inputDispatched");
  const status = result?.status || "ok";
  return t(`state_${status}`, status);
}

function explain(result) {
  const message = t(`error_${result.code}`, result.data?.message || result.code || outcome(result));
  const reason = result.data?.details?.reason;
  return typeof reason === "string" && reason ? `${message} (${reason})` : message;
}

function renderWarnings() {
  for (const [selector, error] of [["#status-warning", statusError], ["#browser-error", frameError]]) {
    const node = $(selector);
    node.hidden = !error;
    node.textContent = error ? error.result ? explain(error.result) : error.message : "";
  }
  $("#frame-state").textContent = frameError ? t("frameUnavailable") : frame ? t("frameLive") : t("noFrame");
}

function runtimePending() {
  return snapshot?.browser_runtime?.managed === true && RUNTIME_PENDING.has(snapshot.browser_runtime.state);
}

function renderRuntime() {
  const runtime = snapshot?.browser_runtime;
  const panel = $("#browser-runtime");
  panel.hidden = !runtime?.managed;
  if (!runtime?.managed) return;
  const preparing = runtimePending();
  panel.classList.toggle("error", runtime.state === "failed");
  panel.classList.toggle("ready", runtime.state === "ready");
  panel.setAttribute("aria-busy", String(preparing));
  $("#runtime-title").textContent = t(runtime.state === "ready" ? "runtimeReady" : "runtimeTitle");
  $("#runtime-message").textContent = runtime.state === "failed" ? t(`error_${runtime.error_code}`, runtime.message || t("runtime_failed")) : t(`runtime_${runtime.state}`, runtime.message || "");
  const progress = $("#runtime-progress");
  progress.hidden = !preparing;
  const percentage = /^(\d{1,3})%$/.exec(runtime.detail || "");
  if (percentage) progress.value = Math.min(100, Number(percentage[1]));
  else progress.removeAttribute("value");
  $("#runtime-retry").hidden = runtime.state !== "failed";
  $("#runtime-retry").disabled = !ready || busy || preparing;
  $("#runtime-details").hidden = runtime.state !== "failed" || !runtime.detail;
  $("#runtime-detail").textContent = runtime.detail || "";
}

function capturePreparation(error, action) {
  const runtime = error.result?.data?.details?.runtime;
  if (runtime) snapshot = { ...snapshot, browser_runtime: runtime };
  if (error.result?.code === "BROWSER_PREPARING") {
    pendingBrowserAction = action;
    frame = null;
    frameError = null;
    autoFrameBlocked = false;
    renderRuntime();
    renderWarnings();
    notice(() => t("runtimeWaiting"), "", "BROWSER_PREPARING");
    pollStatus();
    return true;
  }
  if (runtime?.state === "failed") {
    pendingBrowserAction = action;
    autoFrameBlocked = true;
    renderRuntime();
  }
  return false;
}

function applyControl(result) {
  if (!result.data?.control) return;
  controlRevision += 1;
  snapshot = { ...snapshot, control: result.data.control };
  renderStatus();
}

function withTimeout(promise, milliseconds, message) {
  let timeout;
  return Promise.race([promise, new Promise((_, reject) => {
    timeout = setTimeout(() => reject(new Error(message)), milliseconds);
  })]).finally(() => clearTimeout(timeout));
}

async function api(endpoint, body) {
  if (!ready || document.hidden) throw new Error(t("pageInactive"));
  const response = await withTimeout(
    body === undefined ? bridge.apiGet(`page/${endpoint}`) : bridge.apiPost(`page/${endpoint}`, body),
    65000,
    t("requestTimeout"),
  );
  const value = typeof response === "string" ? JSON.parse(response) : response;
  const result = value?.result;
  if (!result || typeof result !== "object" || !result.data || typeof result.data !== "object") {
    throw new Error(t("invalidResponse"));
  }
  if (result.status === "failed" || result.status === "error") {
    const error = new Error(explain(result));
    error.result = result;
    throw error;
  }
  return result;
}

function updateControls() {
  const owned = snapshot?.control?.owned === true;
  const otherOwner = snapshot?.control?.active === true && !owned;
  for (const button of document.querySelectorAll("button")) button.disabled = !ready || busy;
  $("#refresh-status").disabled = busy;
  $("#toggle-pause").disabled = !ready || !snapshot || pauseBusy;
  for (const node of document.querySelectorAll("[data-owned]")) node.disabled = !ready || busy || !owned || document.hidden;
  $("#open-login").disabled ||= otherOwner;
  $("#open-login").disabled ||= runtimePending() && pendingBrowserAction === "login";
  $("#acquire").disabled ||= owned || otherOwner;
  $("#bind-account").disabled ||= !!statusError || !snapshot?.browser?.authenticated || !snapshot?.config_writable || otherOwner;
  $("#run-operation").disabled ||= !snapshot?.enabled || snapshot?.paused || snapshot?.control?.active;
  $("#run-operation").title = snapshot?.control?.active ? t("releaseBeforeQuick") : "";
  $("#save-settings").disabled ||= !snapshot?.config_writable || otherOwner;
  $("#browser-frame").classList.toggle("inactive", !owned || busy || document.hidden);
  $("#settings-dirty").hidden = !settingsDirty;
  $("#check-receipt").hidden = pendingWrite === null;
  $("#runtime-retry").disabled = !ready || busy || runtimePending();
  if (runtimePending()) {
    $("#send-text").disabled = true;
    $("#send-key").disabled = true;
    for (const button of document.querySelectorAll("[data-scroll]")) button.disabled = true;
  }
}

async function run(task, { silent = false } = {}) {
  if (busy || document.hidden) return;
  busy = true;
  updateControls();
  try {
    await task();
  } catch (error) {
    if (error.result) showResult(error.result);
    if (!silent || error.result?.code === "CONTROL_REQUIRED") notice(() => error.result ? explain(error.result) : error.message || t("operationFailed"), "error", error.result?.code);
    if (["CONTROL_REQUIRED", "CONTROL_BUSY", "SERVICE_CLOSED"].includes(error.result?.code)) {
      frame = null;
      if (snapshot) snapshot.control = { active: false, owned: false };
    }
  } finally {
    busy = false;
    updateControls();
  }
}

function renderSettings() {
  if (settingsDirty || !snapshot?.config) return;
  const config = snapshot.config;
  $("#enabled").checked = config.enabled === true;
  for (const node of document.querySelectorAll('[name="action"]')) node.checked = config.allowed_actions.includes(node.value);
  $("#allowed-targets").value = config.allowed_target_refs.join("\n");
  $("#allowed-origins").value = config.allowed_origins.join("\n");
  $("#allowed-actors").value = config.allowed_actor_ids.join("\n");
}

function renderStatus() {
  if (!snapshot) return;
  $("#account-current").textContent = statusError ? t("accountStatusUnavailable") : snapshot.browser?.authenticated ? snapshot.browser.account_ref : t("notLoggedIn");
  $("#account-bound").textContent = snapshot.expected_account_ref || t("notBound");
  $("#control-state").textContent = snapshot.control?.owned ? t("controlMine") : snapshot.control?.active ? t("controlOther") : t("controlBot");
  $("#plugin-state").textContent = snapshot.paused ? t("paused") : snapshot.enabled ? t("running") : t("disabled");
  $("#toggle-pause").textContent = snapshot.paused ? t("resume") : t("pause");
  renderSettings();
  updateControls();
  renderRuntime();
}

async function refreshStatus() {
  if (statusBusy) return;
  statusBusy = true;
  const revision = controlRevision;
  try {
    const data = (await api("status")).data;
    // 较早发出的账号查询，不能覆盖随后已经确认的接管或归还结果。
    if (revision !== controlRevision) data.control = snapshot?.control;
    const previousRuntime = snapshot?.browser_runtime?.state;
    snapshot = data;
    if (data.browser_runtime?.state === "ready" && previousRuntime !== "ready") {
      autoFrameBlocked = false;
      if (lastNotice?.resultCode === "BROWSER_PREPARING") notice(() => t("runtimeReady"), "success");
    }
    if (data.browser_runtime?.state === "failed") autoFrameBlocked = true;
    statusError = data.browser?.status_available === false && data.browser?.browser_started !== false ? {
      result: { code: data.browser.code || "ACCOUNT_STATUS_UNAVAILABLE", data: data.browser },
    } : null;
  } catch (error) {
    statusError = error;
    throw error;
  } finally {
    statusBusy = false;
    lastStatusAt = Date.now();
    renderStatus();
    renderWarnings();
  }
}

async function refreshFrame() {
  if (!snapshot?.control?.owned) return;
  if (runtimePending() && snapshot.browser_runtime.state !== "idle") {
    pendingBrowserAction ||= "frame";
    renderRuntime();
    return false;
  }
  try {
    const data = (await api("frame")).data;
    if (!/^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(data.image || "") || !Number.isFinite(data.width) || !Number.isFinite(data.height)) {
      throw new Error(t("invalidResponse"));
    }
    const image = $("#browser-frame");
    frame = null;
    image.src = data.image;
    await image.decode();
    frame = { ...data, receivedAt: Date.now() };
    image.hidden = false;
    $("#empty-frame").hidden = true;
    $("#frame-url").textContent = data.url;
    if (autoFrameBlocked && STARTUP_ERRORS.has(lastNotice?.resultCode)) notice(() => t("frameRecovered"), "success");
    frameError = null;
    autoFrameBlocked = false;
  } catch (error) {
    if (capturePreparation(error, pendingBrowserAction || "frame")) return false;
    frame = null;
    frameError = error;
    autoFrameBlocked = STARTUP_ERRORS.has(error.result?.code) || snapshot?.browser_runtime?.state === "failed";
    throw error;
  } finally {
    renderWarnings();
  }
}

function pollStatus() {
  // 账号状态与浏览器画面、输入队列独立；后台查询只更新自己的提示。
  void refreshStatus().catch(() => {});
}

function showResult(result) {
  lastResult = result;
  $("#result-state").textContent = outcome(result);
  $("#result-output").textContent = JSON.stringify(result, null, 2);
}

async function sendInput(kind, params, sourceFrame = frame) {
  if (!snapshot?.control?.owned || !sourceFrame || document.hidden) throw new Error(t("refreshBeforeInput"));
  // 不刷新后再套用旧坐标；发送用户实际看到的那一帧，过期时由后端拒绝。
  frame = null;
  const result = await api("input", { kind, frame_id: sourceFrame.frame_id, ...params });
  notice(() => outcome(result), result.status === "unknown_result" ? "error" : "success");
  await refreshFrame();
}

async function navigate(action) {
  let navigationError;
  try {
    applyControl(await api("control", { action }));
  } catch (error) {
    if (capturePreparation(error, action)) return false;
    navigationError = error;
  }
  if (STARTUP_ERRORS.has(navigationError?.result?.code)) {
    frame = null;
    frameError = navigationError;
    autoFrameBlocked = true;
    renderWarnings();
    pollStatus();
    throw navigationError;
  }
  // 导航失败时浏览器可能已经打开，仍尝试展示现有页面。
  try {
    await refreshFrame();
  } catch (error) {
    navigationError ||= error;
  }
  pollStatus();
  if (navigationError) throw navigationError;
}

function renderFields() {
  const operation = $("#operation").value;
  const values = Object.fromEntries([...$("#operation-fields").querySelectorAll("[data-param]")].map((node) => [node.dataset.param, node.type === "checkbox" ? node.checked : node.value]));
  $("#operation-fields").replaceChildren();
  for (const [name, kind, initial] of OPERATIONS[operation]) {
    const label = document.createElement("label");
    label.htmlFor = `param-${name}`;
    label.textContent = t(`field_${name}`);
    const node = document.createElement(kind === "depth" ? "select" : ["textarea", "json"].includes(kind) ? "textarea" : "input");
    node.id = `param-${name}`;
    node.dataset.param = name;
    node.dataset.kind = kind;
    if (kind === "depth") {
      for (const value of ["metadata", "preview", "full"]) node.add(new Option(t(`depth_${value}`), value));
    } else if (kind === "boolean") node.type = "checkbox";
    else if (kind === "number") {
      node.type = "number";
      node.min = "1";
      node.max = name === "dwell_seconds" ? "15" : operation === "browse" ? String(snapshot?.max_browse_items || 3) : "50";
      node.step = "1";
    } else {
      node.maxLength = ["text", "question"].includes(name) ? 2000 : 512;
      node.autocomplete = "off";
    }
    if (kind === "boolean") node.checked = values[name] ?? initial;
    else node.value = values[name] ?? initial;
    $("#operation-fields").append(label, node);
  }
}

function actionParams() {
  const params = {};
  for (const node of $("#operation-fields").querySelectorAll("[data-param]")) {
    params[node.dataset.param] = node.dataset.kind === "boolean" ? node.checked : node.dataset.kind === "number" ? Number(node.value) : node.dataset.kind === "json" ? JSON.parse(node.value || "[]") : node.value;
  }
  return params;
}

function randomRequestId() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return `page-${[...bytes].map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

async function performOperation() {
  const operation = $("#operation").value;
  const params = actionParams();
  const signature = JSON.stringify({ operation, params });
  if (pendingWrite?.signature === signature) {
    await checkReceipt();
    return;
  }
  const body = { operation, params };
  if (WRITES.has(operation)) {
    body.request_id = randomRequestId();
    pendingWrite = { signature, request_id: body.request_id };
    $("#request-reference").textContent = body.request_id;
  }
  let result;
  try {
    result = await api("action", body);
  } catch (error) {
    if (error.result?.status === "failed" && WRITES.has(operation)) {
      pendingWrite = null;
      $("#request-reference").textContent = "";
    }
    throw error;
  }
  showResult(result);
  notice(() => `${outcome(result)} · ${result.code}`, result.status === "unknown_result" ? "error" : "success");
  if (WRITES.has(operation)) await refreshReceipts();
  if (snapshot?.control?.owned) await refreshFrame();
}

async function checkReceipt() {
  if (!pendingWrite) return;
  const result = await api("action", { operation: "receipt", params: {}, request_id: pendingWrite.request_id });
  showResult(result);
  notice(() => outcome(result), result.status === "unknown_result" ? "error" : "success");
}

async function refreshReceipts() {
  receiptRows = (await api("receipts")).data.receipts || [];
  renderReceipts();
}

function renderReceipts() {
  const rows = receiptRows;
  const container = $("#receipts");
  container.replaceChildren();
  if (!rows.length) {
    const node = document.createElement("p");
    node.textContent = t("noReceipts");
    container.append(node);
    return;
  }
  for (const row of rows) {
    const item = document.createElement("details");
    item.className = "receipt";
    const summary = document.createElement("summary");
    const label = document.createElement("span");
    label.textContent = row.code || row.operation || row.request_id || t("receipt");
    const state = document.createElement("span");
    state.className = "pill";
    state.textContent = outcome(row);
    summary.append(label, state);
    const content = document.createElement("pre");
    content.textContent = JSON.stringify(row, null, 2);
    item.append(summary, content);
    container.append(item);
  }
}

function renderLocale() {
  document.documentElement.lang = bridge?.getLocale() || "zh-CN";
  document.title = t("title");
  for (const node of document.querySelectorAll("[data-i18n]")) {
    if (node.id !== "notice" || !lastNotice) node.textContent = t(node.dataset.i18n);
  }
  for (const [attribute, target] of [["i18nAria", "aria-label"], ["i18nPlaceholder", "placeholder"], ["i18nAlt", "alt"]]) {
    for (const node of document.querySelectorAll(`[data-${attribute.replace(/[A-Z]/g, (value) => `-${value.toLowerCase()}`)}]`)) node.setAttribute(target, t(node.dataset[attribute], node.getAttribute(target)));
  }
  const selected = $("#operation").value || "browse";
  $("#operation").replaceChildren(...Object.keys(OPERATIONS).map((name) => new Option(t(`op_${name}`), name)));
  $("#operation").value = selected;
  renderFields();
  renderStatus();
  renderNotice();
  renderWarnings();
  renderRuntime();
  renderReceipts();
  if (lastResult) showResult(lastResult);
}

function pointerPoint(event) {
  const rect = $("#browser-frame").getBoundingClientRect();
  return {
    x: Math.min(frame.width - 1, Math.max(0, (event.clientX - rect.left) * frame.width / rect.width)),
    y: Math.min(frame.height - 1, Math.max(0, (event.clientY - rect.top) * frame.height / rect.height)),
  };
}

const screen = $("#browser-frame");
screen.addEventListener("pointerdown", (event) => {
  if (busy || !frame || !snapshot?.control?.owned || document.hidden || event.button !== 0) return;
  event.preventDefault();
  screen.focus({ preventScroll: true });
  screen.setPointerCapture(event.pointerId);
  gesture = { pointerId: event.pointerId, frame, points: [pointerPoint(event)] };
});
screen.addEventListener("pointermove", (event) => {
  if (!gesture || event.pointerId !== gesture.pointerId || !frame) return;
  const point = pointerPoint(event);
  const previous = gesture.points.at(-1);
  if (Math.hypot(point.x - previous.x, point.y - previous.y) >= 4 && gesture.points.length < 99) gesture.points.push(point);
});
screen.addEventListener("pointerup", (event) => {
  if (!gesture || event.pointerId !== gesture.pointerId) return;
  const current = gesture;
  gesture = null;
  if (screen.hasPointerCapture(event.pointerId)) screen.releasePointerCapture(event.pointerId);
  if (document.hidden) return;
  if (current.points.length > 1 && frame) current.points.push(pointerPoint(event));
  run(() => sendInput(current.points.length > 1 ? "drag" : "click", current.points.length > 1 ? { points: current.points } : current.points[0], current.frame));
});
screen.addEventListener("pointercancel", () => { gesture = null; });
screen.addEventListener("contextmenu", (event) => event.preventDefault());
screen.addEventListener("wheel", (event) => {
  if (!snapshot?.control?.owned || document.hidden) return;
  event.preventDefault();
  if (busy || gesture || !frame) return;
  const scale = event.deltaMode === 1 ? 20 : event.deltaMode === 2 ? frame.height : 1;
  const clamp = (value) => Math.max(-2000, Math.min(2000, value * scale));
  run(() => sendInput("scroll", { delta_x: clamp(event.deltaX), delta_y: clamp(event.deltaY) }));
}, { passive: false });
screen.addEventListener("keydown", (event) => {
  if (busy || !snapshot?.control?.owned || !frame || document.hidden) return;
  let key = event.key;
  if ((event.ctrlKey || event.metaKey) && key.toLowerCase() === "a") key = event.metaKey ? "Meta+A" : "Control+A";
  else if (event.ctrlKey || event.metaKey || event.altKey) return;
  else if (key === "Tab" && event.shiftKey) key = "Shift+Tab";
  else if (key === " ") key = "Space";
  if (!KEY_NAMES.has(key) && !["Meta+A", "Control+A", "Shift+Tab", "Space"].includes(key)) return;
  event.preventDefault();
  run(() => sendInput("key", { key }));
});

$("#open-login").addEventListener("click", () => run(async () => {
  if (!snapshot?.control?.owned) applyControl(await api("control", { action: "acquire" }));
  pendingBrowserAction = null;
  if (await navigate("login") !== false) notice(() => t("loginReady"), "success");
}));
$("#acquire").addEventListener("click", () => run(async () => {
  applyControl(await api("control", { action: "acquire" }));
  try { await refreshFrame(); }
  finally { pollStatus(); }
  notice(() => t("controlMine"), "success");
}));
$("#release").addEventListener("click", () => run(async () => {
  pendingBrowserAction = null;
  applyControl(await api("control", { action: "release" }));
  frame = null;
  frameError = null;
  renderWarnings();
  pollStatus();
  notice(() => t("released"), "success");
}));
$("#runtime-retry").addEventListener("click", () => run(async () => {
  if (pendingBrowserAction && !snapshot?.control?.owned) {
    if (snapshot?.control?.active) pendingBrowserAction = null;
    else applyControl(await api("control", { action: "acquire" }));
  }
  const result = await api("prepare", {});
  snapshot = { ...snapshot, browser_runtime: result.data.browser_runtime };
  frameError = null;
  autoFrameBlocked = false;
  renderRuntime();
  renderWarnings();
  notice(() => t("runtimeWaiting"), "", "BROWSER_PREPARING");
  pollStatus();
}));
for (const button of document.querySelectorAll("[data-control]")) button.addEventListener("click", () => run(() => navigate(button.dataset.control)));
for (const button of document.querySelectorAll("[data-scroll]")) button.addEventListener("click", () => run(() => sendInput("scroll", { delta_y: Number(button.dataset.scroll) })));
$("#refresh-frame").addEventListener("click", () => run(refreshFrame));
$("#send-text").addEventListener("click", () => run(async () => {
  const text = $("#remote-text").value;
  if (!text) return;
  await sendInput("text", { text });
  $("#remote-text").value = "";
}));
$("#send-key").addEventListener("click", () => run(() => sendInput("key", { key: $("#remote-key").value })));
$("#bind-account").addEventListener("click", () => run(async () => {
  await api("bind", {});
  await refreshStatus();
  notice(() => t("accountSaved"), "success");
}));
$("#toggle-pause").addEventListener("click", async () => {
  if (pauseBusy || !snapshot) return;
  pauseBusy = true;
  updateControls();
  try {
    const desired = !snapshot.paused;
    const result = await api("pause", { paused: desired });
    showResult(result);
    if (typeof result.data.paused === "boolean") snapshot.paused = result.data.paused;
    else snapshot.paused = desired;
    if (!busy) await run(refreshStatus);
    renderStatus();
  } catch (error) { notice(error.message, "error"); }
  finally { pauseBusy = false; updateControls(); }
});
$("#operation").addEventListener("change", () => {
  $("#operation-fields").replaceChildren();
  renderFields();
});
$("#quick-form").addEventListener("submit", (event) => { event.preventDefault(); run(performOperation); });
$("#check-receipt").addEventListener("click", () => run(checkReceipt));
$("#refresh-receipts").addEventListener("click", () => run(refreshReceipts));
$("#settings-form").addEventListener("input", () => { settingsDirty = true; updateControls(); });
$("#settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  run(async () => {
    const lines = (selector) => [...new Set($(selector).value.split(/\r?\n/).map((value) => value.trim()).filter(Boolean))];
    await api("settings", { config: {
      enabled: $("#enabled").checked,
      allowed_actions: [...document.querySelectorAll('[name="action"]:checked')].map((node) => node.value),
      allowed_target_refs: lines("#allowed-targets"),
      allowed_origins: lines("#allowed-origins"),
      allowed_actor_ids: lines("#allowed-actors"),
    } });
    settingsDirty = false;
    await refreshStatus();
    notice(() => t("settingsSaved"), "success");
  });
});

async function initialize() {
  const deadline = Date.now() + 8000;
  while (!window.AstrBotPluginPage && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 50));
  bridge = window.AstrBotPluginPage;
  if (!bridge?.ready || !bridge.apiGet || !bridge.apiPost) throw new Error(t("bridgeMissing", "请从 AstrBot 插件详情中的 Page 打开此页面。"));
  await withTimeout(bridge.ready(), 8000, t("bridgeMissing", "页面未连接 AstrBot，请重新打开插件 Page。"));
  ready = true;
  renderLocale();
  if (!unsubscribe) unsubscribe = bridge.onContext?.(renderLocale);
  await refreshStatus().catch(() => {});
  await refreshReceipts();
  notice(() => t("connected"), "success");
}

$("#refresh-status").addEventListener("click", () => run(ready ? refreshStatus : initialize));
document.addEventListener("visibilitychange", () => {
  if (document.hidden) pendingBrowserAction = null;
  gesture = null;
  frame = null;
  $("#remote-text").value = "";
  updateControls();
  if (!document.hidden && ready) {
    run(refreshFrame);
    pollStatus();
  }
});
window.addEventListener("pagehide", () => {
  pendingBrowserAction = null;
  pageHidden = true;
  clearTimeout(timer);
  timer = null;
  gesture = null;
  frame = null;
  $("#remote-text").value = "";
  unsubscribe?.();
  unsubscribe = null;
});
window.addEventListener("pageshow", (event) => {
  if (!event.persisted && !pageHidden) return;
  pageHidden = false;
  if (ready && !unsubscribe) unsubscribe = bridge.onContext?.(renderLocale);
  if (ready && !document.hidden) {
    run(refreshFrame);
    pollStatus();
  }
  if (timer === null) timer = setTimeout(tick, 2000);
});

async function tick() {
  if (!document.hidden && ready && !busy && !gesture) {
    if (pendingBrowserAction && snapshot?.control?.owned && ["ready", "external"].includes(snapshot?.browser_runtime?.state)) {
      const action = pendingBrowserAction;
      pendingBrowserAction = null;
      await run(async () => {
        if (action === "frame") await refreshFrame();
        else if (await navigate(action) !== false) notice(() => t("loginReady"), "success");
      });
    } else if (!runtimePending() && $("#auto-refresh").checked && snapshot?.control?.owned && !autoFrameBlocked) await run(refreshFrame, { silent: true });
    if (Date.now() - lastStatusAt > (runtimePending() ? 1500 : 15000)) pollStatus();
  }
  if (!pageHidden) timer = setTimeout(tick, 2000);
}

await run(initialize);
timer = setTimeout(tick, 2000);
