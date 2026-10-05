const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const code = fs.readFileSync('static/js/meta-measurement.js', 'utf8');
const pixel = '904593259205579';
const granted = {marketing: true, metaMeasurement: true};

function run({choice = granted, url = 'https://example.invalid/en/properties/darat-safa/',
    referrer = '', dataset = {}, existingFbq} = {}) {
    let consent = choice;
    const scripts = [], calls = [], listeners = {};
    const document = {body: {dataset: {metaMeasurementPageAllowed: 'true',
        cookieConsentEnabled: 'true', gtmEnabled: 'true', googleIntegrationsEnabled: 'true',
        ...dataset}}, referrer, head: {append(s) {scripts.push(s);}},
        createElement() {return {dataset: {}};},
        addEventListener(n, fn) {listeners[n] = fn;}};
    const layer = [];
    const window = {location: {href: url}, dataLayer: layer,
        LSAConsent: {parseConsent: () => consent}, ...(existingFbq ? {fbq: existingFbq} : {})};
    vm.runInNewContext(code, {window, document, URL});
    return {window, scripts, calls, layer,
        consent(v) {consent = v; listeners['lsa:consent-updated']();},
        load() {window.fbq.callMethod = (...args) => calls.push(args); scripts[0].onload();},
        track: (name, payload) => window.LSAMeta.track(name, payload)};
}
const viewed = {property_id: 'darat-safa', property_name: 'Darat Safa', page_language: 'en'};
const events = r => r.calls.filter(c => c[0] === 'trackSingle');

test('missing, rejected, analytics-only, legacy marketing and Google UPD choices send nothing', () => {
    for (const choice of [null, {}, {marketing: false, metaMeasurement: true},
        {analytics: true}, {marketing: true}, {marketing: true, metaMeasurement: 'true'},
        {analytics: true, marketing: true, userProvidedData: true}]) {
        const r = run({choice}); r.track('gtm.init'); r.track('view_item', viewed);
        assert.equal(r.scripts.length, 0); assert.equal(r.window.fbq, undefined);
    }
});
test('marketing consent loads SDK only on GTM invocation; init and repeated choices give one PageView', () => {
    const r = run(); assert.equal(r.scripts.length, 0);
    r.track('gtm.init'); r.track('lsa_meta_consent');
    assert.equal(r.scripts.length, 1); assert.equal(events(r).length, 0);
    assert.equal(r.scripts[0].src, 'https://connect.facebook.net/en_US/fbevents.js');
    r.load(); r.track('gtm.init'); r.track('lsa_meta_consent');
    assert.deepEqual(events(r), [['trackSingle', pixel, 'PageView']]);
    assert.deepEqual(r.calls.find(c => c[0] === 'set'), ['set', 'autoConfig', false, pixel]);
    assert.deepEqual(r.calls.find(c => c[0] === 'init'), ['init', pixel]);
});
test('ViewContent only includes approved property fields and is deduplicated', () => {
    const r = run(); r.track('gtm.init'); r.track('view_item', {...viewed,
        email: 'private@example.invalid', phone: '123', sha256_email_address: 'private',
        transaction_id: 'private', value: 500, items: [{guest_name: 'Private'}]});
    r.load(); r.track('view_item', viewed);
    assert.equal(events(r).length, 2);
    assert.equal(JSON.stringify(events(r)[1]), JSON.stringify(['trackSingle', pixel, 'ViewContent',
        {content_ids: ['darat-safa'], content_name: 'Darat Safa', content_type: 'product', page_language: 'en'}]));
});
test('invalid property identifiers and names cannot load or send the SDK', () => {
    for (const payload of [{...viewed, property_id: 'guest@example.invalid'},
        {...viewed, property_name: 'private@example.invalid'}, {...viewed, property_name: 'https://private.invalid'},
        {...viewed, property_id: 123}, {...viewed, property_name: ''}]) {
        const r = run(); r.track('view_item', payload); assert.equal(r.scripts.length, 0);
    }
});
test('private routes, token query strings, unknown queries and private referrers never load Meta', () => {
    for (const options of [
        {dataset: {metaMeasurementPageAllowed: 'false'}}, {dataset: {gtmEnabled: 'false'}},
        {dataset: {cookieConsentEnabled: 'false'}}, {dataset: {googleIntegrationsEnabled: 'false'}},
        {url: 'https://example.invalid/payments/hyperpay/result/synthetic/?return_token=secret'},
        {url: 'https://example.invalid/en/?return_token=secret'},
        {url: 'https://example.invalid/en/?guest_email=private'},
        {url: 'https://example.invalid/en/?utm_content=private%40example.invalid'},
        {url: 'https://example.invalid/en/#private'},
        {referrer: 'https://example.invalid/reservations/manage/access/private/'},
        {referrer: 'https://example.invalid/payments/hyperpay/result/synthetic/?return_token=secret'},
    ]) {
        const r = run(options); r.track('gtm.init'); assert.equal(r.scripts.length, 0);
    }
    const r = run({url: 'https://example.invalid/en/?utm_source=facebook&fbclid=synthetic'});
    r.track('gtm.init'); assert.equal(r.scripts.length, 1);
});
test('withdrawal during loading prevents initialization; a later fresh grant can start normally', () => {
    const r = run(); r.track('gtm.init'); r.track('view_item', viewed);
    r.consent({marketing: false, metaMeasurement: false}); r.load();
    assert.equal(r.calls.length, 0);
    r.consent(granted); r.track('lsa_meta_consent');
    assert.deepEqual(events(r), [['trackSingle', pixel, 'PageView']]);
    assert.equal(r.scripts.length, 1);
});
test('withdrawal revokes SDK and suppresses new events without replay on approval', () => {
    const r = run(); r.track('gtm.init'); r.load();
    r.consent({marketing: false}); r.track('view_item', viewed);
    assert.deepEqual(r.calls.at(-1), ['consent', 'revoke']);
    r.consent(granted); r.track('lsa_meta_consent');
    assert.equal(events(r).length, 1); r.track('view_item', viewed);
    assert.equal(events(r).length, 2);
});
test('purchase, checkout, UPD and callbacks remain entirely in their existing Google path', () => {
    const r = run(); const originalPush = r.layer.push;
    let acknowledgements = 0;
    const purchase = {event: 'purchase', transaction_id: 'synthetic',
        eventCallback: () => acknowledgements++};
    r.layer.push(purchase);
    for (const event of ['purchase', 'begin_checkout', 'lsa_user_data_provided', 'contact_click']) {
        r.track(event, purchase);
    }
    assert.equal(r.scripts.length, 0); assert.equal(r.layer.push, originalPush);
    assert.equal(r.layer[0], purchase); assert.equal(acknowledgements, 0);
});
test('SDK failure and independently installed pixel do not send or modify Google events', () => {
    const r = run(); r.track('gtm.init'); r.scripts[0].onerror();
    r.track('lsa_meta_consent'); assert.equal(r.scripts.length, 1);
    const independent = () => {throw new Error('must not use another pixel');};
    const other = run({existingFbq: independent}); other.track('gtm.init');
    assert.equal(other.scripts.length, 0); assert.equal(other.window.fbq, independent);
});
