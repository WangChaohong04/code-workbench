// Retained DOM-mocked tests. No commands, tests or candidate code are run while authoring.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
const raw = await readFile(new URL('../longflow/web/operations.js', import.meta.url), 'utf8');
const source = raw.replace('import { currentLocale } from "./i18n.js";', 'const currentLocale = () => globalThis.mockLocale || "en";');
const { createOperationsPanel } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
class Element {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.children = []; this.events = {}; this.value = ''; this.checked = false; this.disabled = false; this.hidden = false; this._text = ''; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  setAttribute(name, value) { this.attrs[name] = value; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  addEventListener(name, listener) { (this.events[name] ||= []).push(listener); }
  fire(name) { return Promise.all((this.events[name] || []).map(listener => listener({ target: this }))); }
}
const all = root => [root, ...(root.children || []).flatMap(all)];
const label = (root, name) => all(root).find(el => el.attrs?.['aria-label'] === name);
const button = (root, name) => all(root).find(el => el.tag === 'button' && el.textContent === name);
const capabilities = () => Object.fromEntries(['git_branch', 'git_merge', 'git_push', 'local_recovery'].map(action => [action, { status: 'configured' }]));
const proposal = action => ({ action, approval_id: 'approval', proposal_id: 'proposal', digest: 'a'.repeat(64), epoch: 0, expires_at: Date.now() / 1000 + 300, payload: { diff: '<img src=x onerror=bad()>', ref: 'refs/heads/topic' } });
async function setup({ caps = capabilities(), response, failure, getter = false, locale = 'en', changed = () => {} } = {}) {
  globalThis.mockLocale = locale;
  globalThis.document = { createElement: tag => new Element(tag), createTextNode: text => ({ textContent: text }) };
  const calls = [], errors = [];
  const context = { workspaceId: 'space/a', frozen: false, recoveryRequired: false };
  let returned;
  const api = async (url, options) => {
    const body = options?.body ? JSON.parse(options.body) : undefined;
    calls.push({ url, method: options?.method, body });
    if (url.endsWith('/production/capabilities')) return { ci_cd: { status: 'not_connected' }, provider_execution_wired: false };
    if (url.endsWith('/git/capabilities')) { if (failure === 'capabilities') throw new Error('capabilities unavailable'); return caps; }
    if (failure === 'proposal') throw new Error('source_conflict');
    if (url.endsWith('/proposals')) {
      returned = response ? response(body) : proposal(url.includes('/recovery/') ? 'local_recovery' : body.action);
      return returned;
    }
    if (failure === 'execute') throw new Error('approval_consumed');
    return { status: 'completed', action: returned.action, proposal_digest: returned.digest };
  };
  const panel = createOperationsPanel({ api, context: getter ? () => context : undefined, onChanged: changed, onError: error => errors.push(error) });
  await panel.refresh(context);
  return { ...panel, calls, errors, context, returned: () => returned };
}
async function fill(root, values) {
  for (const [name, value] of Object.entries(values)) { const input = all(root).find(el => el.attrs?.name === name); assert.ok(input, name); input.value = value; await input.fire('input'); }
}
async function check(root, name) { const input = label(root, name); input.checked = true; await input.fire('change'); }
async function select(root, action) { const input = label(root, 'Git action'); input.value = action; await input.fire('change'); }
const branch = { branch: 'topic', base_branch: 'main' };

test('Git failure fallback ignores delayed production state after workspace changes', async () => {
  globalThis.mockLocale = 'en';
  globalThis.document = { createElement: tag => new Element(tag), createTextNode: text => ({ textContent: text }) };
  const context = { workspaceId: 'old', frozen: false, recoveryRequired: false };
  let resolveProduction;
  const pendingProduction = new Promise(resolve => { resolveProduction = resolve; });
  const panel = createOperationsPanel({ context: () => context, api: async url => {
    if (url.endsWith('/git/capabilities')) throw new Error('Git unavailable');
    return pendingProduction;
  } });
  const refreshing = panel.refresh(context);
  await Promise.resolve();
  context.workspaceId = 'new';
  resolveProduction({ production_deployment: { status: 'configured' }, marker: 'old-workspace' });
  await refreshing;
  assert.equal(label(panel.element, 'Production capability status').textContent.includes('old-workspace'), false);
  assert.equal(button(panel.element, 'Preview git').disabled, true);
});

test('delayed production failure cannot replace status after workspace changes', async () => {
  globalThis.mockLocale = 'en';
  globalThis.document = { createElement: tag => new Element(tag), createTextNode: text => ({ textContent: text }) };
  const context = { workspaceId: 'old', frozen: false, recoveryRequired: false };
  let rejectProduction;
  const pendingProduction = new Promise((resolve, reject) => { rejectProduction = reject; });
  const panel = createOperationsPanel({ context: () => context, api: async url =>
    url.endsWith('/git/capabilities') ? capabilities() : pendingProduction });
  const refreshing = panel.refresh(context);
  await Promise.resolve();
  context.workspaceId = 'new';
  rejectProduction(new Error('Old production request failed'));
  await refreshing;
  assert.equal(label(panel.element, 'Production capability status').textContent, 'Production capabilities not connected.');
  assert.equal(button(panel.element, 'Preview git').disabled, true);
});

test('workspace changes clear previously displayed capability status', async () => {
  const panel = await setup({ getter: true });
  const production = label(panel.element, 'Production capability status');
  production.textContent = JSON.stringify({ production_deployment: { status: 'configured' }, marker: 'old-workspace' });
  panel.context.workspaceId = 'new-workspace';
  await select(panel.element, 'git_merge');
  assert.equal(production.textContent, 'Production capabilities not connected.');
  assert.equal(button(panel.element, 'Preview git').disabled, true);
});

test('delayed proposal errors cannot leak into another workspace', async () => {
  const panel = await setup({ getter: true });
  await fill(panel.element, branch);
  let rejectProposal;
  const pending = new Promise((resolve, reject) => { rejectProposal = reject; });
  const context = panel.context;
  const errors = [];
  const isolated = createOperationsPanel({ context: () => context, onError: error => errors.push(error),
    api: async url => url.endsWith('/proposals') ? pending : url.endsWith('/git/capabilities') ? capabilities() : {} });
  await isolated.refresh(context);
  await fill(isolated.element, branch);
  const proposing = button(isolated.element, 'Preview git').fire('click');
  context.workspaceId = 'new';
  rejectProposal(new Error('Old workspace private detail'));
  await proposing;
  assert.equal(errors.length, 0);
  assert.equal(isolated.element.textContent.includes('Old workspace private detail'), false);
  assert.equal(button(isolated.element, 'Execute git').disabled, true);
});

for (const outcome of ['failure', 'success']) test(`delayed execution ${outcome} cannot affect another workspace`, async () => {
  globalThis.mockLocale = 'en';
  globalThis.document = { createElement: tag => new Element(tag), createTextNode: text => ({ textContent: text }) };
  const context = { workspaceId: 'old', frozen: false, recoveryRequired: false };
  const errors = [], changes = [], executionRequests = [];
  let resolveExecution, rejectExecution;
  const pending = new Promise((resolve, reject) => { resolveExecution = resolve; rejectExecution = reject; });
  const panel = createOperationsPanel({ context: () => context,
    onError: error => errors.push(error), onChanged: result => changes.push(result),
    api: async (url, options) => {
      if (url.endsWith('/git/capabilities')) return capabilities();
      if (url.endsWith('/proposals')) return proposal('git_branch');
      if (url.endsWith('/execute')) {
        executionRequests.push({ url, method: options.method, body: JSON.parse(options.body) });
        return pending;
      }
      return {};
    } });
  await panel.refresh(context);
  await fill(panel.element, branch);
  await button(panel.element, 'Preview git').fire('click');
  await check(panel.element, 'Git confirmation');
  assert.equal(button(panel.element, 'Execute git').disabled, false);
  const executing = button(panel.element, 'Execute git').fire('click');
  assert.deepEqual(executionRequests, [{ url: '/api/local-workspaces/old/git/execute', method: 'POST',
    body: { approval_id: 'approval', proposal_id: 'proposal', confirmed: true } }]);
  context.workspaceId = 'new';
  if (outcome === 'failure') rejectExecution(new Error('Old execution private detail'));
  else resolveExecution({ status: 'completed', action: 'git_branch', proposal_digest: 'a'.repeat(64), marker: 'old-result' });
  await executing;
  assert.equal(errors.length, 0);
  assert.equal(changes.length, 0);
  assert.equal(panel.element.textContent.includes('Old execution private detail'), false);
  assert.equal(panel.element.textContent.includes('old-result'), false);
  assert.equal(button(panel.element, 'Execute git').disabled, true);
});

test('production state is read-only and does not expose provider execution', async () => {
  const panel = await setup();
  const state = label(panel.element, 'Production capability status');
  assert.equal(JSON.parse(state.textContent).ci_cd.status, 'not_connected');
  assert.equal(JSON.parse(state.textContent).provider_execution_wired, false);
  assert.equal(panel.calls.filter(call => call.url.includes('/production/')).length, 1);
  assert.equal(panel.calls.filter(call => call.url.includes('/production/')).every(call => !call.method), true);
});

test('unavailable capabilities default closed; recovery is independent of Git', async () => {
  for (const options of [{ caps: {} }, { failure: 'capabilities' }]) {
    const panel = await setup(options); await fill(panel.element, branch);
    assert.equal(button(panel.element, 'Preview git').disabled, true);
    await button(panel.element, 'Preview git').fire('click');
    assert.equal(panel.calls.some(call => call.url.endsWith('/git/proposals')), false);
    assert.equal(button(panel.element, 'Preview recovery').disabled, false);
    await button(panel.element, 'Preview recovery').fire('click');
    const call = panel.calls.find(call => call.url.endsWith('/recovery/proposals'));
    assert.equal(call.method, 'POST'); assert.deepEqual(call.body, {});
    assert.equal(label(panel.element, 'Recovery confirmation').checked, false);
  }
});

test('all Git actions post exactly their own fields and never auto-execute', async () => {
  const panel = await setup();
  for (const [action, values] of [['git_branch', branch], ['git_merge', { target_branch: 'topic', source_branch: 'main' }], ['git_push', { branch: 'topic', remote: 'https://example.test/repo', remote_branch: 'topic' }]]) {
    await select(panel.element, action); await fill(panel.element, values);
    const names = all(panel.element).filter(el => el.tag === 'input' && el.attrs.name).map(el => el.attrs.name);
    assert.deepEqual(names, Object.keys(values));
    await button(panel.element, 'Preview git').fire('click');
    assert.deepEqual(panel.calls.at(-1), { url: '/api/local-workspaces/space%2Fa/git/proposals', method: 'POST', body: { action, ...values } });
    assert.equal(label(panel.element, 'Git confirmation').checked, false);
    assert.equal(button(panel.element, 'Execute git').disabled, true);
  }
  assert.equal(panel.calls.some(call => call.url.endsWith('/execute')), false);
});

test('preview is immutable JSON rendered as safe text and bound to digest', async () => {
  const panel = await setup(); await fill(panel.element, branch); await button(panel.element, 'Preview git').fire('click');
  const preview = label(panel.element, 'Git server preview');
  const exact = JSON.stringify(panel.returned(), null, 2);
  assert.equal(preview.textContent, exact); assert.equal(label(panel.element, 'Git digest').textContent, 'a'.repeat(64));
  panel.returned().payload.diff = 'mutated after response';
  assert.equal(preview.textContent, exact);
  assert.equal(all(panel.element).some(el => ['img', 'script'].includes(el.tag)), false);
  await check(panel.element, 'Git confirmation'); assert.equal(button(panel.element, 'Execute git').disabled, false);
  preview.textContent = 'tampered'; await label(panel.element, 'Git confirmation').fire('change');
  assert.equal(button(panel.element, 'Execute git').disabled, true);
  await button(panel.element, 'Execute git').fire('click');
  assert.equal(panel.calls.some(call => call.url.endsWith('/execute')), false);
});

test('mismatched actions, missing IDs or digest and blocked previews cannot be approved', async () => {
  const variants = [p => ({ ...p, action: 'git_push' }), p => ({ ...p, approval_id: '' }), p => ({ ...p, proposal_id: null }), p => ({ ...p, digest: '' }), p => ({ ...p, digest: 'not-a-hash' }), p => ({ ...p, epoch: undefined }), p => ({ ...p, epoch: -1 }), p => ({ ...p, epoch: '0' }), p => ({ ...p, expires_at: undefined }), p => ({ ...p, expires_at: 0 }), p => ({ ...p, expires_at: '9999999999' }), p => ({ ...p, payload: null }), p => ({ ...p, status: 'blocked' })];
  for (const variant of variants) {
    const panel = await setup({ response: body => variant(proposal(body.action)) });
    await fill(panel.element, branch); await button(panel.element, 'Preview git').fire('click');
    await check(panel.element, 'Git confirmation'); await button(panel.element, 'Execute git').fire('click');
    assert.equal(button(panel.element, 'Execute git').disabled, true);
    assert.equal(panel.errors.length, 1); assert.equal(panel.calls.some(call => call.url.endsWith('/execute')), false);
  }
});

test('inputs, action changes, refresh and getter workspace changes invalidate consent', async () => {
  const panel = await setup({ getter: true });
  const approve = async () => { await fill(panel.element, branch); await button(panel.element, 'Preview git').fire('click'); await check(panel.element, 'Git confirmation'); };
  await approve(); await fill(panel.element, { branch: 'other' });
  assert.equal(label(panel.element, 'Git confirmation').checked, false);
  await approve(); await select(panel.element, 'git_merge');
  assert.equal(label(panel.element, 'Git confirmation').checked, false);
  await select(panel.element, 'git_branch'); await approve(); await panel.refresh(panel.context);
  assert.equal(label(panel.element, 'Git confirmation').checked, false);
  assert.equal(label(panel.element, 'Git server preview').textContent, '');
  await approve(); panel.context.workspaceId = 'new'; await button(panel.element, 'Execute git').fire('click');
  assert.equal(label(panel.element, 'Git confirmation').checked, false);
  assert.equal(panel.calls.some(call => call.url.endsWith('/execute')), false);
});

test('execute sends only exact approval handle, proposal handle and explicit consent', async () => {
  let changed = 0;
  const panel = await setup({ changed: () => { changed++; } });
  await fill(panel.element, branch); await button(panel.element, 'Preview git').fire('click'); await check(panel.element, 'Git confirmation');
  await button(panel.element, 'Execute git').fire('click');
  assert.deepEqual(panel.calls.at(-1).body, { approval_id: 'approval', proposal_id: 'proposal', confirmed: true });
  assert.equal(changed, 1); assert.equal(label(panel.element, 'Git confirmation').checked, false);
  await button(panel.element, 'Execute git').fire('click');
  assert.equal(panel.calls.filter(call => call.url.endsWith('/execute')).length, 1);
});

test('push requires separate initially unchecked consent and adds confirm_push only for push', async () => {
  const panel = await setup(); await select(panel.element, 'git_push');
  await fill(panel.element, { branch: 'topic', remote: 'https://example.test/repo', remote_branch: 'topic' });
  await button(panel.element, 'Preview git').fire('click'); await check(panel.element, 'Git confirmation');
  assert.equal(label(panel.element, 'Separate push confirmation').checked, false);
  assert.equal(button(panel.element, 'Execute git').disabled, true);
  await button(panel.element, 'Execute git').fire('click'); assert.equal(panel.calls.some(call => call.url.endsWith('/execute')), false);
  await check(panel.element, 'Separate push confirmation'); await button(panel.element, 'Execute git').fire('click');
  assert.deepEqual(panel.calls.at(-1).body, { approval_id: 'approval', proposal_id: 'proposal', confirmed: true, confirm_push: true });
});

test('recovery_required blocks Git but recovery permits explicit preview and execution; freeze blocks both', async () => {
  const panel = await setup({ caps: {} });
  await panel.refresh({ workspaceId: 'space/a', frozen: false, recoveryRequired: true });
  assert.equal(button(panel.element, 'Preview git').disabled, true);
  await button(panel.element, 'Preview recovery').fire('click');
  assert.equal(panel.calls.at(-1).url, '/api/local-workspaces/space%2Fa/recovery/proposals');
  assert.deepEqual(panel.calls.at(-1).body, {});
  await check(panel.element, 'Recovery confirmation'); await button(panel.element, 'Execute recovery').fire('click');
  assert.equal(panel.calls.at(-1).url, '/api/local-workspaces/space%2Fa/recovery/execute');
  assert.deepEqual(panel.calls.at(-1).body, { approval_id: 'approval', proposal_id: 'proposal', confirmed: true });
  await panel.refresh({ workspaceId: 'space/a', frozen: true, recoveryRequired: true });
  const count = panel.calls.length;
  await button(panel.element, 'Preview recovery').fire('click'); await button(panel.element, 'Execute recovery').fire('click');
  assert.equal(panel.calls.length, count); assert.equal(button(panel.element, 'Preview recovery').disabled, true);
});

test('errors do not manufacture success, and failed execution consumes local consent', async () => {
  let changed = 0;
  for (const failure of ['proposal', 'execute']) {
    const panel = await setup({ failure, changed: () => { changed++; } });
    await fill(panel.element, branch); await button(panel.element, 'Preview git').fire('click'); await check(panel.element, 'Git confirmation'); await button(panel.element, 'Execute git').fire('click');
    assert.equal(panel.errors.length, 1); assert.equal(label(panel.element, 'Git confirmation').checked, false);
    assert.equal(button(panel.element, 'Execute git').disabled, true);
  }
  assert.equal(changed, 0);
});

test('late responses are discarded after workspace refresh', async () => {
  globalThis.document = { createElement: tag => new Element(tag), createTextNode: text => ({ textContent: text }) };
  let resolve;
  const panel = createOperationsPanel({ locale: 'en', api: async url => url.endsWith('/capabilities') ? capabilities() : new Promise(done => { resolve = done; }) });
  await panel.refresh({ workspaceId: 'old', frozen: false, recoveryRequired: false });
  await fill(panel.element, branch);
  const pending = button(panel.element, 'Preview git').fire('click');
  await panel.refresh({ workspaceId: 'new', frozen: false, recoveryRequired: false });
  resolve(proposal('git_branch')); await pending;
  assert.equal(label(panel.element, 'Git server preview').textContent, '');
  assert.equal(button(panel.element, 'Execute git').disabled, true);
});

test('Chinese labels follow currentLocale without translating server facts', async () => {
  const panel = await setup({ locale: 'zh' });
  assert.ok(label(panel.element, 'Git 操作')); assert.ok(button(panel.element, '恢复预览'));
  await fill(panel.element, branch); await button(panel.element, 'Git预览').fire('click');
  assert.match(label(panel.element, 'Git服务端预览').textContent, /refs\/heads\/topic/);
});
