const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const data = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const addressModule = data(fs.readFileSync('static/js/checkout-address.js', 'utf8'));
const source = fs.readFileSync('static/js/admin-guest-prefill.js', 'utf8');
const api = import(data(source.replace('"./checkout-address.js"', JSON.stringify(addressModule))));
class Element extends EventTarget {
    constructor(value = '') { super(); this.value = value; this.children = []; this.hidden = false; }
    append(child) { this.children.push(child); }
    replaceChildren() { this.children = []; }
}
const values = { guest_first_name: 'Synthetic', guest_last_name: 'Guest', guest_name: 'Synthetic Guest',
    guest_email: 'saved@example.invalid', guest_phone: '+16502530000', guest_phone_country: 'US', guest_language: 'fr',
    billing_street1: 'Saved Street 10', billing_city: 'Riyadh', billing_state: 'Riyadh', billing_country: 'SA', billing_postcode: '12345',
    final_total_price: '1', check_in: '2030-01-01', terms_accepted: 'on', source_ticket: 'untrusted' };
const result = (body, ok = true) => ({ok, json: async () => body});
const found = {status: 'found', candidates: [{source_label: 'Previous booking', fields: values,
    address_kind: 'billing', address_available: true, address_complete: true}]};
function setup() {
    const form = new Element();
    form.elements = Object.fromEntries(Object.keys(values).map(name => [name, new Element('')]));
    form.elements.guest_email.value = 'saved@example.invalid';
    form.elements.guest_phone_country.value = 'US';
    form.elements.billing_country.value = 'SA';
    for (const [name, options] of Object.entries({billing_country:['SA','FR'], guest_phone_country:['US','SA'], guest_language:['ar','en','fr']})) {
        form.elements[name].tagName = 'SELECT'; form.elements[name].options = options.map(value => ({value}));
    }
    form.elements.csrfmiddlewaretoken = {value: 'synthetic-csrf'};
    form.submit = () => assert.fail('must not submit');
    const names = ['load-saved-guest','saved-guest-choices','saved-guest-choice','saved-guest-preview',
        'saved-guest-status','apply-saved-guest','apply-saved-address'];
    const nodes = Object.fromEntries(names.map(name => [name, new Element()]));
    nodes['saved-guest-choices'].hidden = true;
    const panel = {dataset: {checkUrl: '/private/guest-check/'}, closest: () => form,
        querySelector: selector => nodes[selector.slice(6, -1)]};
    return {form, panel, nodes};
}
const tick = () => new Promise(resolve => setImmediate(resolve));
async function load(env) { env.nodes['load-saved-guest'].dispatchEvent(new Event('click')); await tick(); }
function click(env, name) { env.nodes[name].dispatchEvent(new Event('click')); }

test('private POST, explicit preview/apply, fixed allowlist, no financial or terms changes', async () => {
    const {initSavedGuest} = await api;
    global.document = {createElement: () => new Element()};
    const env = setup();
    initSavedGuest(env.panel, {fetcher: async (url, options) => {
        assert.equal(url, '/private/guest-check/');
        assert.equal(options.method, 'POST'); assert.equal(options.cache, 'no-store');
        assert.equal(options.credentials, 'same-origin');
        assert.equal(options.headers['X-CSRFToken'], 'synthetic-csrf');
        assert.equal(options.body.get('action'), 'lookup_saved_guest');
        return result(found);
    }});
    await load(env);
    assert.equal(env.form.elements.billing_street1.value, '');
    assert.equal(env.nodes['saved-guest-choices'].hidden, false);
    click(env, 'apply-saved-guest');
    assert.equal(env.form.elements.billing_street1.value, values.billing_street1);
    assert.equal(env.form.elements.guest_first_name.value, 'Synthetic');
    for (const name of ['final_total_price','check_in','terms_accepted','source_ticket']) assert.equal(env.form.elements[name].value, '');
    assert.equal(env.nodes['saved-guest-choices'].hidden, true);
});

test('address-only retains contact fields; edits made during lookup survive', async () => {
    const {initSavedGuest} = await api;
    const env = setup(); let resolve;
    initSavedGuest(env.panel, {fetcher: () => new Promise(r => {resolve = r;})});
    click(env, 'load-saved-guest');
    env.form.elements.billing_city.value = 'Manually corrected';
    env.form.elements.guest_first_name.value = 'Typed name';
    resolve(result(found)); await tick(); click(env, 'apply-saved-address');
    assert.equal(env.form.elements.guest_first_name.value, 'Typed name');
    assert.equal(env.form.elements.billing_city.value, 'Manually corrected');
    assert.equal(env.form.elements.billing_street1.value, 'Saved Street 10');
});

test('changed search aborts lookup and discards stale responses', async () => {
    const {initSavedGuest} = await api;
    const env = setup(); let resolve, signal;
    initSavedGuest(env.panel, {fetcher: (_, opts) => {signal = opts.signal; return new Promise(r => {resolve = r;});}});
    click(env, 'load-saved-guest');
    env.form.elements.guest_email.value = 'another@example.invalid';
    env.form.elements.guest_email.dispatchEvent(new Event('input'));
    assert.equal(signal.aborted, true);
    resolve(result(found)); await tick();
    assert.equal(env.nodes['saved-guest-choices'].hidden, true);
    click(env, 'apply-saved-guest');
    assert.equal(env.form.elements.billing_street1.value, '');
});

test('country edits invalidate address application without mixing countries', async () => {
    const {initSavedGuest} = await api;
    const env = setup();
    initSavedGuest(env.panel, {fetcher: async () => result(found)});
    await load(env); env.form.elements.billing_country.value = 'FR';
    click(env, 'apply-saved-address');
    assert.equal(env.form.elements.billing_street1.value, '');
    assert.equal(env.form.elements.billing_country.value, 'FR');
});

test('missing address still offers contact recovery, labels residence and renders markup as text', async () => {
    const {initSavedGuest} = await api;
    const env = setup(); const fields = {...values, guest_name: '<img src=x onerror=alert(1)>'};
    initSavedGuest(env.panel, {fetcher: async () => result({status:'found', candidates:[
        {fields, source_label:'Profile', address_kind:'residence', address_available:false, address_complete:false}]})});
    await load(env);
    assert.equal(env.nodes['apply-saved-address'].disabled, true);
    assert.equal(env.nodes['saved-guest-preview'].children[0].textContent, fields.guest_name);
    assert.match(env.nodes['saved-guest-preview'].children.map(n => n.textContent).join(' '), /عنوان السكن/);
    assert.ok(!source.includes('innerHTML'));
});

test('errors and not-found do not replace any field; retry remains available', async () => {
    const {initSavedGuest} = await api;
    for (const response of [result({status:'not_found'}), result({errors:{phone:['Invalid']}}, false)]) {
        const env = setup(); initSavedGuest(env.panel, {fetcher: async () => response});
        await load(env);
        assert.equal(env.nodes['load-saved-guest'].disabled, false);
        assert.equal(env.form.elements.billing_street1.value, '');
        assert.equal(env.nodes['saved-guest-choices'].hidden, true);
    }
});
