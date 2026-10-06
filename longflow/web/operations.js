/* Explicit server proposals only. Previewing never schedules an operation. */
import { currentLocale } from "./i18n.js";

const fieldsFor = {
  git_branch: ["branch", "base_branch"],
  git_merge: ["target_branch", "source_branch"],
  git_push: ["branch", "remote", "remote_branch"]
};
const fieldLabels = {
  branch: ["分支", "Branch"], base_branch: ["基础分支", "Base branch"],
  target_branch: ["目标分支", "Target branch"], source_branch: ["来源分支", "Source branch"],
  remote: ["远程 HTTPS 地址", "Remote HTTPS URL"], remote_branch: ["远程分支", "Remote branch"]
};
const nonempty = value => typeof value === "string" && value.trim().length > 0;
const validEnvelope = value => !!value && /^[0-9a-f]{64}$/.test(value.digest)
  && Number.isSafeInteger(value.epoch) && value.epoch >= 0
  && typeof value.expires_at === "number" && Number.isFinite(value.expires_at)
  && value.expires_at * 1000 > Date.now();
const freeze = value => {
  if (value && typeof value === "object") { Object.values(value).forEach(freeze); Object.freeze(value); }
  return value;
};
function node(tag, attrs = {}, ...children) {
  const element = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) element.setAttribute(name, value);
  for (const child of children) element.append(typeof child === "string" ? document.createTextNode(child) : child);
  return element;
}

