const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const source = readFileSync('static/js/clarity-public.js', 'utf8');
function run(path = '/ar/', analytics = true, referrer = '', clarityMeasurement = true) {
    const inserted = [], listeners = {}, attributes = {};
    let choice = {analytics, clarityMeasurement};
    const window = {location: {href: 'https://luxurysmartapartments.com' + path},
        LSAConsent: {parseConsent: () => choice}, addEventListener: (n, f) => {listeners[n] = f;}};
    const document = {referrer, documentElement: {setAttribute: (k, v) => {attributes[k] = v;}},
        head: {appendChild: s => inserted.push(s)}, querySelectorAll: () => [],
        createElement: () => ({dataset: {}}), addEventListener: (n, f) => {listeners[n] = f;}};
    const context = {window, document, URL};
    vm.runInNewContext(source, context);
    return {window, inserted, listeners, attributes,
        rerun: () => vm.runInNewContext(source, context),
        consent(analytics, clarityMeasurement = true) {
            choice = {analytics, clarityMeasurement}; listeners['lsa:consent-updated']();
        }};
}
test('one SDK after fresh explicit consent; strict masking and no ad permission', () => {
    const t = run(); t.rerun(); t.consent(true);
    assert.equal(t.inserted.length, 1);
    assert.equal(t.inserted[0].src, 'https://www.clarity.ms/tag/yz1e54we6c');
    assert.equal(t.inserted[0].referrerPolicy, 'no-referrer');
    assert.equal(t.attributes['data-clarity-mask'], 'true');
    assert.equal(t.window.clarity.q[0][1].ad_Storage, 'denied');
    assert.equal(t.window.clarity.q[0][1].analytics_Storage, 'granted');
});
test('legacy analytics consent alone is insufficient', () => {
    const t = run('/ar/', true, '', false);
    assert.equal(t.inserted.length, 0);
    t.consent(true, true); assert.equal(t.inserted.length, 1);
});
test('denial, missing consent, readiness and re-grant never duplicate SDK', () => {
    const t = run('/en/', false);
    assert.equal(t.inserted.length, 0);
    t.consent(true); t.listeners.DOMContentLoaded(); t.listeners.load();
    assert.equal(t.inserted.length, 1);
    assert.equal(run('/ar/', null).inserted.length, 0);
});
test('withdrawal denies consent then stops; new grant requires a fresh page', () => {
    const t = run(); t.consent(false);
    assert.equal(t.window.clarity.q.at(-2)[1].analytics_Storage, 'denied');
    assert.equal(t.window.clarity.q.at(-1)[0], 'stop');
    assert.equal(t.attributes['data-lsa-clarity-state'], 'stop-requested');
    t.consent(true); assert.equal(t.inserted.length, 1);
});
for (const path of ['/payments/hyperpay/result/example/', '/payments/pay/example/',
    '/reservations/example/', '/accounts/login/', '/admin/', '/my-bookings/', '/ar/contact/',
    '/ar/?return_token=synthetic', '/ar/?utm_source=synthetic', '/ar/#private',
    '/ar/properties/flat/?email=synthetic', '/ar/properties/flat/gallery/', '/i/example/', '/ar/unknown/']) {
    test('excluded URL ' + path, () => assert.equal(run(path).inserted.length, 0));
}
for (const referrer of ['https://luxurysmartapartments.com/payments/hyperpay/result/example/',
    'https://luxurysmartapartments.com/ar/?return_token=synthetic', 'https://eu-prod.oppwa.com/',
    'https://example.com/private/path', 'https://example.com/?email=synthetic']) {
    test('excluded referral ' + referrer, () => assert.equal(run('/ar/', true, referrer).inserted.length, 0));
}
test('public referral and all three language roots accepted', () => {
    for (const path of ['/ar/', '/en/', '/fr/', '/ar/about/', '/ar/properties/flat/']) {
        assert.equal(run(path, true, 'https://www.google.com/').inserted.length, 1);
    }
});
