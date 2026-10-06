/* Local workspace file editor: all writes are staged, diffed, and explicitly approved. */
import { currentLocale } from "./i18n.js";
import { createOperationsPanel } from "./operations.js";
const obj = value => value && typeof value === "object" && !Array.isArray(value) ? value : {};
const arr = value => Array.isArray(value) ? value : [];
const safe = value => value == null ? "" : typeof value === "string" ? value : JSON.stringify(value, null, 2);

function node(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") n.className = value;
    else if (key === "text") n.textContent = String(value);
    else if (key.startsWith("on") && typeof value === "function") n.addEventListener(key.slice(2).toLowerCase(), value);
    else n.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) if (child != null && child !== false) n.append(typeof child === "string" ? document.createTextNode(child) : child);
  return n;
}
const button = (label, action, cls = "button") => node("button", { type: "button", class: cls, onClick: action }, label);

export function createFileWorkspace({ api, onError } = {}) {
  const root = node("section", { class: "file-workspace", "aria-label": "本机文件工作区" });
  const tree = node("aside", { class: "file-tree" });
  const main = node("main", { class: "file-main" });
  const inspector = node("aside", { class: "file-inspector" });
  const status = node("p", { class: "workspace-status", role: "status", "aria-live": "polite" }, "请选择本机文件夹。");
  const errors = node("p", { class: "workspace-error", role: "alert" });
  const list = node("div", { class: "file-list", role: "list" });
  const editor = node("textarea", { class: "workspace-editor", "aria-label": "文件编辑器", spellcheck: "false" });
  const diff = node("pre", { class: "workspace-diff", "aria-label": "精确统一差异" }, "尚未生成候选差异。");
  const fileTitle = node("h2", { class: "workspace-file-title" }, "未选择文件");
  const pick = button("选择本机文件夹", () => void pickWorkspace(), "button button-primary");
  const addName = node("input", { class: "file-name-input", type: "text", placeholder: "新增文件相对路径", "aria-label": "新增文件相对路径" });
  const add = button("添加文件", () => addFile(), "button");
  const generate = button("生成候选", () => void generateCandidate(), "button button-primary");
  const confirm = node("input", { type: "checkbox", "aria-label": "确认已审阅精确差异" });
  const confirmLabel = node("label", { class: "approval-check" }, confirm, "我已审阅上方精确差异，确认可写入原始目录");
  const approve = button("批准并应用", () => void approveApply(), "button button-primary");
  const history = node("div", { class: "workspace-history" });
  const blocked = node("p", { class: "workspace-blocked", role: "alert" });
  const versionContent = node("pre", { class: "workspace-version", "aria-label": "历史文件内容" });
  const fromCommit = node("select", { "aria-label": "差异起始版本" });
  const toCommit = node("select", { "aria-label": "差异目标版本" });
  const compare = button("比较版本", () => void compareVersions());
  const refresh = button("刷新工作区", () => void refreshWorkspace());
  const metadata = node("p", { class: "workspace-candidate" });
  const staged = node("p", { class: "workspace-staged" }, "暂无暂存变更");
  const originals = new Map();
  const aiText = (zh, en) => currentLocale() === "en" ? en : zh;
  const aiGoal = node("textarea", { "aria-label": aiText("AI 目标", "AI goal"), maxlength: "4000", rows: "3" });
  const sendCode = node("input", { type: "checkbox", "aria-label": aiText("同意发送代码", "Consent to send code") });
  const sensitiveConsent = node("input", { type: "checkbox", "aria-label": aiText("单独同意敏感路径", "Separate sensitive path consent") });
  const aiScope = node("div", { class: "workspace-ai-scope" });
  const aiError = node("p", { class: "workspace-error", role: "alert" });
  const aiAnalysis = node("pre", { "aria-label": aiText("AI 分析结果", "AI analysis result") });
  const aiPlan = node("pre", { "aria-label": aiText("AI 计划结果", "AI plan result") });
  const aiProposal = node("pre", { "aria-label": aiText("AI 提议说明", "AI proposal notes") });
  const analyze = button(aiText("分析", "Analyze"), () => void requestAI("analyze"));
  const plan = button(aiText("计划", "Plan"), () => void requestAI("plan"));
  const propose = button(aiText("提议", "Propose"), () => void requestAI("propose"));
  const allowedPaths = new Set(), sensitivePaths = new Set();
  let aiAvailable = false;
  let runId = null, runState = null;
  const runPlan = node("pre", { "aria-label": aiText("运行计划", "Run plan") });
  const runStatus = node("pre", { "aria-label": aiText("运行状态", "Run status") });
  const runError = node("p", { class: "workspace-error", role: "alert" });
  const runReview = node("input", { type: "checkbox", "aria-label": aiText("确认运行计划", "Confirm run plan") });
  const runCreate = button(aiText("创建运行计划", "Create run plan"), () => void requestRun("create"));
  const runConfirm = button(aiText("确认计划", "Confirm plan"), () => void requestRun("confirm"));
  const runStep = button(aiText("手动推进一步", "Advance one step"), () => void requestRun("step"));
  const runRefresh = button(aiText("刷新运行", "Refresh run"), () => void requestRun("read"));
  const runPause = button(aiText("暂停运行", "Pause run"), () => void requestRun("pause"));
  const runResume = button(aiText("恢复运行", "Resume run"), () => void requestRun("resume"));
  const runStop = button(aiText("停止运行", "Stop run"), () => void requestRun("stop"));
  const retryNode = node("select", { "aria-label": aiText("重试失败节点", "Retry failed node") });
  const runRetry = button(aiText("确认重试", "Confirm retry"), () => void requestRun("retry"));
  const runCandidates = node("div", { "aria-label": aiText("运行节点候选", "Run node candidates") });
  const recordApplied = button(aiText("记录已应用", "Record applied"), () => void requestRun("record-applied"));
  const executionPreset = node("select", { "aria-label": aiText("候选执行预设", "Candidate execution preset") }, ...["pytest", "compileall", "ruff"].map(preset => node("option", { value: preset }, preset)));
  executionPreset.value = "pytest";
  const executionConsent = node("input", { type: "checkbox", "aria-label": aiText("批准当前候选执行", "Approve current candidate execution") });
  const executionBinding = node("p", { class: "workspace-execution-binding" });
  const executionStatus = node("p", { role: "status" });
  const executionResult = node("pre", { "aria-label": aiText("候选执行结果", "Candidate execution result") });
  const execute = button(aiText("批准并执行候选", "Approve and execute candidate"), () => void executeCandidate());
  const readEvidence = button(aiText("读取执行证据", "Read execution evidence"), () => void loadExecutionEvidence());
  let executionCapabilities = {}, executionApprovalHash = null, appliedNode = null, retainedApplicationState = false;
  const executionEvidence = new Map();
  const candidateKey = value => JSON.stringify([value?.candidate_id, value?.source_hash, value?.candidate_hash]);
  const validCandidate = () => !!(candidate?.candidate_id && candidate.source_hash && candidate.candidate_hash && typeof candidate.unified_diff === "string" && diff.textContent === candidate.unified_diff);
  function canAdvanceRun() {
    if (retainedApplicationState || appliedNode) return false;
    if (["pending", "in_progress"].includes(runState?.status)) return true;
    if (runState?.status !== "blocked") return false;
    const completed = new Set(arr(runState.nodes).filter(n => n.status === "completed").map(n => n.id));
    const next = arr(runState.nodes).find(n => ["pending", "blocked"].includes(n.status) && arr(n.depends_on).every(id => completed.has(id)));
    const presets = { test: "pytest", lint: "ruff", compile: "compileall" };
    return next?.phase === "verify" && arr(next.tests).length > 0 && arr(next.tests).every(name => executionEvidence.get(candidateKey(next.candidate))?.has(presets[name] || name));
  }
  function applicationNode(value) {
    return arr(runState?.nodes).find(n => n.phase === "apply" && n.status === "waiting_user" && candidateKey(n.candidate) === candidateKey(value));
  }

  let workspaceId = null, files = [], selected = null, drafts = new Map(), dirty = false, busy = false, candidate = null, frozen = false, recoveryRequired = false;
  const pathFor = suffix => `/api/local-workspaces/${encodeURIComponent(workspaceId)}${suffix}`;
  const operationsPanel = createOperationsPanel({ api,
    onChanged: async () => { invalidateCandidate(); await loadWorkspace(); setBusy(busy); },
    onError: error => { errors.textContent = safe(error?.message || error); },
  });
  const report = error => { const message = safe(error?.message || error) || "操作失败"; errors.textContent = message; if (/frozen|freeze|blocked|冻结|阻止/i.test(message)) { frozen = true; invalidateCandidate(); } try { if (typeof onError === "function") onError(error); } catch {} };
  const setBusy = value => {
    busy = value;
    root.querySelectorAll("button,input,textarea,select").forEach(control => { control.disabled = busy; });
    add.disabled = busy || frozen || !workspaceId || !addName.value.trim();
    addName.disabled = busy || frozen || !workspaceId;
    editor.disabled = busy || frozen || !selected || drafts.get(selected) === null;
    generate.disabled = busy || frozen || !workspaceId || !drafts.size;
    const valid = candidate?.candidate_id && candidate?.source_hash && candidate?.candidate_hash && typeof candidate.unified_diff === "string" && candidate.unified_diff && diff.textContent === candidate.unified_diff;
    confirm.disabled = busy || frozen || !valid;
    approve.disabled = busy || frozen || !valid || !confirm.checked;
    refresh.disabled = busy || !workspaceId;
    compare.disabled = busy || !fromCommit.value || !toCommit.value;
    list.querySelectorAll(".file-entry-delete").forEach(control => { control.disabled = busy || frozen; });
    const aiReady = !busy && !frozen && !!workspaceId && aiAvailable && !!aiGoal.value.trim() && sendCode.checked;
    analyze.disabled = plan.disabled = !aiReady;
    propose.disabled = !aiReady || !allowedPaths.size || (sensitivePaths.size > 0 && !sensitiveConsent.checked);
    aiScope.querySelectorAll("input").forEach(control => { control.disabled = busy || frozen; });
    runCreate.disabled = !aiReady;
    runConfirm.disabled = busy || frozen || !runId || runState?.confirmed !== false || !runReview.checked || !runScopeAuthorized();
    runStep.disabled = busy || frozen || !runId || !aiAvailable || !sendCode.checked || runState?.confirmed !== true || !canAdvanceRun() || !runScopeAuthorized();
    const executionReady = executionCapabilities.available === true && executionCapabilities.configured === true && arr(executionCapabilities.presets).includes(executionPreset.value);
    executionPreset.disabled = executionConsent.disabled = busy || frozen || !validCandidate() || !executionReady;
    execute.disabled = busy || frozen || !validCandidate() || !executionReady || !executionConsent.checked || executionApprovalHash !== candidateKey(candidate);
    readEvidence.disabled = busy || !validCandidate();
    recordApplied.disabled = busy || frozen || !runId || !appliedNode || appliedNode.runId !== runId || !arr(runState?.nodes).some(n => n.id === appliedNode.nodeId && n.phase === "apply" && n.status === "waiting_user");
    runCandidates.querySelectorAll("button").forEach(control => { control.disabled = busy || frozen || retainedApplicationState || !!appliedNode; });
    runRefresh.disabled = busy || !runId;
    runPause.disabled = runStop.disabled = busy || frozen || !runId || ["stopped", "completed"].includes(runState?.status);
    runResume.disabled = busy || frozen || runState?.status !== "paused";
    runRetry.disabled = busy || frozen || !runId || !retryNode.value || ["paused", "stopped", "completed"].includes(runState?.status);
    operationsPanel.setExternalBusy(busy);
  };
  function renderAIScope() {
    const paths = [...new Set(files.map(file => typeof file === "string" ? file : file.path).filter(path => typeof path === "string"))];
    for (const path of allowedPaths) if (!paths.includes(path)) { allowedPaths.delete(path); sensitivePaths.delete(path); invalidateCandidate(); }
    aiScope.replaceChildren(...paths.map(path => {
      const allow = node("input", { type: "checkbox", "aria-label": `${aiText("允许提议修改", "Allow proposal changes")} ${path}` });
      const sensitive = node("input", { type: "checkbox", "aria-label": `${aiText("敏感路径授权", "Sensitive path authorization")} ${path}` });
      allow.checked = allowedPaths.has(path); sensitive.checked = sensitivePaths.has(path);
      allow.addEventListener("change", () => { if (busy || frozen) return; if (allow.checked) allowedPaths.add(path); else { allowedPaths.delete(path); sensitivePaths.delete(path); sensitive.checked = false; } sensitiveConsent.checked = false; invalidateCandidate(); setBusy(busy); });
      sensitive.addEventListener("change", () => { if (busy || frozen) return; if (sensitive.checked && !allowedPaths.has(path)) { sensitive.checked = false; return; } if (sensitive.checked) sensitivePaths.add(path); else sensitivePaths.delete(path); sensitiveConsent.checked = false; invalidateCandidate(); setBusy(busy); });
      return node("div", {}, node("label", {}, allow, path), node("label", {}, sensitive, aiText("敏感路径", "Sensitive path")));
    }));
  }
  function runScopeAuthorized() {
    const tasks = arr(runState?.plan?.subtasks);
    return tasks.length > 0 && tasks.every(task => arr(task.allowed_paths).length > 0 && task.allowed_paths.every(path => allowedPaths.has(path) && (!sensitivePaths.has(path) || sensitiveConsent.checked)));
  }
  function showRun(result) {
    if (!result?.id || !result.plan) throw new Error(aiText("运行结果缺少标识或计划。", "Run result missing ID or plan."));
    runId = result.id; runState = result; runReview.checked = false; retainedApplicationState = false;
    if (appliedNode && (appliedNode.runId !== runId || !arr(result.nodes).some(n => n.id === appliedNode.nodeId && n.phase === "apply" && n.status === "waiting_user"))) { appliedNode = null; try { localStorage.removeItem(`longflow.fileWorkspaceApplied.${workspaceId}`); } catch {} }
    runPlan.textContent = safe(result.plan); const { plan: ignored, ...state } = result; runStatus.textContent = safe(state);
    retryNode.replaceChildren(node("option", { value: "" }, aiText("选择失败节点", "Select failed node")), ...arr(result.nodes).filter(n => n.status === "failed").map(n => node("option", { value: n.id }, safe(n.id))));
    runCandidates.replaceChildren(...arr(result.nodes).filter(n => n.candidate?.candidate_id).map(n => button(`${aiText("载入节点候选", "Load node candidate")} ${safe(n.id)}`, () => {
      if (busy || frozen || retainedApplicationState || appliedNode) return;
      try { showCandidate(n.candidate); } catch (error) { report(error); } finally { setBusy(busy); }
    })));
    try { localStorage.setItem(`longflow.fileWorkspaceRun.${workspaceId}`, runId); } catch {}
  }
  async function restoreRun() {
    runId = null; runState = null; runReview.checked = false; runPlan.textContent = runStatus.textContent = runError.textContent = ""; retryNode.replaceChildren();
    try { runId = localStorage.getItem(`longflow.fileWorkspaceRun.${workspaceId}`); if (runId) showRun(await api(pathFor(`/runs/${encodeURIComponent(runId)}`))); }
    catch (error) {
      runError.textContent = safe(error?.message || error);
      try {
        const retained = JSON.parse(localStorage.getItem(`longflow.fileWorkspaceApplied.${workspaceId}`) || "null");
        if (retained?.runId === runId && retained.state?.id === runId && arr(retained.state?.nodes).some(n => n.id === retained.nodeId && n.phase === "apply" && n.status === "waiting_user")) {
          appliedNode = { runId, nodeId: retained.nodeId }; showRun(retained.state); retainedApplicationState = true;
          runError.textContent += aiText("\n仅保留待记录应用的本地状态；服务端仍须验证收据。", "\nLocal pending-application state only; the server must still verify the receipt.");
        } else runId = null;
      } catch { runId = null; }
    }
  }
  async function requestRun(action) {
    if (busy || !workspaceId || (frozen && action !== "read")) return;
    let body = {}, suffix = "/runs";
    if (action === "create") {
      if (!aiAvailable || !sendCode.checked || !aiGoal.value.trim()) { runError.textContent = aiText("需要真实模型、目标和发送代码同意。", "A real model, goal and code-sharing consent are required."); return; }
      body = { goal: aiGoal.value.trim(), confirmed_send_code: true };
    } else {
      if (!runId) return; suffix += `/${encodeURIComponent(runId)}`;
      if (action !== "read") suffix += `/${action}`;
      if (action === "confirm") {
        if (!runReview.checked || !runScopeAuthorized()) { runError.textContent = aiText("请审阅计划并明确选择计划中的全部精确修改路径。", "Review the plan and explicitly select all its exact change paths."); return; }
        body = { confirmed: true, allowed_paths: [...new Set(arr(runState?.plan?.subtasks).flatMap(task => arr(task.allowed_paths)))] };
      }
      if (action === "step") {
        if (!aiAvailable || !sendCode.checked || runState?.confirmed !== true || !canAdvanceRun() || !runScopeAuthorized()) { runError.textContent = aiText("推进需要已确认计划、代码同意和明确路径范围。", "Advancement requires a confirmed plan, code consent and explicit path scope."); return; }
        body = { confirmed_send_code: true }; invalidateCandidate();
      }
      if (action === "record-applied") {
        if (!appliedNode || appliedNode.runId !== runId || !arr(runState?.nodes).some(n => n.id === appliedNode.nodeId && n.phase === "apply" && n.status === "waiting_user") || !window.confirm(aiText("确认记录刚刚实际应用的节点？服务端将核验应用收据。", "Record the node just actually applied? The server will verify its application receipt."))) return;
        body = { node_id: appliedNode.nodeId, confirmed: true };
      }
      if (action === "retry") {
        if (!retryNode.value || !window.confirm(aiText("确认重试所选失败节点？不会重放状态不明的节点。", "Retry the selected failed node? In-doubt nodes will not be replayed."))) return;
        body = { node_id: retryNode.value, confirmed: true };
      }
    }
    setBusy(true); runError.textContent = "";
    try { showRun(await api(pathFor(suffix), action === "read" ? undefined : { method: "POST", body: JSON.stringify(body) })); }
    catch (error) { runError.textContent = safe(error?.message || error); report(error); }
    finally { setBusy(false); }
  }
  async function loadAICapabilities() {
    aiAvailable = false; executionCapabilities = {};
    executionStatus.textContent = aiText("执行能力不可用，未验证任何测试。", "Execution capabilities unavailable; no tests verified.");
    try { const result = await api(pathFor("/capabilities")); executionCapabilities = obj(result?.execution); executionStatus.textContent = safe(executionCapabilities); aiAvailable = result?.ai?.available === true && result.ai.effective_driver === "openai_compatible"; aiError.textContent = aiAvailable ? "" : aiText("AI 不可用：本地驱动或未配置模型端点，不会生成模拟结果。", "AI unavailable: local driver or model endpoint not configured; no simulated results."); }
    catch (error) { aiError.textContent = `${aiText("AI 能力不可用", "AI capabilities unavailable")}: ${safe(error?.message || error)}`; }
  }
  async function requestAI(kind) {
    if (busy || frozen || !workspaceId) return;
    if (!aiAvailable) { aiError.textContent = aiText("AI 不可用：需要配置真实模型端点。", "AI unavailable: configure a real model endpoint."); return; }
    if (!aiGoal.value.trim() || !sendCode.checked) { aiError.textContent = aiText("请填写目标并明确同意发送代码。", "Enter a goal and explicitly consent to sending code."); return; }
    const body = { goal: aiGoal.value.trim(), confirmed_send_code: sendCode.checked === true };
    if (kind === "propose") {
      if (!allowedPaths.size || (sensitivePaths.size && !sensitiveConsent.checked)) { aiError.textContent = aiText("请选择精确修改路径；敏感路径需要单独同意。", "Select exact change paths; sensitive paths require separate consent."); return; }
      body.allowed_paths = [...allowedPaths]; body.sensitive_paths = [...sensitivePaths]; body.confirmed_sensitive = sensitiveConsent.checked === true;
      invalidateCandidate(); aiProposal.textContent = "";
    }
    const output = kind === "analyze" ? aiAnalysis : aiPlan;
    if (kind !== "propose") output.textContent = "";
    setBusy(true); aiError.textContent = "";
    try {
      const result = await api(pathFor(`/ai/${kind}`), { method: "POST", body: JSON.stringify(body) });
      if (mutationBlocked(result)) { invalidateCandidate(); showBlocked(); throw new Error(aiText("AI 结果已被阻止。", "AI result blocked.")); }
      if (kind === "propose") {
        showCandidate(result);
        aiProposal.textContent = safe({ summary: result.ai_summary, context: result.ai_context, evidence: result.ai_evidence });
      } else if (kind === "analyze") output.textContent = safe({ summary: result?.summary, files: arr(result?.files), unknowns: arr(result?.unknowns), context: result?.context, evidence: result?.evidence });
      else output.textContent = safe({ subtasks: arr(result?.subtasks), context: result?.context, evidence: result?.evidence });
    } catch (error) { aiError.textContent = safe(error?.message || error); report(error); }
    finally { setBusy(false); }
  }
  function invalidateCandidate() { candidate = null; confirm.checked = executionConsent.checked = false; executionApprovalHash = null; executionBinding.textContent = executionResult.textContent = ""; metadata.textContent = ""; diff.textContent = "暂存变更尚未生成候选差异。"; }
  function retainExecutionEvidence(value, records) {
    const passed = new Set();
    for (const record of records) {
      const evidence = obj(record.evidence);
      if (record.candidate_id === value.candidate_id && record.source_hash === value.source_hash && record.candidate_hash === value.candidate_hash && record.expires_at * 1000 >= Date.now() && evidence.passed === true && evidence.candidate_id === value.candidate_id && evidence.candidate_sha === value.candidate_hash && evidence.preset === record.preset) passed.add(record.preset);
    }
    executionEvidence.set(candidateKey(value), passed);
  }
  async function fetchExecutionEvidence(value) {
    const result = await api(pathFor(`/execution/evidence/${encodeURIComponent(value.candidate_id)}`));
    retainExecutionEvidence(value, arr(result?.evidence));
    executionResult.textContent = safe(result);
  }
  async function loadExecutionEvidence() {
    if (busy || !validCandidate()) return;
    const value = candidate; setBusy(true);
    try { await fetchExecutionEvidence(value); } catch (error) { executionResult.textContent = safe({ error: error?.message || error }); report(error); } finally { setBusy(false); }
  }
  async function executeCandidate() {
    if (busy || frozen || !validCandidate() || executionCapabilities.available !== true || executionCapabilities.configured !== true || !arr(executionCapabilities.presets).includes(executionPreset.value) || !executionConsent.checked || executionApprovalHash !== candidateKey(candidate)) return;
    const value = candidate, preset = executionPreset.value;
    executionConsent.checked = false; executionApprovalHash = null; executionResult.textContent = ""; setBusy(true);
    try {
      const approval = await api(pathFor("/execution/approvals"), { method: "POST", body: JSON.stringify({ candidate_id: value.candidate_id, source_hash: value.source_hash, candidate_hash: value.candidate_hash, preset, confirmed: true }) });
      if (mutationBlocked(approval)) { invalidateCandidate(); showBlocked(); return; }
      if (!approval?.approval_id) throw new Error(aiText("执行批准缺少标识。", "Execution approval missing ID."));
      const result = await api(pathFor("/execution/run"), { method: "POST", body: JSON.stringify({ approval_id: approval.approval_id }) });
      executionResult.textContent = safe(result);
      if (mutationBlocked(result)) { invalidateCandidate(); showBlocked(); return; }
      try { await fetchExecutionEvidence(value); } catch (error) { executionResult.textContent = safe({ result, evidence_error: error?.message || error }); }
    } catch (error) { executionResult.textContent = safe({ error: error?.message || error }); report(error); } finally { setBusy(false); }
  }
  function updateDrafts() { staged.textContent = `${drafts.size} 个暂存变更（未写入原始目录）`; setBusy(busy); }
  function showCandidate(result) {
    invalidateCandidate();
    if (mutationBlocked(result)) { showBlocked("工作区被冻结或候选被阻止。"); throw new Error("工作区被冻结或候选被阻止。"); }
    if (!result?.candidate_id || !result.source_hash || !result.candidate_hash || typeof result.unified_diff !== "string") throw new Error("候选缺少标识、哈希或精确差异，无法批准。");
    candidate = result; diff.textContent = result.unified_diff;
    executionBinding.textContent = `${aiText("执行批准绑定候选哈希", "Execution approval bound to candidate hash")}: ${safe(result.candidate_hash)}`;
    metadata.textContent = `候选：${safe(result.candidate_id)}\n原始哈希：${safe(result.source_hash)}\n候选哈希：${safe(result.candidate_hash)}\n变更文件：${safe(result.changed_files)}`;
    status.textContent = result.unified_diff ? "候选已生成；请审阅精确差异。原始目录未修改。" : "服务端返回空差异，不能应用。";
  }
  const mutationBlocked = payload => { const p = obj(payload); return p.recovery_required === true || p.info?.recovery_required === true || p.frozen === true || p.blocked === true || p.status === "frozen" || p.status === "blocked"; };
  function showBlocked(message) { frozen = true; blocked.textContent = message || "工作区已冻结或被阻止；仅可读取和刷新。"; setBusy(busy); }
  function renderFiles() { renderAIScope(); list.replaceChildren(...files.map(file => { const path = typeof file === "string" ? file : file.path; const entry = node("div", { class: "file-entry", role: "listitem" }); const select = button(path, () => selectFile(path), "file-entry-button"); if (path === selected) entry.classList.add("selected"); const del = button("删除", () => stageDelete(path), "file-entry-delete"); entry.append(select, del); return entry; })); }
  async function pickWorkspace() {
    if (busy) return;
    if (drafts.size && !window.confirm("切换文件夹将丢弃当前全部暂存变更，继续？")) return;
    setBusy(true); errors.textContent = "";
    try { const result = await api("/api/local-workspaces/pick", { method: "POST", body: JSON.stringify({}) }); if (result?.cancelled || result?.canceled) return; if (!result?.workspace_id) throw new Error("后端未返回 workspace_id"); workspaceId = result.workspace_id; appliedNode = null; executionEvidence.clear(); allowedPaths.clear(); sensitivePaths.clear(); sendCode.checked = sensitiveConsent.checked = false; aiAvailable = false; aiAnalysis.textContent = aiPlan.textContent = aiProposal.textContent = ""; drafts.clear(); originals.clear(); selected = null; dirty = false; frozen = false; editor.value = ""; fileTitle.textContent = "未选择文件"; invalidateCandidate(); updateDrafts(); try { localStorage.setItem("longflow.fileWorkspace", workspaceId); } catch {} await loadWorkspace(); }
    catch (error) { report(error); } finally { setBusy(false); }
  }
  async function loadWorkspace({ preserveRun = false } = {}) {
    if (!workspaceId) return; const result = await api(pathFor("")); files = arr(result?.files); frozen = mutationBlocked(result); renderFiles(); recoveryRequired = result?.recovery_required === true || result?.info?.recovery_required === true; blocked.textContent = ""; status.textContent = `${safe(result?.source || "本机文件夹")} · ${files.length} 个文件`; if (frozen) showBlocked(recoveryRequired ? "工作区需要恢复；禁止写入，请先处理恢复并刷新。" : "工作区已冻结或被阻止；禁止写入，请刷新状态。"); await loadHistory(); await loadAICapabilities(); await operationsPanel.refresh({ workspaceId, frozen: result?.frozen === true, recoveryRequired }); if (!preserveRun && !appliedNode) await restoreRun();
  }
  async function loadFile(path) { const result = await api(`${pathFor("/file")}?path=${encodeURIComponent(path)}`); if (typeof result?.content !== "string") throw new Error("服务端未返回文本文件内容。"); const content = result.content; originals.set(path, content); editor.value = content; dirty = false; fileTitle.textContent = path; }
  async function selectFile(path) { if (busy || path === selected) return; if (dirty && !window.confirm("当前文件有编辑草稿，切换将保留全部暂存内容。继续？")) return; setBusy(true); invalidateCandidate(); try { if (drafts.has(path)) { editor.value = drafts.get(path) ?? ""; fileTitle.textContent = `${path}${drafts.get(path) === null ? "（暂存删除）" : ""}`; } else await loadFile(path); selected = path; dirty = false; renderFiles(); } catch (error) { report(error); } finally { setBusy(false); } }
  function stageDelete(path) { if (busy || frozen) return; if (!window.confirm(`暂存删除 ${path}？删除仅在批准并应用后写入。`)) return; drafts.set(path, null); invalidateCandidate(); if (selected === path) { dirty = true; editor.value = ""; fileTitle.textContent = `${path}（暂存删除）`; } renderFiles(); updateDrafts(); }
  function addFile() { const path = addName.value.trim().replace(/\\/g, "/"); if (!path || busy || frozen || !workspaceId) return; if (path.startsWith("/") || /^[a-z]:/i.test(path) || path.split("/").some(part => !part || part === "." || part === "..") || /[\x00-\x1f]/.test(path)) { errors.textContent = "请输入工作区内有效的文件相对路径。"; return; } if (files.some(f => (typeof f === "string" ? f : f.path) === path)) { errors.textContent = "文件已存在。"; return; } if (dirty && !window.confirm("保留当前暂存草稿并切换到新增文件？")) return; files = [...files, { path }]; drafts.set(path, ""); invalidateCandidate(); addName.value = ""; selected = path; editor.value = ""; fileTitle.textContent = `${path}（新增）`; dirty = true; renderFiles(); updateDrafts(); }
  async function generateCandidate() { if (busy || frozen || !workspaceId) return; invalidateCandidate(); const changes = Object.fromEntries([...drafts.entries()]); if (!Object.keys(changes).length) { errors.textContent = "没有暂存变更。"; return; } setBusy(true); errors.textContent = ""; try { const result = await api(pathFor("/candidates"), { method: "POST", body: JSON.stringify({ changes }) }); showCandidate(result); status.textContent = `候选 ${safe(result?.candidate_id)} · 变更 ${arr(result?.changed_files).length} 个`; if (mutationBlocked(result)) showBlocked("服务端阻止了候选写入。"); } catch (error) { report(error); } finally { setBusy(false); } }
  async function approveApply() { if (busy || frozen || !candidate?.candidate_id) return; if (!candidate.unified_diff || diff.textContent !== candidate.unified_diff || !candidate.source_hash || !candidate.candidate_hash) { errors.textContent = "未显示精确差异，不能应用。"; return; } if (!confirm.checked) { errors.textContent = "请先勾选差异审阅确认。"; return; } if (!window.confirm("确认按当前精确差异批准并写入原始目录？")) return; setBusy(true); try { const approval = await api(pathFor("/approvals"), { method: "POST", body: JSON.stringify({ candidate_id: candidate.candidate_id, source_hash: candidate.source_hash, candidate_hash: candidate.candidate_hash, confirmed: true }) }); if (mutationBlocked(approval)) { showBlocked("服务端拒绝批准：工作区已冻结或被阻止。"); return; } if (!approval?.approval_id) throw new Error("服务端未返回 approval_id，已阻止应用。"); confirm.checked = false; const nodeToRecord = applicationNode(candidate); const result = await api(pathFor("/apply"), { method: "POST", body: JSON.stringify({ approval_id: approval.approval_id }) }); if (mutationBlocked(result)) { showBlocked("服务端阻止应用。"); invalidateCandidate(); return; } if (result?.commit && nodeToRecord) { appliedNode = { runId, nodeId: nodeToRecord.id }; try { localStorage.setItem(`longflow.fileWorkspaceApplied.${workspaceId}`, JSON.stringify({ ...appliedNode, state: runState })); } catch {} } status.textContent = "应用请求已返回；正在刷新文件与历史。"; const previousPath = selected; drafts.clear(); originals.clear(); selected = null; editor.value = ""; dirty = false; fileTitle.textContent = "未选择文件"; invalidateCandidate(); updateDrafts(); await loadWorkspace({ preserveRun: !!appliedNode }); if (previousPath && files.some(file => (typeof file === "string" ? file : file.path) === previousPath)) { await loadFile(previousPath); selected = previousPath; renderFiles(); } } catch (error) { report(error); } finally { setBusy(false); } }
  async function refreshWorkspace() { if (busy || !workspaceId) return; setBusy(true); invalidateCandidate(); try { await loadWorkspace(); } catch (error) { report(error); } finally { setBusy(false); } }
  async function loadHistory() { if (!workspaceId) return; try { const result = await api(pathFor("/versions")); const versions = arr(result?.versions); history.replaceChildren(...versions.map(versionEntry)); fromCommit.replaceChildren(); toCommit.replaceChildren(); for (const version of versions) { for (const select of [fromCommit, toCommit]) select.append(node("option", { value: safe(version.commit) }, safe(version.commit))); } if (versions.length > 1) fromCommit.selectedIndex = 1; } catch (error) { history.replaceChildren(node("p", { class: "workspace-error" }, `历史加载失败：${safe(error.message)}`)); } }
  function versionEntry(version) { const commit = safe(version?.commit); const entry = node("article", { class: "history-entry" }, node("strong", {}, safe(version?.message || commit)), node("p", {}, commit), node("time", {}, safe(version?.timestamp))); entry.append(button("查看文件版本", () => void showVersion(commit), "button"), button("恢复预览", () => void restorePreview(commit), "button")); return entry; }
  async function showVersion(commit) { if (busy) return; if (!selected) { errors.textContent = "请先选择文件。"; return; } setBusy(true); try { const result = await api(`${pathFor("/version")}?commit=${encodeURIComponent(commit)}&path=${encodeURIComponent(selected)}`); if (typeof result?.content !== "string") throw new Error("该历史版本未提供文本内容。"); versionContent.textContent = `${commit} · ${selected}\n\n${result.content}`; } catch (error) { report(error); } finally { setBusy(false); } }
  async function compareVersions() { if (busy || !workspaceId || !fromCommit.value || !toCommit.value) return; setBusy(true); invalidateCandidate(); try { const result = await api(`${pathFor("/diff")}?from_commit=${encodeURIComponent(fromCommit.value)}&to_commit=${encodeURIComponent(toCommit.value)}`); if (typeof result?.unified_diff !== "string") throw new Error("历史比较未提供 unified_diff。"); diff.textContent = result.unified_diff; metadata.textContent = "历史比较差异（非可应用候选）"; } catch (error) { report(error); } finally { setBusy(false); } }
  async function restorePreview(commit) { if (busy || frozen) return; setBusy(true); try { const result = await api(pathFor("/restore-preview"), { method: "POST", body: JSON.stringify({ commit }) }); showCandidate(result); status.textContent = `恢复候选 ${safe(result?.candidate_id)}，请审阅差异后确认。`; } catch (error) { report(error); } finally { setBusy(false); } }
  editor.addEventListener("input", () => { if (selected && !busy && !frozen) { dirty = true; drafts.set(selected, editor.value); invalidateCandidate(); updateDrafts(); renderFiles(); } });
  addName.addEventListener("input", () => { if (!busy && !frozen) add.disabled = !addName.value.trim(); });
  aiGoal.addEventListener("input", () => { if (!busy) { invalidateCandidate(); setBusy(busy); } });
  sendCode.addEventListener("change", () => { if (!busy) { invalidateCandidate(); setBusy(busy); } });
  sensitiveConsent.addEventListener("change", () => { if (!busy) { invalidateCandidate(); setBusy(busy); } });
  executionConsent.addEventListener("change", () => { if (!busy) { executionApprovalHash = executionConsent.checked && validCandidate() ? candidateKey(candidate) : null; setBusy(busy); } });
  executionPreset.addEventListener("change", () => { executionConsent.checked = false; executionApprovalHash = null; executionResult.textContent = ""; setBusy(busy); });
  runReview.addEventListener("change", () => setBusy(busy));
  retryNode.addEventListener("change", () => setBusy(busy));
  confirm.addEventListener("change", () => setBusy(busy));
  fromCommit.addEventListener("change", () => setBusy(busy));
  toCommit.addEventListener("change", () => setBusy(busy));
  tree.append(node("div", { class: "file-toolbar" }, pick, refresh, addName, add, status, errors), list);
  const aiPanel = node("section", { "aria-label": aiText("AI 工程建议", "AI engineering suggestions") },
    node("h2", {}, aiText("AI 工程建议", "AI engineering suggestions")),
    node("label", {}, aiText("目标", "Goal"), aiGoal),
    node("label", {}, sendCode, aiText("我同意将工作区代码上下文发送到已配置的模型端点（不限于下方修改路径）。", "I consent to sending workspace code context to the configured model endpoint (not limited to the change paths below).")),
    node("h3", {}, aiText("精确提议修改范围", "Exact proposal change scope")), aiScope,
    node("label", {}, sensitiveConsent, aiText("我单独同意上方明确选中的敏感路径修改。", "I separately consent to changes to the explicitly selected sensitive paths above.")),
    node("div", { class: "file-toolbar" }, analyze, plan, propose), aiError,
    node("p", {}, aiText("模型建议未执行；测试、部署未验证。提议仅生成候选，写入需要精确差异审阅和独立批准。", "Model suggestions are not executed; tests and deployment are unverified. Proposals only create candidates; writing requires exact-diff review and separate approval.")),
    node("h3", {}, aiText("分析", "Analysis")), aiAnalysis,
    node("h3", {}, aiText("计划（仅文本）", "Plan (text only)")), aiPlan,
    node("h3", {}, aiText("提议说明（非差异）", "Proposal notes (not the diff)")), aiProposal);
  const runPanel = node("section", { "aria-label": aiText("手动工程运行", "Manual engineering run") },
    node("h2", {}, aiText("手动工程运行", "Manual engineering run")), runCreate,
    node("h3", {}, aiText("运行计划", "Run plan")), runPlan,
    node("label", {}, runReview, aiText("我已审阅运行计划并确认上方精确路径范围。", "I have reviewed the run plan and confirmed the exact path scope above.")),
    node("div", { class: "file-toolbar" }, runConfirm, runStep, runRefresh, runPause, runResume, runStop, retryNode, runRetry), runError,
    node("p", {}, aiText("仅手动推进，无后台自动循环。候选执行需要独立批准；执行不可用不代表测试或部署成功。", "Manual advancement only; no background loop. Candidate execution needs separate approval; unavailable execution is not test or deployment success.")),
    node("h3", {}, aiText("运行状态", "Run status")), runStatus, runCandidates, recordApplied);
  main.append(node("div", { class: "workspace-editor-head" }, fileTitle), editor, staged, generate, aiPanel, runPanel, node("h2", {}, "精确候选差异"), diff);
  main.append(node("section", { "aria-label": aiText("候选执行", "Candidate execution") }, node("h2", {}, aiText("候选执行", "Candidate execution")), executionStatus, executionBinding, executionPreset, node("label", {}, executionConsent, aiText("我明确批准当前候选哈希按所选预设在隔离环境执行。", "I explicitly approve executing the current candidate hash with the selected preset in isolation.")), execute, readEvidence, executionResult));
  inspector.append(node("h2", {}, "批准 / 历史"), blocked, metadata, confirmLabel, approve, node("h3", {}, "版本历史"), node("div", { class: "file-toolbar" }, fromCommit, toCommit, compare), history, versionContent);
  main.append(operationsPanel.element);
  root.append(tree, main, inspector);
  try { workspaceId = localStorage.getItem("longflow.fileWorkspace"); } catch {}
  updateDrafts(); setBusy(false); add.disabled = true;
  if (workspaceId) { status.textContent = "正在恢复工作区…"; setBusy(true); void loadWorkspace().catch(report).finally(() => setBusy(false)); }
  return root;
}
