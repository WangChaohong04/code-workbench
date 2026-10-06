/* Software-development safety controls. Server evidence, never optimistic success. */
const object = value => value && typeof value === "object" && !Array.isArray(value) ? value : {};
const text = value => value == null ? "未知 / 未提供" : typeof value === "string" ? value : JSON.stringify(value, null, 2);
export function normalizeSafety(payload) {
  const raw = object(payload);
  const s = object(raw.safety || raw.snapshot || raw);
  const control = object(s.control || s.global);
  const frozen = s.frozen ?? control.frozen;
  const progress = object(s.progress);
  return { raw, contract: object(s.contract), frozen: frozen === true || frozen === 1 ? true : frozen === false || frozen === 0 ? false : null,
    epoch: s.epoch ?? control.epoch, execution: s.execution ?? s.execution_status ?? progress.execution_status,
    verification: s.verification ?? s.verification_status ?? progress.verification_status,
    recovery: s.recovery ?? { code: progress.code_recovery ?? null, config: progress.config_recovery ?? null, database: progress.data_recovery ?? null },
    unknowns: s.unknowns ?? s.unknown_items, logs: s.logs ?? s.events, diff: s.diff ?? s.diffs ?? s.candidates, deployment: s.deployment ?? s.production };
}
export function validateContract(value) {
  const c = object(value);
  const keys = ["repository", "branch", "allowed_dirs", "forbidden_dirs", "allow_dependencies", "allow_config", "database", "environment", "acceptance", "required_tests", "constraints", "stop_conditions"];
  for (const k of keys) if (!(k in c)) throw new Error(`开发契约缺少字段：${k}`);
  for (const k of ["repository", "branch", "database", "environment"]) if (typeof c[k] !== "string" || !c[k].trim()) throw new Error(`${k} 必须是非空文字；未知时请先调查，不要批准。`);
  for (const k of ["allowed_dirs", "forbidden_dirs", "acceptance", "required_tests", "constraints", "stop_conditions"]) {
    if (!Array.isArray(c[k]) || c[k].some(v => typeof v !== "string" || !v.trim())) throw new Error(`${k} 必须是非空文字组成的数组。`);
  }
  for (const k of ["allowed_dirs", "acceptance", "required_tests", "stop_conditions"]) if (!c[k].length) throw new Error(`${k} 不得为空。`);
  for (const k of ["allow_dependencies", "allow_config"]) if (typeof c[k] !== "boolean") throw new Error(`${k} 必须是 true 或 false。`);
  return Object.fromEntries([...keys.map(k => [k, c[k]]), ["confirmed", true]]);
}
const draftTemplate = { repository: "", branch: "", allowed_dirs: [], forbidden_dirs: [], allow_dependencies: false, allow_config: false,
  database: "", environment: "", acceptance: [], required_tests: [], constraints: [], stop_conditions: [] };
