// Retained DOM-mocked UI tests. Do not execute without user authorization.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
const raw = await readFile(new URL('../longflow/web/files.js', import.meta.url), 'utf8');
const operations = await readFile(new URL('../longflow/web/operations.js', import.meta.url), 'utf8');
const localeMock = 'const currentLocale = () => globalThis.mockLocale || "en";';
const operationsSource = operations.replace('import { currentLocale } from "./i18n.js";', localeMock);
const operationsUrl = `data:text/javascript;base64,${Buffer.from(operationsSource).toString('base64')}`;
const source = raw.replace('import { currentLocale } from "./i18n.js";', localeMock)
  .replace('import { createOperationsPanel } from "./operations.js";',
    `import { createOperationsPanel } from ${JSON.stringify(operationsUrl)};`);
const { createFileWorkspace } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.events = {}; this.value = ''; this.checked = false; this.disabled = false; this.className = ''; this._text = ''; this.classList = { add: cls => { this.className += ` ${cls}`; } }; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  setAttribute(key, value) { this.attrs[key] = value; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  addEventListener(key, action) { (this.events[key] ||= []).push(action); }
  querySelectorAll(selector) { return all(this).slice(1).filter(el => selector.split(',').some(part => part.startsWith('.') ? el.className?.split(' ').includes(part.slice(1)) : el.tag === part)); }
  fire(event) { for (const action of this.events[event] || []) action({ target: this }); }
}
const all = el => [el, ...(el.children || []).flatMap(all)];
const label = (root, name) => all(root).find(el => el.attrs?.['aria-label'] === name);
const btn = (root, name) => all(root).find(el => el.tag === 'button' && el.textContent === name);
const tick = async () => { for (let i = 0; i < 40; i++) await Promise.resolve(); };
const exactDiff = '--- a/a.js\n+++ b/a.js\n@@ -1 +1 @@\n-old\n+<img src=x onerror=alert(1)>\n';
const preview = { candidate_id: 'candidate', source_hash: 'source', candidate_hash: 'hash', unified_diff: exactDiff, changed_files: ['a.js'], ai_summary: '<script>bad()</script>', ai_context: { omitted_files: 2 }, ai_evidence: { fallback: false } };
async function setup({ available = true, aiFailure = '', locale = 'en', executionAvailable = true, executionConfigured = true, executionFailure = '', nodeCandidate = null, applyFailure = '' } = {}) {
  globalThis.mockLocale = locale;
  globalThis.document = { createElement: tag => new Element(tag), createTextNode: text => ({ textContent: text }) };
  const storage = new Map();
  globalThis.localStorage = { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) };
  globalThis.window = { confirm: () => true };
  const calls = [];
  let runConfirmed = false, runStatus = 'waiting_user', steps = 0, applied = false, recorded = false;
  const evidence = [];
  const nodes = () => nodeCandidate ? [{ id: 'step', status: recorded ? 'completed' : steps > 1 ? 'waiting_user' : steps ? 'blocked' : 'pending', phase: steps > 1 ? 'apply' : 'verify', tests: ['test', 'lint'], depends_on: [], candidate: nodeCandidate }] : [];
  const run = () => ({ id: 'run-1', confirmed: runConfirmed, status: runStatus, reason: steps > 0 ? 'executor_unavailable' : 'plan_confirmation', nodes: nodes(), plan: { subtasks: [{ id: 'step', deps: [], objective: '<script>run()</script>', acceptance: ['review'], allowed_paths: ['a.js'], presets: ['test'] }] } });
  const api = async (url, options) => {
    calls.push({ url, body: options?.body ? JSON.parse(options.body) : undefined });
    if (url.endsWith('/pick')) return { workspace_id: 'workspace' };
    if (url.endsWith('/git/capabilities')) return {
      git_branch: { status: 'not_connected' }, git_merge: { status: 'not_connected' },
      git_push: { status: 'not_connected' }, local_recovery: { status: 'configured' }
    };
    if (url.endsWith('/production/capabilities')) return {
      ci_cd: { status: 'not_connected' }, production_deployment: { status: 'not_connected' },
      database_migration: { status: 'not_connected' }, database_restore: { status: 'not_connected' },
      provider_execution_wired: false, approval_required: true, automatic_execution: false
    };
    if (url.endsWith('/capabilities')) return { ai: { available, effective_driver: available ? 'openai_compatible' : 'local' }, execution: { available: executionAvailable, configured: executionConfigured, presets: ['pytest', 'compileall', 'ruff'], reason: executionConfigured ? 'daemon_not_verified' : 'disabled' } };
    if (url.endsWith('/execution/approvals')) { if (executionFailure) throw new Error(executionFailure); return { approval_id: `exec-${JSON.parse(options.body).preset}` }; }
    if (url.endsWith('/execution/run')) {
      const preset = JSON.parse(options.body).approval_id.slice(5);
      const value = nodeCandidate || preview;
      const result = { candidate_id: value.candidate_id, candidate_sha: value.candidate_hash, preset, passed: true, status: 'completed', output: '<img src=x onerror=alert(1)>' };
      evidence.push({ candidate_id: value.candidate_id, source_hash: value.source_hash, candidate_hash: value.candidate_hash, preset, expires_at: Math.floor(Date.now() / 1000) + 300, evidence: result });
      return result;
    }
    if (url.includes('/execution/evidence/')) return { evidence };
    if (url.endsWith('/approvals')) return { approval_id: 'write-approval' };
    if (url.endsWith('/apply')) { if (applyFailure) throw new Error(applyFailure); applied = true; return { commit: 'actual-commit' }; }
    if (url.endsWith('/versions')) return { versions: [] };
    if (url.includes('/file?')) return { content: 'old' };
    if (url.includes('/ai/')) {
      if (aiFailure) throw new Error(aiFailure);
      if (url.endsWith('/analyze')) return { summary: '<img src=x onerror=alert(1)>', files: [{ path: 'a.js', summary: 'source' }], unknowns: ['omitted'], context: { omitted_files: 1 }, evidence: { fallback: false }, allowed_paths: ['config.json'] };
      if (url.endsWith('/plan')) return { subtasks: [{ id: 'step', deps: [], objective: '<script>bad()</script>', acceptance: ['review'], allowed_paths: ['config.json'], presets: ['test', 'lint'] }], context: {}, evidence: { fallback: false } };
      return preview;
    }
    if (url.endsWith('/runs')) return run();
    if (url.includes('/runs/run-1')) {
      if (url.endsWith('/confirm')) { runConfirmed = true; runStatus = 'pending'; }
      if (applied && !url.endsWith('/record-applied')) throw new Error('source hash mismatch until receipt recorded');
      if (url.endsWith('/step')) { steps++; runStatus = nodeCandidate && steps > 1 ? 'waiting_user' : 'blocked'; }
      if (url.endsWith('/record-applied')) { assert.equal(applied, true); recorded = true; runStatus = 'completed'; }
      if (url.endsWith('/pause')) runStatus = 'paused';
      if (url.endsWith('/resume')) runStatus = runConfirmed ? 'pending' : 'waiting_user';
      if (url.endsWith('/stop')) runStatus = 'stopped';
      return run();
    }
    if (url.endsWith('/candidates')) return preview;
    return { source: 'folder', files: [{ path: 'a.js' }, 'b.js', { path: 'config.json' }] };
  };
  const root = createFileWorkspace({ api });
  btn(root, '选择本机文件夹').fire('click'); await tick();
  return { root, calls, api, storage };
}
function consent(root) {
  label(root, 'AI goal').value = 'fix source'; label(root, 'AI goal').fire('input');
  label(root, 'Consent to send code').checked = true; label(root, 'Consent to send code').fire('change');
}
function check(root, name, checked = true) { const control = label(root, name); control.checked = checked; control.fire('change'); }
test('workspace reads separate operation capabilities without granting execution', async () => {
  const { root, calls } = await setup();
  assert.equal(calls.some(call => call.url.endsWith('/git/capabilities')), true);
  assert.equal(calls.some(call => call.url.endsWith('/production/capabilities')), true);
  const production = JSON.parse(label(root, 'Production capability status').textContent);
  assert.equal(production.production_deployment.status, 'not_connected');
  assert.equal(production.database_restore.status, 'not_connected');
  assert.equal(production.automatic_execution, false);
  assert.equal(btn(root, 'Preview git').disabled, true);
  assert.equal(btn(root, 'Execute git').disabled, true);
  assert.equal(calls.some(call => /\/(proposals|execute)$/.test(call.url)), false);
});
test('goal and explicit code consent gate requests; analyze/plan stay inert and never grant scope', async () => {
  const { root, calls } = await setup();
  assert.equal(label(root, 'Consent to send code').checked, false);
  assert.equal(btn(root, 'Analyze').disabled, true);
  btn(root, 'Analyze').fire('click'); await tick();
  assert.equal(calls.filter(call => call.url.includes('/ai/')).length, 0);
  consent(root);
  btn(root, 'Analyze').fire('click'); await tick();
  btn(root, 'Plan').fire('click'); await tick();
  for (const call of calls.filter(call => /\/ai\/(analyze|plan)$/.test(call.url))) assert.deepEqual(call.body, { goal: 'fix source', confirmed_send_code: true });
  assert.match(label(root, 'AI analysis result').textContent, /<img/);
  assert.match(label(root, 'AI plan result').textContent, /presets/);
  assert.equal(all(root).some(el => ['img', 'script'].includes(el.tag)), false);
  assert.equal(label(root, 'Allow proposal changes config.json').checked, false);
  assert.equal(btn(root, 'Propose').disabled, true);
  assert.equal(calls.some(call => /apply|approvals|execute|deploy/.test(call.url)), false);
});
test('proposal uses exact checked paths and independent sensitive consent', async () => {
  const { root, calls } = await setup(); consent(root);
  check(root, 'Allow proposal changes a.js');
  check(root, 'Allow proposal changes config.json');
  check(root, 'Sensitive path authorization config.json');
  assert.equal(btn(root, 'Propose').disabled, true);
  btn(root, 'Propose').fire('click'); await tick();
  assert.equal(calls.some(call => call.url.endsWith('/ai/propose')), false);
  check(root, 'Separate sensitive path consent');
  btn(root, 'Propose').fire('click'); await tick();
  assert.deepEqual(calls.find(call => call.url.endsWith('/ai/propose')).body, { goal: 'fix source', confirmed_send_code: true, allowed_paths: ['a.js', 'config.json'], sensitive_paths: ['config.json'], confirmed_sensitive: true });
  assert.equal(label(root, '精确统一差异').textContent, exactDiff);
  assert.match(label(root, 'AI proposal notes').textContent, /<script>/);
  assert.equal(label(root, '确认已审阅精确差异').checked, false);
  assert.equal(btn(root, '批准并应用').disabled, true);
  assert.equal(calls.some(call => /apply|approvals/.test(call.url)), false);
  check(root, 'Allow proposal changes config.json', false);
  assert.equal(label(root, 'Sensitive path authorization config.json').checked, false);
  assert.equal(label(root, 'Separate sensitive path consent').checked, false);
  assert.notEqual(label(root, '精确统一差异').textContent, exactDiff);
});
test('switching files and staging edits invalidate existing candidate approval', async () => {
  const { root } = await setup(); consent(root); check(root, 'Allow proposal changes a.js');
  btn(root, 'Propose').fire('click'); await tick();
  check(root, '确认已审阅精确差异'); assert.equal(btn(root, '批准并应用').disabled, false);
  btn(root, 'a.js').fire('click'); await tick();
  assert.equal(btn(root, '批准并应用').disabled, true);
  btn(root, 'Propose').fire('click'); await tick();
  const editor = label(root, '文件编辑器'); editor.value = 'draft'; editor.fire('input');
  assert.notEqual(label(root, '精确统一差异').textContent, exactDiff);
  btn(root, '生成候选').fire('click'); await tick();
  assert.equal(label(root, '精确统一差异').textContent, exactDiff);
  btn(root, 'b.js').fire('click'); await tick();
  assert.equal(label(root, '确认已审阅精确差异').checked, false);
  assert.equal(btn(root, '批准并应用').disabled, true);
});
test('local driver remains unavailable and real endpoint failures are errors, not success', async () => {
  const local = await setup({ available: false }); consent(local.root);
  assert.equal(btn(local.root, 'Analyze').disabled, true);
  assert.match(local.root.textContent, /AI unavailable: local driver/);
  btn(local.root, 'Analyze').fire('click'); await tick();
  assert.equal(local.calls.some(call => call.url.includes('/ai/')), false);
  const failed = await setup({ aiFailure: 'ai_unavailable: fallback rejected' }); consent(failed.root);
  check(failed.root, 'Allow proposal changes a.js'); btn(failed.root, 'Propose').fire('click'); await tick();
  assert.match(failed.root.textContent, /ai_unavailable: fallback rejected/);
  assert.equal(label(failed.root, 'AI proposal notes').textContent, '');
  assert.equal(btn(failed.root, '批准并应用').disabled, true);
});
test('manual runs require plan review and user-selected exact scope, never auto-step', async () => {
  const { root, calls } = await setup(); consent(root);
  btn(root, 'Create run plan').fire('click'); await tick();
  assert.deepEqual(calls.find(call => call.url.endsWith('/runs')).body, { goal: 'fix source', confirmed_send_code: true });
  assert.equal(calls.some(call => call.url.endsWith('/step')), false);
  assert.match(label(root, 'Run plan').textContent, /<script>run/);
  assert.equal(label(root, 'Allow proposal changes a.js').checked, false);
  check(root, 'Confirm run plan'); btn(root, 'Confirm plan').fire('click'); await tick();
  assert.equal(calls.some(call => call.url.endsWith('/confirm')), false);
  check(root, 'Allow proposal changes a.js'); check(root, 'Allow proposal changes b.js');
  btn(root, 'Confirm plan').fire('click'); await tick();
  assert.deepEqual(calls.find(call => call.url.endsWith('/confirm')).body, { confirmed: true, allowed_paths: ['a.js'] });
  assert.equal(calls.some(call => call.url.endsWith('/step')), false);
  btn(root, 'Advance one step').fire('click'); await tick();
  assert.deepEqual(calls.find(call => call.url.endsWith('/step')).body, { confirmed_send_code: true });
  assert.equal(calls.filter(call => call.url.endsWith('/step')).length, 1);
  assert.match(label(root, 'Run status').textContent, /executor_unavailable/);
  assert.equal(btn(root, 'Advance one step').disabled, true);
  btn(root, 'Pause run').fire('click'); await tick();
  assert.deepEqual(calls.find(call => call.url.endsWith('/pause')).body, {});
  btn(root, 'Resume run').fire('click'); await tick();
  assert.deepEqual(calls.find(call => call.url.endsWith('/resume')).body, {});
  btn(root, 'Stop run').fire('click'); await tick();
  assert.deepEqual(calls.find(call => call.url.endsWith('/stop')).body, {});
  assert.equal(calls.filter(call => call.url.endsWith('/step')).length, 1);
});
test('new workspace clears scope and all sharing authorizations; Chinese labels are explicit', async () => {
  const { root } = await setup(); consent(root); check(root, 'Allow proposal changes a.js');
  btn(root, '选择本机文件夹').fire('click'); await tick();
  assert.equal(label(root, 'Consent to send code').checked, false);
  assert.equal(label(root, 'Allow proposal changes a.js').checked, false);
  const zh = await setup({ locale: 'zh' });
  assert.ok(label(zh.root, 'AI 目标'));
  assert.ok(label(zh.root, '单独同意敏感路径'));
  assert.ok(btn(zh.root, '分析'));
});
async function proposeCandidate(root) {
  consent(root); check(root, 'Allow proposal changes a.js');
  btn(root, 'Propose').fire('click'); await tick();
}
async function executePreset(root, preset) {
  const select = label(root, 'Candidate execution preset'); select.value = preset; select.fire('change');
  check(root, 'Approve current candidate execution');
  btn(root, 'Approve and execute candidate').fire('click'); await tick();
}
test('execution requires separate hash-bound consent and preset; evidence and output are safe text', async () => {
  const { root, calls } = await setup(); await proposeCandidate(root);
  assert.deepEqual(label(root, 'Candidate execution preset').children.map(el => el.attrs.value), ['pytest', 'compileall', 'ruff']);
  assert.equal(label(root, 'Approve current candidate execution').checked, false);
  btn(root, 'Approve and execute candidate').fire('click'); await tick();
  assert.equal(calls.some(call => call.url.includes('/execution/')), false);
  await executePreset(root, 'pytest');
  const operations = calls.filter(call => call.url.includes('/execution/'));
  assert.deepEqual(operations.map(call => call.url.split('/execution/')[1]), ['approvals', 'run', 'evidence/candidate']);
  assert.deepEqual(operations[0].body, { candidate_id: 'candidate', source_hash: 'source', candidate_hash: 'hash', preset: 'pytest', confirmed: true });
  assert.deepEqual(operations[1].body, { approval_id: 'exec-pytest' });
  assert.equal(operations[2].body, undefined);
  assert.match(label(root, 'Candidate execution result').textContent, /<img/);
  assert.equal(all(root).some(el => ['img', 'script'].includes(el.tag)), false);
  assert.equal(label(root, 'Approve current candidate execution').checked, false);
  assert.equal(label(root, '确认已审阅精确差异').checked, false);
  check(root, 'Approve current candidate execution');
  label(root, 'Candidate execution preset').value = 'ruff'; label(root, 'Candidate execution preset').fire('change');
  assert.equal(label(root, 'Approve current candidate execution').checked, false);
  check(root, 'Approve current candidate execution');
  label(root, 'AI goal').fire('input');
  assert.equal(label(root, 'Approve current candidate execution').checked, false);
  assert.equal(label(root, 'Candidate execution result').textContent, '');
  assert.equal(btn(root, 'Approve and execute candidate').disabled, true);
});
test('execution unavailable configuration and endpoint errors never yield fake passing evidence', async () => {
  for (const config of [{ executionAvailable: false }, { executionConfigured: false }]) {
    const { root, calls } = await setup(config); await proposeCandidate(root);
    assert.equal(btn(root, 'Approve and execute candidate').disabled, true);
    check(root, 'Approve current candidate execution'); btn(root, 'Approve and execute candidate').fire('click'); await tick();
    assert.equal(calls.some(call => call.url.endsWith('/execution/run')), false);
    assert.equal(label(root, 'Candidate execution result').textContent, '');
  }
  const { root, calls } = await setup({ executionFailure: 'sandbox unavailable' }); await proposeCandidate(root); await executePreset(root, 'pytest');
  assert.match(label(root, 'Candidate execution result').textContent, /sandbox unavailable/);
  assert.equal(calls.some(call => call.url.endsWith('/execution/run')), false);
});
test('stored run node preview drives execution, blocked verification and explicit receipt recording without stale read', async () => {
  const stored = { ...preview, candidate_id: 'stored-node', candidate_hash: 'stored-hash', unified_diff: exactDiff + 'stored exact preview\n' };
  const { root, calls, api, storage } = await setup({ nodeCandidate: stored }); consent(root); check(root, 'Allow proposal changes a.js');
  btn(root, 'Create run plan').fire('click'); await tick();
  check(root, 'Confirm run plan'); btn(root, 'Confirm plan').fire('click'); await tick();
  btn(root, 'Advance one step').fire('click'); await tick();
  assert.equal(btn(root, 'Advance one step').disabled, true);
  btn(root, 'Load node candidate step').fire('click'); await tick();
  assert.equal(label(root, '精确统一差异').textContent, stored.unified_diff);
  assert.equal(label(root, 'Approve current candidate execution').checked, false);
  await executePreset(root, 'pytest');
  assert.equal(btn(root, 'Advance one step').disabled, true);
  await executePreset(root, 'ruff');
  assert.equal(btn(root, 'Advance one step').disabled, false);
  assert.equal(calls.filter(call => call.url.endsWith('/step')).length, 1);
  btn(root, 'Advance one step').fire('click'); await tick();
  btn(root, 'Load node candidate step').fire('click'); await tick();
  assert.equal(btn(root, 'Record applied').disabled, true);
  btn(root, 'Record applied').fire('click'); await tick();
  assert.equal(calls.some(call => call.url.endsWith('/record-applied')), false);
  check(root, '确认已审阅精确差异'); btn(root, '批准并应用').fire('click'); await tick();
  assert.equal(btn(root, 'Record applied').disabled, false);
  const applyIndex = calls.findIndex(call => call.url.endsWith('/apply'));
  assert.equal(calls.slice(applyIndex + 1).some(call => call.url.endsWith('/runs/run-1')), false);
  assert.equal(calls.some(call => call.url.endsWith('/record-applied')), false);
  const retained = JSON.parse(storage.get('longflow.fileWorkspaceApplied.workspace'));
  assert.equal(retained.nodeId, 'step');
  const restored = createFileWorkspace({ api }); await tick();
  assert.match(restored.textContent, /Local pending-application state only/);
  assert.equal(btn(restored, 'Advance one step').disabled, true);
  assert.equal(btn(restored, 'Record applied').disabled, false);
  btn(restored, 'Record applied').fire('click'); await tick();
  assert.deepEqual(calls.find(call => call.url.endsWith('/record-applied')).body, { node_id: 'step', confirmed: true });
  assert.match(label(restored, 'Run status').textContent, /completed/);
  assert.equal(btn(restored, 'Record applied').disabled, true);
  assert.equal(storage.has('longflow.fileWorkspaceApplied.workspace'), false);
});