export function createOperationsPanel({ api, workspaceId: workspaceGetter, context: contextGetter, onChanged, onError, locale } = {}) {
  const text = (zh, en) => (typeof locale === "function" ? locale() : locale || currentLocale()) === "en" ? en : zh;
  const element = node("section", { class: "workspace-operations", "aria-label": text("工程操作", "Engineering operations") });
  const status = node("pre", { "aria-label": text("Git 能力", "Git capabilities"), role: "status" });
  const productionStatus = node("pre", { "aria-label": text("生产能力状态", "Production capability status"), role: "status" });
  const errorText = node("p", { class: "workspace-error", role: "alert" });
  const action = node("select", { "aria-label": text("Git 操作", "Git action"), name: "action" },
    node("option", { value: "git_branch" }, text("创建分支", "Create branch")),
    node("option", { value: "git_merge" }, text("快进合并", "Fast-forward merge")),
    node("option", { value: "git_push" }, text("推送", "Push")));
  action.value = "git_branch";
  const fieldArea = node("div", { class: "file-toolbar" });
  const makeButton = (zh, en, handler) => {
    const button = node("button", { type: "button", class: "button" }, text(zh, en));
    button.addEventListener("click", handler);
    return button;
  };
  const makeLane = (kind, zh, en) => {
    const preview = node("pre", { "aria-label": text(`${zh}服务端预览`, `${en} server preview`) });
    const digest = node("p", { "aria-label": text(`${zh}摘要`, `${en} digest`) });
    const confirmed = node("input", { type: "checkbox", "aria-label": text(`${zh}确认`, `${en} confirmation`) });
    const push = kind === "git" ? node("input", { type: "checkbox", "aria-label": text("单独确认推送", "Separate push confirmation") }) : null;
    const pushLabel = push ? node("label", { class: "approval-check" }, push, text("我单独确认推送到预览中的远程地址。", "I separately confirm pushing to the previewed remote.")) : null;
    const lane = { kind, preview, digest, confirmed, push, pushLabel, proposal: null, json: "", binding: null };
    lane.propose = makeButton(`${zh}预览`, `Preview ${en.toLowerCase()}`, () => propose(lane));
    lane.execute = makeButton(`确认执行${zh}`, `Execute ${en.toLowerCase()}`, () => execute(lane));
    confirmed.checked = false;
    confirmed.addEventListener("change", () => { syncContext(); if (busy || !available(lane) || !valid(lane)) confirmed.checked = false; update(); });
    if (push) { push.checked = false; push.addEventListener("change", () => { syncContext(); if (busy || !available(lane) || !valid(lane) || action.value !== "git_push") push.checked = false; update(); }); }
    lane.section = node("section", { class: "workspace-operation-band", "aria-label": en },
      node("h3", {}, text(zh, en)), lane.propose, preview, digest,
      node("label", { class: "approval-check" }, confirmed, text("我已审阅此服务端精确预览和摘要，确认执行。", "I have reviewed this exact server preview and digest and confirm execution.")));
    if (pushLabel) lane.section.append(pushLabel);
    lane.section.append(lane.execute);
    return lane;
  };
  let wid = null, frozen = false, recoveryRequired = false, capabilities = {}, busy = false, externalBusy = false, generation = 0;
  let inputs = {};
  const git = makeLane("git", "Git", "Git");
  const recovery = makeLane("recovery", "恢复", "Recovery");
  const lanes = [git, recovery];
  function clear(lane) {
    lane.proposal = null; lane.json = ""; lane.binding = null;
    lane.confirmed.checked = false;
    if (lane.push) lane.push.checked = false;
    lane.preview.textContent = lane.digest.textContent = "";
  }
  function invalidate() { generation++; busy = false; lanes.forEach(clear); }
  function syncContext() {
    const context = typeof contextGetter === "function" ? contextGetter() : null;
    const nextWid = context && Object.hasOwn(context, "workspaceId") ? context.workspaceId : typeof workspaceGetter === "function" ? workspaceGetter() : wid;
    const nextFrozen = context && Object.hasOwn(context, "frozen") ? context.frozen === true : frozen;
    const nextRecovery = context && Object.hasOwn(context, "recoveryRequired") ? context.recoveryRequired === true : recoveryRequired;
    if (nextWid !== wid || nextFrozen !== frozen || nextRecovery !== recoveryRequired) {
      if (nextWid !== wid) {
        capabilities = {};
        status.textContent = "";
        productionStatus.textContent = text("生产能力未接入。", "Production capabilities not connected.");
        errorText.textContent = "";
      }
      wid = nextWid; frozen = nextFrozen; recoveryRequired = nextRecovery; invalidate();
    }
  }
  function bodyForGit() { return { action: action.value, ...Object.fromEntries((fieldsFor[action.value] || []).map(name => [name, inputs[name].value])) }; }
  function available(lane) {
    return !!wid && !frozen && !externalBusy && (lane === recovery || (!recoveryRequired && capabilities[action.value]?.status === "configured"));
  }
  function valid(lane) {
    const proposal = lane.proposal;
    return !!proposal && lane.binding?.wid === wid && proposal.action === (lane === git ? action.value : "local_recovery") &&
      lane.preview.textContent === lane.json && lane.digest.textContent === proposal.digest &&
      (lane === recovery || lane.binding.request === JSON.stringify(bodyForGit())) &&
      validEnvelope(proposal);
  }
  function update() {
    action.disabled = busy || externalBusy || frozen || !wid || recoveryRequired;
    Object.values(inputs).forEach(input => { input.disabled = action.disabled; });
    for (const lane of lanes) {
      const ready = available(lane) && !busy;
      lane.propose.disabled = !ready || (lane === git && (!fieldsFor[action.value] || Object.values(inputs).some(input => !input.value.trim())));
      lane.confirmed.disabled = !ready || !valid(lane);
      if (lane.push) {
        lane.pushLabel.hidden = action.value !== "git_push";
        lane.push.disabled = !ready || !valid(lane) || action.value !== "git_push";
      }
      lane.execute.disabled = !ready || !valid(lane) || !lane.confirmed.checked || (lane === git && action.value === "git_push" && !lane.push.checked);
    }
    status.textContent = Object.keys(capabilities).length ? JSON.stringify(capabilities, null, 2) : text("Git 能力不可用。", "Git capabilities unavailable.");
  }
  function renderFields() {
    inputs = {};
    fieldArea.replaceChildren(...(fieldsFor[action.value] || []).map(name => {
      const label = text(...fieldLabels[name]);
      const input = node("input", { type: "text", name, "aria-label": label, autocomplete: "off" });
      input.value = ""; inputs[name] = input;
      const changed = () => { syncContext(); generation++; clear(git); update(); };
      input.addEventListener("input", changed); input.addEventListener("change", changed);
      return node("label", {}, label, input);
    }));
  }
  function report(error) {
    errorText.textContent = String(error?.message || error);
    try { if (typeof onError === "function") onError(error); } catch {}
  }
  const pathFor = (workspace, suffix) => `/api/local-workspaces/${encodeURIComponent(workspace)}${suffix}`;
  const post = (url, body) => api(url, { method: "POST", body: JSON.stringify(body) });
  async function propose(lane) {
    syncContext(); update();
    if (lane.propose.disabled) return;
    clear(lane); errorText.textContent = "";
    const workspace = wid, requested = lane === git ? bodyForGit() : {}, requestedAction = lane === git ? action.value : "local_recovery";
    const ticket = ++generation; busy = true; update();
    try {
      const response = await post(pathFor(workspace, `/${lane.kind}/proposals`), requested);
      syncContext();
      if (ticket !== generation || workspace !== wid) return;
      const copy = JSON.parse(JSON.stringify(response));
      if (copy?.action !== requestedAction || !nonempty(copy?.approval_id) || !nonempty(copy?.proposal_id) || !validEnvelope(copy) || !copy.payload || typeof copy.payload !== "object" || Array.isArray(copy.payload) || copy.frozen === true || copy.blocked === true || ["blocked", "frozen"].includes(copy.status)) {
        throw new Error(text("服务端预览缺少精确操作、批准标识或摘要。", "Server preview missing exact action, approval IDs or digest."));
      }
      lane.proposal = freeze(copy); lane.json = JSON.stringify(copy, null, 2);
      lane.binding = Object.freeze({ wid: workspace, request: JSON.stringify(requested) });
      lane.preview.textContent = lane.json; lane.digest.textContent = copy.digest;
    } catch (error) {
      syncContext();
      if (ticket === generation && workspace === wid) { clear(lane); report(error); }
    }
    finally { if (ticket === generation) busy = false; update(); }
  }
  async function execute(lane) {
    syncContext(); update();
    if (lane.execute.disabled) return;
    const proposal = lane.proposal, workspace = wid;
    const body = { approval_id: proposal.approval_id, proposal_id: proposal.proposal_id, confirmed: true };
    if (proposal.action === "git_push") body.confirm_push = true;
    clear(lane); errorText.textContent = "";
    const ticket = ++generation; busy = true; update();
    try {
      const result = await post(pathFor(workspace, `/${lane.kind}/execute`), body);
      syncContext();
      if (ticket !== generation || workspace !== wid) return;
      lane.preview.textContent = JSON.stringify(result, null, 2);
      if (result?.status !== "completed" || result?.action !== proposal.action || result?.proposal_digest !== proposal.digest) throw new Error(text("服务端未确认此提案完成；请刷新检查状态。", "Server did not confirm this proposal completed; refresh and inspect state."));
      if (typeof onChanged === "function") await onChanged(result);
    } catch (error) {
      syncContext();
      if (ticket === generation && workspace === wid) report(error);
    }
    finally { if (ticket === generation) busy = false; update(); }
  }
  async function refresh(context = {}) {
    invalidate(); errorText.textContent = ""; capabilities = {};
    productionStatus.textContent = text("生产能力未接入。", "Production capabilities not connected.");
    if (Object.hasOwn(context, "workspaceId")) wid = context.workspaceId;
    if (Object.hasOwn(context, "frozen")) frozen = context.frozen === true;
    if (Object.hasOwn(context, "recoveryRequired")) recoveryRequired = context.recoveryRequired === true;
    syncContext(); update();
    if (!wid) return;
    const workspace = wid, ticket = generation;
    try {
      const result = await api(pathFor(workspace, "/git/capabilities"));
      syncContext();
      if (ticket !== generation || workspace !== wid) return;
      capabilities = result && typeof result === "object" && !Array.isArray(result) ? result : {};
      try {
        const production = await api(pathFor(workspace, "/production/capabilities"));
        syncContext();
        if (ticket === generation && workspace === wid) productionStatus.textContent = JSON.stringify(production, null, 2);
      } catch (error) {
        syncContext();
        if (ticket === generation && workspace === wid) productionStatus.textContent = text("生产能力状态不可用。", "Production capability status unavailable.");
      }
    } catch (error) {
      syncContext();
      if (ticket !== generation || workspace !== wid) return;
      report(error);
      try {
        const production = await api(pathFor(workspace, "/production/capabilities"));
        syncContext();
        if (ticket === generation && workspace === wid) productionStatus.textContent = JSON.stringify(production, null, 2);
      } catch {
        syncContext();
        if (ticket === generation && workspace === wid) productionStatus.textContent = text("生产能力状态不可用。", "Production capability status unavailable.");
      }
    } finally { update(); }
  }
  action.addEventListener("change", () => { syncContext(); generation++; clear(git); renderFields(); update(); });
  renderFields();
  element.append(node("h2", {}, text("工程操作", "Engineering operations")), status, productionStatus, errorText, node("label", {}, text("操作", "Action"), action), fieldArea, git.section, recovery.section);
  syncContext(); update();
  return { element, refresh, setExternalBusy(value) { externalBusy = value === true; update(); } };
}