export function createSafetyPanel(taskId, { api, errorHint = e => String(e.message || e), confirmAction = message => window.confirm(message) }) {
  function node(tag, content, attrs = {}) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (content != null) n.textContent = String(content);
    return n;
  }
  const root = node("section", null, { class: "card", "data-safety-panel": "", "aria-label": "软件开发安全工作台" });
  root.append(node("h2", "软件开发安全工作台"), node("p", "AI 提议 → 用户批准 → 工具执行 → 系统验证。批准不等于执行，执行不等于验证通过。"));
  const notice = node("p", "正在加载安全快照…", { role: "status", "aria-live": "polite" });
  const errors = node("p", "", { role: "alert", class: "note-box warn" });
  const evidence = node("div");
  const reason = node("input", null, { type: "text", placeholder: "全局冻结 / 解冻原因（必填）", "aria-label": "安全控制原因" });
  const freeze = node("button", "全局冻结", { type: "button", class: "btn danger" });
  const unfreeze = node("button", "解冻（不恢复审批）", { type: "button", class: "btn" });
  const refreshButton = node("button", "刷新安全快照", { type: "button", class: "btn" });
  root.append(notice, errors, evidence, reason, freeze, unfreeze, refreshButton);
  root.append(node("p", "冻结影响所有任务并撤销旧审批；已经运行的副作用不保证可中断。解冻不会恢复任何旧审批，必须重新审批。"));
  const editor = node("details");
  editor.append(node("summary", "查看 / 编辑开发契约（JSON，用户确认后提交）"));
  const draft = node("textarea", null, { rows: "18", "aria-label": "开发契约 JSON", "data-draft-key": `${taskId}:safety-contract`, style: "width:100%;font-family:monospace" });
  draft.value = JSON.stringify(draftTemplate, null, 2);
  let dirty = false, busy = false, loading = false, snapshot = null;
  draft.addEventListener("input", () => { dirty = true; });
  const acknowledge = node("input", null, { type: "checkbox" });
  const label = node("label");
  label.append(acknowledge, document.createTextNode("我已审阅范围、测试与停止条件；确认契约不等于批准发布或数据库恢复。"));
  const submit = node("button", "确认开发契约", { type: "button", class: "btn primary" });
  editor.append(draft, label, submit);
  root.append(editor);
  const path = `/api/tasks/${encodeURIComponent(taskId)}`;
  const detail = (title, value) => {
    const d = node("details");
    d.append(node("summary", title), node("pre", text(value), { style: "white-space:pre-wrap;overflow-wrap:anywhere" }));
    return d;
  };
  function paint(s) {
    snapshot = s;
    notice.textContent = `全局冻结：${s.frozen === null ? "未知（不能判定安全）" : s.frozen ? "已冻结" : "未冻结（不代表已批准）"} · epoch：${text(s.epoch)}`;
    evidence.replaceChildren();
    evidence.append(detail("1 · AI 提议 / 当前契约", s.contract),
      detail("2 · 用户批准（仅服务端记录；冻结后旧审批无效）", { confirmed_by: s.contract.confirmed_by ?? null, confirmed_epoch: s.contract.confirmed_epoch ?? null, revision: s.contract.revision ?? null }),
      detail("3 · 工具执行（原始记录，不据此宣称成功）", s.execution),
      detail("4 · 系统验证（无验证证据不得视为通过）", s.verification));
    const eng = object(s.contract.engineering || s.contract);
    evidence.append(detail("允许范围 / 禁止范围", { allowed_dirs: eng.allowed_dirs ?? null, forbidden_dirs: eng.forbidden_dirs ?? null, allowed_tools: s.contract.allowed_tools ?? null, constraints: eng.constraints ?? null }),
      detail("未知项（未提供不代表没有未知项）", s.unknowns), detail("日志入口 / 安全日志", s.logs),
      detail("差异入口 / 原始差异（未提供即未接入）", s.diff), detail("恢复记录（不是恢复成功证明）", s.recovery),
      node("p", "实际发布 / DB 恢复：此面板未接入执行与证据验证，不能显示成功；任何后端状态仅作原始记录。", { class: "note-box warn" }),
      detail("原始安全快照", s.raw));
    if (!dirty) draft.value = JSON.stringify({ ...draftTemplate, ...Object.fromEntries(Object.keys(draftTemplate).filter(k => k in eng).map(k => [k, eng[k]])) }, null, 2);
  }
  function controls() {
    freeze.disabled = busy;
    unfreeze.disabled = busy || snapshot?.frozen !== true;
    submit.disabled = busy || !snapshot || snapshot.frozen !== false;
    refreshButton.disabled = busy || loading;
  }
  let taskEvents;
  async function refresh(events) {
    if (Array.isArray(events)) taskEvents = events;
    if (loading || busy) return;
    loading = true; controls();
    try {
      const s = normalizeSafety(await api(`${path}/safety`));
      if (s.logs == null) s.logs = taskEvents;
      paint(s);
    }
    catch (e) { snapshot = null; notice.textContent = "安全快照不可用 / 状态未知"; evidence.replaceChildren(); errors.textContent = `安全快照加载失败：${errorHint(e)}`; }
    finally { loading = false; controls(); }
  }
  async function mutate(url, body, message) {
    if (busy) return;
    if (!confirmAction(message)) return;
    busy = true; controls(); errors.textContent = "";
    try {
      await api(url, { method: "POST", body: JSON.stringify(body) });
      notice.textContent = "请求已返回；正在重新读取服务端快照（不代表工具执行或验证成功）。";
      acknowledge.checked = false;
    } catch (e) { errors.textContent = `安全操作失败：${errorHint(e)}`; }
    finally { busy = false; controls(); await refresh(); }
  }
  freeze.addEventListener("click", () => {
    if (!reason.value.trim()) { errors.textContent = "请填写全局冻结原因。"; return; }
    void mutate("/api/safety/freeze", { reason: reason.value.trim() }, "确认全局冻结所有任务？旧审批将撤销，已运行的副作用不保证能中断。");
  });
  unfreeze.addEventListener("click", () => {
    if (!reason.value.trim()) { errors.textContent = "请填写解冻原因。"; return; }
    void mutate("/api/safety/unfreeze", { reason: reason.value.trim() }, "确认解除全局冻结？解冻不会恢复旧审批或自动批准工具操作，必须重新审批。");
  });
  submit.addEventListener("click", () => {
    try {
      if (!acknowledge.checked) throw new Error("请先勾选用户审阅确认。");
      const contract = validateContract(JSON.parse(draft.value));
      void mutate(`${path}/development/contract`, contract, "确认提交当前开发契约？旧审批可能失效；发布、数据库恢复仍须独立批准与实际证据。");
    } catch (e) { errors.textContent = `契约未提交：${errorHint(e)}`; }
  });
  refreshButton.addEventListener("click", () => { errors.textContent = ""; void refresh(); });
  controls();
  return { element: root, refresh };
}

