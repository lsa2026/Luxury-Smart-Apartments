const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const code = fs.readFileSync('static/js/ads-purchase-data.js', 'utf8');

function run({consent = true, hash = 'a'.repeat(64), ads = true, present = true} = {}) {
    const handlers = {}, windowHandlers = {};
    const element = {dataset: {adsPurchaseEmailHash: hash}, removeAttribute() {this.removed = true;}};
    const window = {dataLayer: [], LSAConsent: {isUserProvidedDataAllowed: () => consent},
        addEventListener(n, f) {windowHandlers[n] = f;}};
    const document = {body: {dataset: {googleAdsEnabled: String(ads)}},
        querySelector: () => present ? element : null, addEventListener(n, f) {handlers[n] = f;}};
    vm.runInNewContext(code, {window, document});
    return {window, element, withdraw() {consent = false; handlers['lsa:consent-updated']();},
        grant() {consent = true;}, exit() {windowHandlers.pagehide();}};
}

test('Ads-only accessor exposes prehashed identity with consent but never enqueues events', () => {
    const r = run();
    assert.equal(r.window.LSAAdsPurchaseData.read().sha256_email_address, 'a'.repeat(64));
    assert.equal(r.element.removed, true);
    assert.equal(r.window.dataLayer.length, 0);
    assert.equal(r.window.LSAUserProvidedData, undefined);
});

test('denied, disabled and malformed identity fail closed without affecting conversion tags', () => {
    for (const options of [{consent: false}, {ads: false}, {hash: 'raw@example.invalid'}, {hash: ''}]) {
        const r = run(options);
        assert.equal(Object.keys(r.window.LSAAdsPurchaseData.read()).length, 0);
        assert.equal(r.window.dataLayer.length, 0);
    }
    assert.equal(run({present: false}).window.LSAAdsPurchaseData, undefined);
});

test('withdrawal and page exit erase identity; later consent never resurrects it', () => {
    for (const action of ['withdraw', 'exit']) {
        const r = run(); r[action](); r.grant();
        assert.equal(Object.keys(r.window.LSAAdsPurchaseData.read()).length, 0);
    }
    assert.doesNotMatch(code, /localStorage|sessionStorage|dataLayer\.push|gtag\(/);
});
