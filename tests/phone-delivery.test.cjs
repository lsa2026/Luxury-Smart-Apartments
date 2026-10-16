const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/js/phone-delivery.js', 'utf8');

function setup(fetch, initial = 'waiting') {
  const status = {
    textContent: 'waiting',
    dataset: {state: initial, url: '/account/mobile/confirm/?delivery=1',
      waiting: 'waiting', accepted: 'accepted', failed: 'failed', expired: 'expired'},
  };
  const form = {hidden: false};
  const document = {hidden: false,
    querySelector: (selector) => selector === '[data-phone-delivery]' ? status : form};
  const window = new EventTarget();
  const timers = [];
  vm.runInNewContext(source, {document, window, fetch,
    setTimeout: (callback) => {timers.push(callback); return callback;},
    clearTimeout: (callback) => {const i = timers.indexOf(callback); if (i >= 0) timers.splice(i, 1);},
  });
  return {status, form, document, window, timers,
    tick: async () => {if (timers.length) await timers.shift()();}};
}

for (const state of ['accepted', 'failed', 'expired']) {
  test(`shows ${state} without sending a code or changing the account`, async () => {
    let calls = 0;
    const env = setup(async (url, options) => {
      calls++;
      assert.equal(url, '/account/mobile/confirm/?delivery=1');
      assert.equal(options.credentials, 'same-origin');
      assert.equal(options.cache, 'no-store');
      assert.equal(options.method, undefined); // GET only, never a send POST.
      return {ok: true, json: async () => ({state})};
    });
    await env.tick();
    assert.equal(env.status.textContent, state);
    assert.equal(env.form.hidden, state !== 'accepted');
    assert.equal(env.timers.length, 0);
    assert.equal(calls, 1);
  });
}

test('temporary read failures do not claim success, hide the form, or resend', async () => {
  for (const fetch of [async () => {throw Error('offline');},
    async () => ({ok: false}),
    async () => ({ok: true, json: async () => ({state: 'unexpected'})})]) {
    const env = setup(fetch);
    await env.tick();
    assert.equal(env.status.dataset.state, 'waiting');
    assert.equal(env.form.hidden, false);
    assert.equal(env.timers.length, 1);
  }
});

test('polling is bounded and does not run on a finished page', async () => {
  let calls = 0;
  const fetch = async () => {calls++; return {ok: true, json: async () => ({state: 'waiting'})};};
  const env = setup(fetch);
  for (let i = 0; i < 40; i++) await env.tick();
  assert.equal(calls, 30);
  assert.equal(env.timers.length, 0);
  for (const state of ['accepted', 'failed', 'expired']) assert.equal(setup(fetch, state).timers.length, 0);
});

test('background tabs pause reads and navigation cancels polling', async () => {
  let calls = 0;
  const env = setup(async () => {calls++; return {ok: true, json: async () => ({state: 'waiting'})};});
  env.document.hidden = true;
  await env.tick();
  assert.equal(calls, 0);
  env.window.dispatchEvent(new Event('pagehide'));
  assert.equal(env.timers.length, 0);
});

test('a slow read cannot overlap another read or restart polling after navigation', async () => {
  let release;
  const env = setup(() => new Promise(resolve => {release = resolve;}));
  const pending = env.tick();
  assert.equal(env.timers.length, 0);
  env.window.dispatchEvent(new Event('pagehide'));
  release({ok: true, json: async () => ({state: 'waiting'})});
  await pending;
  assert.equal(env.timers.length, 0);
});
