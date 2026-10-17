const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/js/loyalty-tree.js', 'utf8');

function setup(response, initial = false) {
  const attrs = {'aria-expanded': 'false', 'aria-controls': 'children'};
  const button = {
    dataset: {treeUrl: '/admin/loyalty/tree/?parent=1&fragment=children'},
    hidden: true,
    getAttribute: key => attrs[key],
    setAttribute: (key, value) => { attrs[key] = value; },
    removeAttribute: key => { delete attrs[key]; },
    closest: selector => selector === '[data-tree-toggle]' ? button : null,
  };
  const children = {hidden: true, dataset: {}, querySelectorAll: () => [], replaceChildren: content => {children.content = content;}};
  const status = {textContent: ''};
  let handler, calls = 0;
  const tree = {
    querySelector: selector => selector === '[data-tree-status]' ? status : button,
    querySelectorAll: () => [button],
    contains: () => true,
    addEventListener: (_, fn) => { handler = fn; },
    hasAttribute: () => initial,
  };
  const page = {
    querySelector: () => tree,
    getElementById: () => children,
    createElement: () => ({content: {fragment: true}}),
  };
  button.click = () => handler({target: button});
  vm.runInNewContext(source, {
    document: page,
    window: {location: {href: 'https://example.invalid/admin/loyalty/tree/', origin: 'https://example.invalid'}},
    URL,
    fetch: async (url, options) => {
      calls++;
      assert.equal(url.origin, 'https://example.invalid');
      assert.equal(url.searchParams.get('fragment'), 'children');
      assert.equal(options.cache, 'no-store');
      assert.equal(options.credentials, 'same-origin');
      return await response();
    },
  });
  return {button, children, status, attrs, tree, page, click: target => handler({target, preventDefault() {}}), calls: () => calls};
}

const success = async () => ({ok: true, redirected: false, headers: {get: () => 'children'}, text: async () => '<ul></ul>'});

test('opens a branch once, collapses and reopens without another request', async () => {
  const env = setup(success);
  assert.equal(env.button.hidden, false);
  await env.click(env.button);
  assert.equal(env.calls(), 1);
  assert.equal(env.children.hidden, false);
  assert.equal(env.attrs['aria-expanded'], 'true');
  await env.click(env.button);
  assert.equal(env.children.hidden, true);
  await env.click(env.button);
  assert.equal(env.calls(), 1);
  assert.equal(env.children.hidden, false);
});

test('login redirects or unexpected response pages are never inserted as a tree', async () => {
  for (const response of [
    {ok: true, redirected: true, headers: {get: () => 'children'}},
    {ok: true, redirected: false, headers: {get: () => null}},
    {ok: false},
  ]) {
    const env = setup(async () => response);
    await env.click(env.button);
    assert.equal(env.children.content, undefined);
    assert.equal(env.attrs['aria-expanded'], 'false');
    assert.equal(env.button.disabled, false);
    assert.match(env.status.textContent, /تعذر/);
  }
});

test('failed request is recoverable and rapid repeated clicks do not duplicate it', async () => {
  let release;
  const env = setup(() => new Promise(resolve => {release = resolve;}));
  const pending = env.click(env.button);
  await env.click(env.button);
  assert.equal(env.calls(), 1);
  release(await success());
  await pending;
  assert.equal(env.button.disabled, false);
  const failed = setup(async () => {throw new Error('offline');});
  await failed.click(failed.button);
  await failed.click(failed.button);
  assert.equal(failed.calls(), 2);
  assert.equal(failed.children.content, undefined);
});

test('collapse-all keeps data but hides every expanded branch', async () => {
  const env = setup(success);
  await env.click(env.button);
  const control = {closest: selector => selector === '[data-tree-collapse]' ? control : null};
  await env.click(control);
  assert.equal(env.children.hidden, true);
  assert.equal(env.children.dataset.loaded, 'true');
  assert.match(env.status.textContent, /تم طي/);
});

test('cross-origin requests are rejected and there are no mutation or storage endpoints', async () => {
  const env = setup(success);
  env.button.dataset.treeUrl = 'https://outside.invalid/';
  await env.click(env.button);
  assert.equal(env.calls(), 0);
  assert.equal(env.children.content, undefined);
  assert.doesNotMatch(source, /method:\s*['"]POST|localStorage|sessionStorage|sendBeacon|XMLHttpRequest|console\./);
});

test('single-root mode starts only the first-level read', async () => {
  const env = setup(success, true);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(env.calls(), 1);
  assert.equal(env.children.hidden, false);
});

test('pagination appends only new siblings and keeps the next cursor link', async () => {
  const env = setup(success);
  const appended = [];
  const next = {focus() {next.focused = true;}};
  const content = {querySelector: selector => selector === '.loyalty-forest' ? {children: ['child-21']} : next};
  env.page.createElement = () => ({content});
  const more = {
    href: '/admin/loyalty/tree/?parent=1&after=20',
    getAttribute: () => null,
    setAttribute() {},
    removeAttribute() {},
    parentElement: {
      querySelector: () => ({append: (...children) => appended.push(...children)}),
      querySelectorAll: () => [],
    },
    replaceWith: value => {more.replacement = value;},
    closest: selector => selector === '[data-tree-more]' ? more : null,
  };
  await env.click(more);
  assert.deepEqual(appended, ['child-21']);
  assert.equal(more.replacement, next);
  assert.equal(next.focused, true);
  assert.match(env.status.textContent, /تم عرض المزيد/);
});
