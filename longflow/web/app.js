/* LongFlow engineering console. UI records are not execution or release evidence. */
import { createSafetyPanel, renderDevelopmentTaskForm } from "./safety.js";
import { createFileWorkspace } from "./files.js";
import { t, toggleLocale, localeLabel, localeTitle, translateDynamic } from "./i18n.js";

const state = { view: "tasks", detailId: null, safety: null, revision: 0, controller: null };
const STATUS = {
  pending: "待处理", ready: "就绪", in_progress: "进行中", waiting_user: "待用户处理",
  waiting_approval: "待审批", completed: "已完成", partially_completed: "部分完成",
  failed: "失败", cancelled: "已取消",
};
const $ = (selector, root = document) => root.querySelector(selector);
const array = value => Array.isArray(value) ? value : [];
const object = value => value && typeof value === "object" && !Array.isArray(value) ? value : {};
const text = value => value == null ? "未提供" : typeof value === "string" ? value : JSON.stringify(value, null, 2);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2).toLowerCase(), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(typeof child === "string" || typeof child === "number" ? document.createTextNode(child) : child);
  }
  return node;
}
function chip(status) { return el("span", { class: "status-chip", "data-status": status || "unknown" }, STATUS[status] || status || "未知"); }
function button(label, action, primary = false) {
  return el("button", { type: "button", class: `button ${primary ? "button-primary" : "button-quiet"}`, onClick: action }, label);
}
function header(eyebrow, title, context, ...actions) {
  return el("header", { class: "page-header" }, el("div", {}, el("p", { class: "eyebrow" }, eyebrow),
    el("h1", {}, title), el("p", { class: "muted" }, context)), el("div", {}, ...actions));
}
function panel(title, ...children) {
  return el("section", { class: "panel" }, el("div", { class: "panel-heading" }, el("h2", {}, title)), ...children);
}
function table(headings, rows, label) {
  return el("div", { class: "task-table" }, el("table", { "aria-label": label },
    el("thead", {}, el("tr", {}, headings.map(title => el("th", { scope: "col" }, title)))),
    el("tbody", {}, rows.map(cells => el("tr", {}, cells.map(cell => el("td", {}, cell)))))));
}
function authHeaders() {
  const headers = {};
  try {
    const token = localStorage.getItem("longflow.token"), workspace = localStorage.getItem("longflow.workspace");
    if (token) headers.Authorization = `Bearer ${token}`;
    if (workspace) headers["X-Workspace"] = workspace;
  } catch { /* Storage can be disabled. */ }
  return headers;
}
async function api(path, options = {}) {
  const { responseFormat, ...request } = options;
  const headers = { ...authHeaders(), ...(request.headers || {}) };
  const isForm = typeof FormData !== "undefined" && request.body instanceof FormData;
  if (request.body && !isForm) headers["Content-Type"] = "application/json";
  let response;
  try { response = await fetch(path, { ...request, headers }); }
  catch (error) { if (error.name === "AbortError") throw error; throw new Error("无法连接后端服务"); }
  const raw = await response.text();
  let data;
  try { data = raw ? JSON.parse(raw) : {}; } catch { data = { raw }; }
  if (!response.ok) {
    if (response.status === 405 && path.startsWith("/api/local-workspaces")) {
      throw new Error("当前后端尚未加载文件工作区接口。请在服务终端按 Ctrl+C，重新启动服务后刷新页面。");
    }
    const detail = data?.detail;
    throw new Error(text(detail?.error || detail || data?.error || `HTTP ${response.status}`));
  }
  return responseFormat === "text" ? raw : data;
}
function toast(message, kind = "info") {
  const wrap = $("#toast-wrap");
  if (!wrap) return;
  const item = el("div", { class: `notice ${kind}`, role: kind === "error" ? "alert" : "status" }, text(message));
  wrap.append(item); setTimeout(() => item.remove(), 5000);
}
function applyLocale() {
  document.documentElement.lang = localeLabel() === "中" ? "en" : "zh-CN";
  document.querySelectorAll("[data-i18n]").forEach(node => {
    const key = node.dataset.i18n;
    const value = t(key);
    if (key === "boundaryText") { node.replaceChildren(); value.split("\\n").forEach((part, index, parts) => { node.append(document.createTextNode(part)); if (index < parts.length - 1) node.append(document.createElement("br")); }); }
    else node.textContent = value;
  });
  const toggle = $("#locale-toggle");
  if (toggle) { toggle.textContent = localeLabel(); toggle.title = localeTitle(); toggle.setAttribute("aria-label", localeTitle()); }
}
function setView(view, detailId = null, navigate = true) {
  if (!["files", "tasks", "new", "detail"].includes(view) || (view === "detail" && !detailId)) view = "tasks";
  state.view = view; state.detailId = view === "detail" ? detailId : null; state.safety = null;
  if (navigate) {
    const hash = view === "detail" ? `#task/${encodeURIComponent(detailId)}` : `#${view}`;
    if (location.hash !== hash) history.pushState(null, "", hash);
  }
  document.querySelectorAll("[data-view]").forEach(nav => {
    const active = nav.dataset.view === (view === "detail" ? "tasks" : view);
    nav.classList.toggle("active", active);
    if (active) nav.setAttribute("aria-current", "page"); else nav.removeAttribute("aria-current");
  });
  render();
}
async function renderTasks(app, current, signal) {
  app.append(header("ENGINEERING / WORKSPACE", "工程任务控制台", "当前授权工作区的根任务 · 状态来自服务端，不代表测试或发布通过。",
    button("刷新", render), button("新建任务", () => setView("new"), true)));
  const loading = el("p", { class: "notice", role: "status" }, "正在读取任务…"); app.append(loading);
  try {
    const data = await api("/api/tasks", { signal });
    if (!current()) return;
    const tasks = array(Array.isArray(data) ? data : data.tasks || data.items);
    const roots = tasks.filter(task => task.kind === "goal" || !task.parent_id);
    loading.remove();
    const counts = [
      ["任务总数", roots.length], ["进行中", roots.filter(t => t.status === "in_progress").length],
      ["等待用户 / 审批", roots.filter(t => ["waiting_user", "waiting_approval"].includes(t.status)).length],
      ["已完成（任务状态）", roots.filter(t => t.status === "completed").length],
    ];
    app.append(el("section", { class: "metric-grid", "aria-label": "根任务统计" }, counts.map(([name, count]) =>
      el("div", { class: "metric" }, el("span", { class: "muted" }, name), el("strong", {}, count)))));
    if (!roots.length) { app.append(panel("任务队列", el("div", { class: "empty-state" }, "暂无任务。创建目标后，再确认开发契约与候选范围。"))); return; }
    app.append(panel("任务队列", table(["目标 / 任务", "工作区", "角色", "状态", "操作"], roots.map(task => [
      el("div", {}, el("strong", {}, task.objective || task.title || task.id), el("p", { class: "muted" }, task.id)),
      task.repository || task.workspace || "未提供", task.agent_role || "未提供", chip(task.status),
      button("查看任务", () => setView("detail", task.id)),
    ]), "工程任务队列")));
  } catch (error) {
    if (!current() || error.name === "AbortError") return;
    loading.replaceWith(el("div", { class: "empty-state", role: "alert" }, "任务加载失败：", error.message, button("重试", render)));
  }
}
function renderNew(app, current) {
  // Translate the retained form's aliases without changing safety.js.
  const aliases = { "page-head": "page-header", sub: "muted", card: "panel", "btn primary": "button button-primary", btn: "button button-quiet" };
  renderDevelopmentTaskForm(app, {
    el: (tag, attrs = {}, ...children) => el(tag, { ...attrs, class: aliases[attrs.class] || attrs.class }, ...children),
    api, errorHint: error => error.message,
    onCreated: id => { if (current()) setView(id ? "detail" : "tasks", id); },
    onBack: () => { if (current()) setView("tasks"); },
  });
  $(".page-header", app)?.prepend(el("p", { class: "eyebrow" }, "ENGINEERING / NEW REQUEST"));
}
function renderAgents(app, detail, root) {
  const children = array(detail.subtasks || detail.children || detail.tasks || root.subtasks);
  app.append(panel("执行节点 / Agent", children.length ? table(["任务", "角色", "状态"], children.map(task => [
    task.title || task.objective || task.id, task.agent_role || task.kind || "未提供", chip(task.status),
  ]), "Agent 执行节点") : el("p", { class: "empty-state" }, "暂无子任务记录；未推断 Agent 已启动。")));
}
function renderApprovals(app, detail) {
  const approvals = array(detail.approvals);
  app.append(panel("审批记录", approvals.length ? table(["工具", "状态", "原因 / 决策者"], approvals.map(a => [
    a.tool_name || "未提供", chip(a.status), text(a.reason || a.decided_by),
  ]), "工具审批记录") : el("p", { class: "empty-state" }, "暂无审批记录；不代表已授权。")));
}
function createStages() {
  const strip = el("section", { class: "stage-strip", "aria-label": "工程阶段记录" });
  const values = {};
  for (const [key, name] of [["contract", "契约"], ["candidate", "候选"], ["test", "测试"], ["verify", "验证"], ["release", "发布"]]) {
    values[key] = el("p", { class: "muted" }, "未提供记录");
    strip.append(el("div", { class: "stage", "data-stage": key }, el("span", { class: "eyebrow" }, key.toUpperCase()), el("h3", {}, name), values[key]));
  }
  function update(payload) {
    const safety = object(payload), contract = object(safety.contract), progress = object(safety.progress);
    values.contract.textContent = contract.confirmed_by ? `服务端确认记录：${text(contract.confirmed_by)} · ${text(contract.confidence)}（不代表发布授权）` : "未提供用户确认记录";
    values.candidate.textContent = Array.isArray(safety.candidates) ? `${safety.candidates.length} 个候选副本记录（未执行）` : "未提供候选记录";
    values.test.textContent = "测试执行器未接入；不将候选生成视为测试";
    values.verify.textContent = progress.verification_status == null ? "未提供验证记录" : `服务端状态：${text(progress.verification_status)}（需独立证据）`;
    values.release.textContent = "未接入受控生产执行器；无发布成功证据";
  }
  return { element: strip, update };
}
function renderCandidateActions(app, root, current, onGenerated) {
  const input = el("textarea", { class: "code-editor", rows: "12", "aria-label": "候选变更 JSON", placeholder: '{"relative/path.js":"完整文件内容"}' });
  const diff = el("pre", { class: "code-output", "aria-label": "统一差异" }, "尚未读取候选差异。");
  const metadata = el("div", { class: "muted" });
  const raw = el("pre", { class: "code-output" }, "尚无 API 响应。");
  const selection = el("select", { "aria-label": "服务端候选记录" });
  selection.append(el("option", { value: "" }, "选择候选记录"));
  let previewRevision = 0;
  function show(result) {
    const data = object(result);
    diff.textContent = typeof data.unified_diff === "string" ? data.unified_diff || "服务端返回空差异。" : "响应未提供 unified_diff；不推断没有变更。";
    metadata.replaceChildren(el("p", {}, `候选：${text(data.candidate_id)} · 能力：${text(data.capability)}`),
      el("p", {}, `基础 SHA：${text(data.base_sha)}\n候选 SHA：${text(data.candidate_sha)}`));
    raw.textContent = JSON.stringify(result, null, 2);
  }
  function setCandidates(candidates) {
    const previous = selection.value;
    selection.replaceChildren(el("option", { value: "" }, "选择候选记录"));
    array(candidates).forEach(c => { if (c?.candidate_id) selection.append(el("option", { value: c.candidate_id }, c.candidate_id)); });
    selection.value = previous;
  }
  selection.addEventListener("change", async () => {
    const revision = ++previewRevision, id = selection.value;
    if (!id) return;
    try {
      const result = await api(`/api/tasks/${encodeURIComponent(root.id)}/candidates/${encodeURIComponent(id)}`);
      if (current() && revision === previewRevision) show(result);
    } catch (error) { if (current() && revision === previewRevision) toast(error.message, "error"); }
  });
  const generate = button("生成候选副本", async () => {
    if (!current() || generate.disabled) return;
    const revision = ++previewRevision;
    try {
      const changes = JSON.parse(input.value || "{}");
      if (!changes || typeof changes !== "object" || Array.isArray(changes) || Object.values(changes).some(value => typeof value !== "string" && value !== null))
        throw new Error("候选 JSON 必须是路径到文件内容（文字或 null）的对象。");
      generate.disabled = true;
      const result = await api(`/api/tasks/${encodeURIComponent(root.id)}/candidates`, { method: "POST", body: JSON.stringify({ changes }) });
      if (!current()) return;
      if (revision === previewRevision) show(result);
      toast("候选请求已返回；未执行测试、应用或发布。", "info");
      onGenerated();
    } catch (error) { if (current()) toast(error.message, "error"); }
    finally { generate.disabled = false; }
  }, true);
  app.append(panel("候选工作区", el("p", { class: "notice" }, "仅生成服务端候选副本。完整契约和权限由后端校验；这里不提供测试、合并或发布操作。"),
    selection, el("div", { class: "editor-grid" },
      el("section", {}, el("h3", {}, "变更输入 / JSON"), input, generate),
      el("section", {}, el("h3", {}, "结构化预览 / Unified diff"), metadata, diff)),
    el("details", {}, el("summary", {}, "原始 API 响应（与差异分开）"), raw)));
  return { setCandidates };
}
function renderAudit(app, events) {
  const rows = array(events);
  const list = el("ol", { class: "audit-list" });
  rows.forEach(event => {
    const entry = object(event);
    list.append(el("li", { class: "audit-item" }, el("div", {}, el("strong", {}, text(entry.type || entry.event_type || entry.kind)),
      el("span", { class: "muted" }, ` · ${text(entry.created_at || entry.timestamp)} · ${text(entry.actor)}`)),
      el("pre", { class: "code-output" }, text(entry.detail ?? event))));
  });
  app.append(panel(`审计时间线 · ${rows.length} 条记录`, rows.length ? list : el("p", { class: "empty-state" }, "暂无事件记录。")));
}
async function renderDetail(app, current, signal) {
  const id = state.detailId;
  const loading = el("p", { class: "notice", role: "status" }, "正在读取任务详情…"); app.append(loading);
  try {
    const detail = await api(`/api/tasks/${encodeURIComponent(id)}`, { signal });
    if (!current()) return;
    loading.remove();
    const root = detail.task || detail.root || detail;
    app.append(header("ENGINEERING / TASK", root.objective || root.title || "开发任务", `${text(root.id)} · ${text(root.workspace)} · 任务状态不是验证证据`,
      chip(root.status), button("返回任务", () => setView("tasks")), button("刷新", render)));
    const stages = createStages(); stages.update(detail.safety); app.append(stages.element);
    let candidate;
    const safetyApi = async (path, options) => {
      if (!current()) throw new Error("任务视图已切换，请在当前任务重新操作。");
      const result = await api(path, options);
      if (!current()) throw new Error("任务视图已切换。");
      if (path.endsWith("/safety") && (!options?.method || options.method === "GET")) {
        stages.update(result); candidate?.setCandidates(result.candidates);
      }
      return result;
    };
    const safety = createSafetyPanel(root.id, { api: safetyApi, errorHint: error => error.message });
    state.safety = { id: root.id, ...safety }; app.append(safety.element);
    const grid = el("div", { class: "detail-grid" }); renderAgents(grid, detail, root); renderApprovals(grid, detail); app.append(grid);
    candidate = renderCandidateActions(app, root, current, () => { if (current()) void safety.refresh(); });
    renderAudit(app, detail.events || root.events);
    void safety.refresh(detail.events);
  } catch (error) {
    if (!current() || error.name === "AbortError") return;
    loading.replaceWith(el("div", { class: "empty-state", role: "alert" }, `详情加载失败：${error.message}`, button("返回任务", () => setView("tasks"))));
  }
}
function render() {
  const app = $("#app"); if (!app) return;
  state.controller?.abort(); state.controller = new AbortController();
  const revision = ++state.revision, signal = state.controller.signal;
  const current = () => revision === state.revision;
  state.safety = null; app.replaceChildren();
  if (state.view === "files") app.append(createFileWorkspace({ api, onError: error => toast(error.message || String(error), "error") }));
  else if (state.view === "tasks") void renderTasks(app, current, signal);
  else if (state.view === "new") renderNew(app, current);
  else void renderDetail(app, current, signal);
}
function restoreFromHash() {
  const match = location.hash.match(/^#task\/(.+)$/);
  let id = null;
  try { if (match) id = decodeURIComponent(match[1]); } catch { /* Invalid fragments fall back to tasks. */ }
  setView(id ? "detail" : location.hash === "#new" ? "new" : location.hash === "#tasks" ? "tasks" : "files", id, false);
}
document.querySelectorAll('[data-view="tasks"], [data-view="new"], [data-view="files"]').forEach(nav => nav.addEventListener("click", () => setView(nav.dataset.view)));
$("#brand")?.addEventListener("click", () => setView("tasks"));
window.addEventListener("popstate", restoreFromHash);
window.addEventListener("hashchange", restoreFromHash);
restoreFromHash();
applyLocale();
translateDynamic(document.body);
$("#locale-toggle")?.addEventListener("click", () => { toggleLocale(); applyLocale(); render(); translateDynamic(document.body); });
(async () => {
  try {
    const health = await api("/api/health");
    if (health.ok !== true) throw new Error("服务端未报告健康状态");
    $("#health")?.classList.add("ok");
    if ($("#health-text")) $("#health-text").textContent = health.degraded_to_local ? `${t("connected")} · local fallback` : t("connected");
  } catch { if ($("#health-text")) $("#health-text").textContent = t("disconnected"); }
})();
