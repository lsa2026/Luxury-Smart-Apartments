// Exercise the exact production guard, including native successful-control rules.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/js/site.js', 'utf8');
const start = source.indexOf('document.querySelectorAll("[data-submit-once]")');
const end = source.indexOf('\ndocument.querySelectorAll("[data-country-select]")', start);
assert.ok(start >= 0 && end > start);
const guard = source.slice(start, end);

function setup(actions = ['apply']) {
    const buttons = actions.map(value => ({
        type: 'submit', name: 'action', value, disabled: false,
        attrs: {}, setAttribute(key, v) { this.attrs[key] = v; },
    }));
    const fields = [{type: 'number', name: 'points', value: '25'},
        {type: 'hidden', name: 'request_reference', value: 'TEST-REQUEST-ONLY-01'},
        {type: 'hidden', name: 'csrfmiddlewaretoken', value: 'test-only-token'}];
    const listeners = {};
    const loading = {hidden: true};
    const form = {
        dataset: {}, valid: true,
        checkValidity() { return this.valid; },
        querySelector(selector) { return selector.includes('loading') ? loading : buttons[0]; },
        addEventListener(name, fn) { listeners[name] = fn; },
        appendChild(field) { fields.push(field); },
    };
    const document = {
        querySelectorAll() { return [form]; },
        createElement(tag) { assert.equal(tag, 'input'); return {}; },
    };
    vm.runInNewContext(guard, {document});
    function submit(button = buttons[0], prevented = false) {
        const event = { submitter: button, defaultPrevented: prevented,
            preventDefault() { this.defaultPrevented = true; } };
        listeners.submit(event);
        // Native submission excludes disabled buttons and includes only the
        // actual submitter, never other buttons in the same form.
        const successful = [...fields, ...buttons.filter(b => b === button)]
            .filter(field => field.name && !field.disabled);
        const payload = new URLSearchParams(successful.map(field => [field.name, field.value]));
        return {event, payload};
    }
    return {form, fields, buttons, loading, submit};
}

test('25 points retains exactly one apply command after click guard disables submitter', () => {
    const env = setup();
    const {event, payload} = env.submit();
    assert.equal(event.defaultPrevented, false);
    assert.equal(payload.get('points'), '25');
    assert.deepEqual(payload.getAll('action'), ['apply']);
    assert.equal(payload.get('request_reference'), 'TEST-REQUEST-ONLY-01');
    assert.equal(payload.get('csrfmiddlewaretoken'), 'test-only-token');
    assert.equal(env.buttons[0].disabled, true);
    assert.equal(env.buttons[0].attrs['aria-busy'], 'true');
    assert.equal(env.loading.hidden, false);
});

for (const action of ['confirm', 'remove']) {
    test(`multi-action loyalty form preserves clicked ${action}, not first button`, () => {
        const env = setup(['confirm', 'remove']);
        const clicked = env.buttons.find(button => button.value === action);
        const {payload} = env.submit(clicked);
        assert.deepEqual(payload.getAll('action'), [action]);
        assert.equal(clicked.disabled, true);
        assert.equal(env.buttons.find(button => button !== clicked).disabled, false);
    });
}

test('repeat submission is actually prevented and adds no duplicate hidden command', () => {
    const env = setup();
    env.submit();
    const second = env.submit();
    assert.equal(second.event.defaultPrevented, true);
    assert.deepEqual(second.payload.getAll('action'), ['apply']);
});

test('invalid or previously prevented submissions do not disable or append controls', () => {
    for (const prevented of [true, false]) {
        const env = setup();
        env.form.valid = prevented;
        env.submit(env.buttons[0], prevented);
        assert.equal(env.buttons[0].disabled, false);
        assert.equal(env.fields.length, 3);
        assert.equal(env.form.dataset.submitting, undefined);
    }
});

test('unnamed checkout and sign-in buttons do not gain an action or change existing fields', () => {
    const env = setup();
    env.buttons[0].name = '';
    const {payload} = env.submit();
    assert.equal(payload.has('action'), false);
    assert.equal(payload.get('points'), '25');
    assert.equal(env.fields.length, 3);
});

test('programmatic submit without a submitter does not invent a named command', () => {
    const env = setup();
    const {payload} = env.submit(null);
    assert.equal(payload.has('action'), false);
    assert.equal(env.fields.length, 3);
});
