const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const code = fs.readFileSync('static/js/analytics.js', 'utf8');

function run(consent = true) {
    const window = {dataLayer: [], addEventListener() {},
        LSAConsent: {parseConsent: () => ({analytics: consent})}};
    const document = {body: {dataset: {googleIntegrationsEnabled: 'true'}},
        querySelectorAll: () => [], querySelector: () => null, addEventListener() {}};
    vm.runInNewContext(code, {window, document});
    return window;
}

test('apartment journey events expose scoped ecommerce without emitting a purchase', () => {
    const w = run();
    for (const event of ['view_item_list', 'select_item', 'view_item', 'begin_checkout',
        'quote_created', 'booking_intent_created']) {
        w.LSAAnalytics.pushEvent(event, {items: [{item_id: 'apartment-a11', quantity: 1}],
            value: 1253, currency: 'SAR', item_list_name: 'apartments',
            transaction_id: 'must-not-survive'});
        const payload = w.dataLayer.at(-1);
        assert.equal(payload.event, event);
        assert.equal(payload.ecommerce.items[0].item_id, 'apartment-a11');
        assert.equal(payload.ecommerce.transaction_id, undefined);
        assert.equal(w.dataLayer.at(-2).ecommerce, null);
        if (['view_item', 'begin_checkout', 'quote_created', 'booking_intent_created'].includes(event)) {
            assert.equal(payload.ecommerce.value, 1253);
            assert.equal(payload.ecommerce.currency, 'SAR');
        }
    }
    assert.equal(w.dataLayer.filter(e => e.event === 'purchase').length, 0);
});

test('contact event clears stale ecommerce after checkout and cannot carry transaction identity', () => {
    const w = run();
    w.LSAAnalytics.pushEvent('begin_checkout', {value: 1120, currency: 'SAR',
        items: [{item_id: 'darat-safa'}]});
    w.LSAAnalytics.pushEvent('phone_click', {lead_source: 'footer'});
    const event = w.dataLayer.at(-1);
    assert.equal(event.ecommerce, null);
    assert.equal(event.transaction_id, undefined);
    assert.equal(event.items, undefined);
});

test('denied analytics cannot send any journey ecommerce', () => {
    const w = run(false);
    assert.equal(w.LSAAnalytics.pushEvent('view_item', {items: [{item_id: 'a11'}]}), false);
    assert.equal(w.dataLayer.length, 0);
});
