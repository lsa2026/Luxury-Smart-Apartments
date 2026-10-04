const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const {webcrypto, createHash} = require('node:crypto');
const code = fs.readFileSync('static/js/user-provided-data.js', 'utf8');

function run({consented = false, value = 'Synthetic.Guest@example.invalid', crypto = webcrypto,
    formPresent = true, enabled = true} = {}) {
    const inputHandlers = {}, formHandlers = {}, documentHandlers = {}, windowHandlers = {};
    const input = {value, checkValidity: () => input.value.includes('@'),
        addEventListener: (n, fn) => {inputHandlers[n] = fn;}};
    const form = {querySelector: selector => selector === 'input[name="guest_email"]' ? input : null,
        addEventListener: (n, fn) => {formHandlers[n] = fn;}};
    const window = {crypto, dataLayer: [],
        LSAConsent: {isUserProvidedDataAllowed: () => consented},
        addEventListener: (n, fn) => {windowHandlers[n] = fn;}};
    const document = {body: {dataset: {googleIntegrationsEnabled: String(enabled),
        gtmEnabled: 'true', ga4Enabled: 'true'}},
        querySelector: s => s === '[data-upd-guest-form]' && formPresent ? form : null,
        addEventListener: (n, fn) => {documentHandlers[n] = fn;}};
    vm.runInNewContext(code, {window, document, TextEncoder, Uint8Array});
    return {window, input, inputHandlers, formHandlers, windowHandlers,
        consent(value) {consented = value; documentHandlers['lsa:consent-updated']?.();}};
}
const settled = () => new Promise(resolve => setTimeout(resolve, 20));

test('no identity or event is exposed before the independent opt-in', async () => {
    const r = run(); await settled();
    await r.inputHandlers.blur();
    assert.equal(r.window.dataLayer.length, 0);
    assert.equal(r.window.LSAUserProvidedData.read(), undefined);
});
test('consented email uses normalized hexadecimal SHA-256 through the dedicated accessor only', async () => {
    const r = run({consented: true, value: '  Synthetic.Guest@EXAMPLE.invalid  '});
    await settled();
    const data = r.window.LSAUserProvidedData.read();
    assert.equal(data.sha256_email_address,
        createHash('sha256').update('synthetic.guest@example.invalid').digest('hex'));
    assert.equal(JSON.stringify(r.window.dataLayer), '[{"event":"lsa_user_data_provided"}]');
    assert.deepEqual(Object.keys(data), ['sha256_email_address']);
});
test('blur/change/submit/consent updates cannot send duplicate identity events on the page', async () => {
    const r = run({consented: true}); await settled();
    await r.inputHandlers.blur(); await r.inputHandlers.change();
    await r.formHandlers.submit(); r.consent(true); await settled();
    assert.equal(r.window.dataLayer.length, 1);
    assert.equal(r.window.dataLayer.filter(e => e.event === 'purchase').length, 0);
});
test('revocation immediately removes identity without altering a purchase already queued', async () => {
    const r = run({consented: true}); await settled();
    const purchase = {event: 'purchase', transaction_id: 'synthetic-tx', value: 1253,
        currency: 'SAR', property_id: 'synthetic-apartment', page_language: 'en'};
    r.window.dataLayer.push(purchase); r.consent(false);
    assert.equal(r.window.LSAUserProvidedData.read(), undefined);
    assert.equal(r.window.dataLayer.at(-1), purchase);
    await r.formHandlers.submit(); assert.equal(r.window.dataLayer.length, 2);
});
test('an edited or silently autofilled email cannot reuse the previous identity', async () => {
    const r = run({consented: true}); await settled();
    r.input.value = 'second@example.invalid';
    assert.equal(r.window.LSAUserProvidedData.read(), undefined);
    r.inputHandlers.input(); assert.equal(r.window.LSAUserProvidedData.read(), undefined);
    await r.inputHandlers.change(); assert.equal(r.window.dataLayer.length, 2);
});
test('revocation during hashing cancels pending transmission', async () => {
    let resolve;
    const crypto = {subtle: {digest: () => new Promise(r => {resolve = r;})}};
    const r = run({consented: true, crypto}); r.consent(false);
    resolve(new Uint8Array(32).buffer); await settled();
    assert.equal(r.window.dataLayer.length, 0);
    assert.equal(r.window.LSAUserProvidedData.read(), undefined);
});
test('missing/failed hashing does not block form submission or emit UPD', async () => {
    const unavailable = run({consented: true, crypto: {}});
    assert.equal(unavailable.formHandlers.submit, undefined);
    const failed = run({consented: true, crypto: {subtle: {digest: async () => {throw Error('offline');}}}});
    await settled(); await failed.formHandlers.submit({preventDefault: () => assert.fail('blocked')});
    assert.equal(failed.window.dataLayer.length, 0);
});
test('invalid/empty email, non-checkout pages and disabled Google integrations are excluded', async () => {
    for (const value of ['', 'not-an-email', 'x@y', 'x @example.invalid']) {
        const r = run({consented: true, value}); await settled();
        assert.equal(r.window.dataLayer.length, 0);
    }
    for (const options of [{formPresent: false}, {enabled: false}]) {
        const r = run({consented: true, ...options}); await settled();
        assert.equal(r.window.LSAUserProvidedData, undefined);
    }
});
test('page exit clears in-memory identity and no browser storage or gtag global state is used', async () => {
    const r = run({consented: true}); await settled(); r.windowHandlers.pagehide();
    assert.equal(r.window.LSAUserProvidedData.read(), undefined);
    assert.doesNotMatch(code, /localStorage|sessionStorage|window\.gtag\(/);
});
