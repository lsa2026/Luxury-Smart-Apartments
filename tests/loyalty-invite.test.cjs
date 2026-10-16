const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync('static/js/loyalty-invite.js', 'utf8');
const api = import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

class Element extends EventTarget {
    constructor() { super(); this.attrs = {}; this.dataset = {}; this.textContent = ''; }
    setAttribute(k, v) { this.attrs[k] = v; }
    removeAttribute(k) { delete this.attrs[k]; }
    focus() { this.focused = true; }
}
function setup() {
    const panel = new Element(), input = new Element(), button = new Element(), status = new Element();
    input.value = 'https://example.invalid/i/abcdefghijklmnop/';
    input.select = () => { input.selected = true; };
    input.setSelectionRange = (start, end) => { input.selection = [start, end]; };
    panel.dataset = {copySuccess: 'تم نسخ الرابط', copyFailed: 'تعذر النسخ التلقائي'};
    panel.querySelector = selector => ({'[data-invite-link]': input, '[data-invite-copy-button]': button, '[data-invite-copy-status]': status})[selector];
    return {panel, input, button, status};
}

test('copies the complete link only after the guest action and announces success', async () => {
    const {bindInviteCopy} = await api, env = setup(); let calls = 0;
    const controller = bindInviteCopy(env.panel, {clipboard: {writeText: async text => {
        assert.equal(text, env.input.value); calls++;
    }}});
    assert.equal(calls, 0);
    await controller.copy();
    assert.equal(calls, 1); assert.equal(env.status.textContent, env.panel.dataset.copySuccess);
    assert.equal(env.button.disabled, false); assert.equal(env.button.focused, true);
    assert.doesNotMatch(source, /fetch\(|XMLHttpRequest|localStorage|dataLayer|console\.|requestSubmit|\.submit\(/);
});

test('selection fallback supports browsers without clipboard API', async () => {
    const {bindInviteCopy} = await api, env = setup();
    const controller = bindInviteCopy(env.panel, {clipboard: null, page: {execCommand: command => {
        assert.equal(command, 'copy'); assert.equal(env.input.selected, true); return true;
    }}});
    await controller.copy();
    assert.deepEqual(env.input.selection, [0, env.input.value.length]);
    assert.equal(env.status.textContent, env.panel.dataset.copySuccess);
});

test('clipboard denial falls back without claiming a failed copy succeeded', async () => {
    const {bindInviteCopy} = await api, env = setup();
    const controller = bindInviteCopy(env.panel, {clipboard: {writeText: async () => {throw new Error('denied');}}, page: {execCommand: () => false}});
    await controller.copy();
    assert.equal(env.status.textContent, env.panel.dataset.copyFailed);
    assert.equal(env.input.selected, true); assert.equal(env.button.focused, undefined);
    assert.equal(env.status.attrs['data-copy-error'], ''); assert.equal(env.button.disabled, false);
});

test('a denied clipboard may use a successful legacy copy and exceptions stay recoverable', async () => {
    const {bindInviteCopy} = await api, env = setup();
    const controller = bindInviteCopy(env.panel, {clipboard: {writeText: async () => {throw new Error('denied');}}, page: {execCommand: () => true}});
    await controller.copy();
    assert.equal(env.status.textContent, env.panel.dataset.copySuccess);
    const failed = setup();
    await bindInviteCopy(failed.panel, {clipboard: null, page: {execCommand: () => {throw new Error('unsupported');}}}).copy();
    assert.equal(failed.status.textContent, failed.panel.dataset.copyFailed);
    assert.equal(failed.button.disabled, false);
});

test('rapid duplicate clicks do not write twice and binding is idempotent', async () => {
    const {bindInviteCopy} = await api, env = setup(); let release, calls = 0;
    const controller = bindInviteCopy(env.panel, {clipboard: {writeText: () => {calls++; return new Promise(resolve => {release = resolve;});}}});
    const first = controller.copy(); await controller.copy();
    assert.equal(calls, 1); assert.equal(env.button.disabled, true);
    assert.equal(bindInviteCopy(env.panel), undefined);
    release(); await first; assert.equal(env.button.disabled, false);
});

test('missing or empty controls do not copy or throw', async () => {
    const {bindInviteCopy} = await api; assert.equal(bindInviteCopy(null), undefined);
    assert.equal(bindInviteCopy({dataset: {}, querySelector: () => null}), undefined);
    const env = setup(); env.input.value = ''; let calls = 0;
    await bindInviteCopy(env.panel, {clipboard: {writeText: async () => {calls++;}}}).copy();
    assert.equal(calls, 0);
});
