import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
const source = await readFile(new URL('../longflow/web/safety.js', import.meta.url), 'utf8');
const { normalizeSafety, validateContract, createSafetyPanel, renderDevelopmentTaskForm } = await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.events = {}; this.textContent = ''; this.value = ''; this.checked = false; }
  setAttribute(k,v) { this.attrs[k] = v; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(k,f) { this.events[k] = f; }
}
globalThis.document = { createElement: tag => new Element(tag), createTextNode: text => ({ textContent: text }) };
const all = node => [node, ...(node.children || []).flatMap(all)];
const contents = node => all(node).map(n => n.textContent).join('\n');
const contract = { repository: 'repo', branch: 'dev', allowed_dirs: ['web'], forbidden_dirs: ['secrets'], allow_dependencies: false, allow_config: false, database: 'none', environment: 'local', acceptance: ['regression fixed'], required_tests: ['node --test'], constraints: [], stop_conditions: ['unexpected changes'] };
const tick = async () => { for (let i=0;i<12;i++) await Promise.resolve(); };
test('default creation posts only development fields and exposes backend errors', async () => {
  const app = new Element('main'); let fail = false; const calls = []; let created;
  const el = (tag, attrs = {}, ...children) => {
    const n = new Element(tag); for (const [k,v] of Object.entries(attrs)) {
      if (k.startsWith('on')) n.events[k.slice(2).toLowerCase()] = v; else n.attrs[k] = v;
    }
    n.append(...children.map(c => typeof c === 'string' ? { textContent: c } : c)); return n;
  };
  renderDevelopmentTaskForm(app, { el, errorHint: e => e.message, onBack() {}, onCreated: id => { created = id; }, api: async (url, options) => {
    if (fail) throw new Error('creation denied'); calls.push({ url, body: JSON.parse(options.body) }); return { task_id: 'new' };
  } });
  all(app).find(n => n.attrs?.id === 'f-goal').value = 'fix bug';
  const submit = all(app).find(n => n.tag === 'form').events.submit;
  await submit({ preventDefault() {} });
  assert.equal(calls[0].url, '/api/development/tasks'); assert.deepEqual(calls[0].body, { goal: 'fix bug' }); assert.equal(created, 'new');
  fail = true; await submit({ preventDefault() {} }); assert.match(contents(app), /creation denied/);
});
test('adapts server snapshot and never coerces unknown freeze to safe', () => {
  const s = normalizeSafety({ control: { frozen: true, epoch: 4 }, progress: { execution_status: 'in_doubt', data_recovery: 'not_connected' }, candidates: [{ unified_diff: '+code' }] });
  assert.equal(s.frozen, true); assert.equal(s.epoch, 4); assert.equal(s.execution, 'in_doubt'); assert.equal(s.recovery.database, 'not_connected'); assert.equal(s.diff[0].unified_diff, '+code');
  assert.equal(normalizeSafety({ frozen: 'false' }).frozen, null);
});
test('contract validates types and excludes injection of extra authority', () => {
  assert.equal(validateContract({ ...contract, allowed_tools: ['deploy'], confirmed: false }).confirmed, true);
  assert.equal(validateContract({ ...contract, allowed_tools: ['deploy'] }).allowed_tools, undefined);
  assert.throws(() => validateContract({ ...contract, required_tests: [] }));
  assert.throws(() => validateContract({ ...contract, allow_config: 'true' }));
});
test('panel uses text, preserves edited drafts, confirms global freeze and exposes errors', async () => {
  const calls = []; const confirms = []; let fail = false;
  const api = async (url, options) => {
    calls.push({ url, options });
    if (fail) throw new Error('backend unavailable');
    return { control: { frozen: false, epoch: 1 }, contract: { engineering: contract }, candidates: [{ unified_diff: '<img src=x onerror=alert(1)>' }] };
  };
  const panel = createSafetyPanel('task/1', { api, confirmAction: msg => { confirms.push(msg); return true; } });
  await panel.refresh();
  assert.equal(calls[0].url, '/api/tasks/task%2F1/safety');
  assert.match(contents(panel.element), /<img src=x onerror=alert\(1\)>/);
  assert.equal(all(panel.element).some(n => n.tag === 'img'), false);
  const draft = all(panel.element).find(n => n.tag === 'textarea');
  draft.value = 'unsaved draft'; draft.events.input(); await panel.refresh(); assert.equal(draft.value, 'unsaved draft');
  const freeze = all(panel.element).find(n => n.textContent === '全局冻结');
  freeze.events.click(); assert.equal(confirms.length, 0); assert.match(contents(panel.element), /请填写全局冻结原因/);
  all(panel.element).find(n => n.attrs?.['aria-label'] === '安全控制原因').value = 'review';
  freeze.events.click(); await tick();
  assert.match(confirms[0], /所有任务/); assert.ok(calls.some(c => c.url === '/api/safety/freeze' && JSON.parse(c.options.body).reason === 'review'));
  fail = true; await panel.refresh(); assert.match(contents(panel.element), /backend unavailable/);
  assert.equal(all(panel.element).find(n => n.textContent === '确认开发契约').disabled, true);
});
test('unfreeze warns approvals stay revoked and contract sends explicit confirmation', async () => {
  const calls = [], confirms = [];
  let frozen = true;
  const panel = createSafetyPanel('t', { api: async (url, options) => {
    calls.push({ url, options }); return { control: { frozen }, contract: { engineering: contract } };
  }, confirmAction: msg => { confirms.push(msg); return true; } });
  await panel.refresh();
  all(panel.element).find(n => n.attrs?.['aria-label'] === '安全控制原因').value = 'review done';
  all(panel.element).find(n => n.textContent === '解冻（不恢复审批）').events.click(); await tick();
  assert.match(confirms[0], /不会恢复旧审批/);
  frozen = false; await panel.refresh();
  all(panel.element).find(n => n.attrs?.type === 'checkbox').checked = true;
  all(panel.element).find(n => n.textContent === '确认开发契约').events.click(); await tick();
  assert.ok(calls.some(c => c.url === '/api/tasks/t/development/contract' && JSON.parse(c.options.body).confirmed === true));
});