export function renderDevelopmentTaskForm(app, { el, api, errorHint, onCreated, onBack }) {
  app.replaceChildren();
  app.appendChild(el("div", { class: "page-head" }, el("h1", {}, "新建软件开发任务"),
    el("span", { class: "sub" }, "先提出目标，再确认开发契约；不自动批准修改、发布或数据库操作。")));
  const goal = el("textarea", { id: "f-goal", required: true, placeholder: "例如：修复登录校验，限定修改目录并增加回归测试" });
  const repository = el("input", { id: "f-repository", placeholder: "仓库路径（可选，随后在契约中确认）" });
  const branch = el("input", { id: "f-branch", placeholder: "分支（可选）" });
  const error = el("p", { role: "alert" });
  const submit = el("button", { type: "submit", class: "btn primary" }, "创建开发任务");
  const form = el("form", { class: "card", onSubmit: async event => {
    event.preventDefault();
    if (submit.disabled) return;
    if (!goal.value.trim()) { error.textContent = "请填写软件开发目标。"; return; }
    submit.disabled = true; error.textContent = "";
    try {
      const body = { goal: goal.value.trim() };
      if (repository.value.trim()) body.repository = repository.value.trim();
      if (branch.value.trim()) body.branch = branch.value.trim();
      const result = await api("/api/development/tasks", { method: "POST", body: JSON.stringify(body) });
      onCreated(result?.id || result?.task_id || result?.task?.id);
    } catch (e) { error.textContent = `创建失败：${errorHint(e)}`; }
    finally { submit.disabled = false; }
  } }, el("label", { for: "f-goal" }, "开发目标（必填）"), goal,
  el("label", { for: "f-repository" }, "仓库"), repository, el("label", { for: "f-branch" }, "分支"), branch,
  error, submit, el("button", { type: "button", class: "btn", onClick: onBack }, "返回列表"));
  app.appendChild(form);
}
