const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const code = fs.readFileSync('static/js/analytics.js', 'utf8');

function run(value, consent = true, previouslyPushed = false) {
    const handlers = {};
    const storage = new Map(previouslyPushed ? [['lsa:purchase:test-booking', 'pushed']] : []);
    const purchase = {dataset: {analyticsValue: value, analyticsTransactionId: 'test-booking',
        analyticsCurrency: 'SAR', analyticsItemId: 'test-apartment', analyticsReceiptToken: 'test',
        analyticsReceiptUrl: '/ack/'}};
    let acknowledgements = 0;
    const context = {console, URLSearchParams, fetch: () => {acknowledgements++; return Promise.resolve();},
        document: {body: {dataset: {googleIntegrationsEnabled: 'true'}},
            querySelectorAll: () => [], querySelector: s => s === '[data-analytics-purchase-event]' ? purchase : null,
            addEventListener: (name, callback) => {handlers[name] = callback;}},
        window: {dataLayer: [], LSAConsent: {parseConsent: () => ({analytics: consent, marketing: consent})},
            sessionStorage: {getItem: k => storage.get(k), setItem: (k,v) => storage.set(k,v)}}};
    vm.runInNewContext(code, context);
    return {context, handlers, get acknowledgements() {return acknowledgements;},
        consent: () => {consent = true; handlers['lsa:consent-updated']({detail:{analytics:true,marketing:true,version:1}});}};
}
test('valid purchase contains real amount, currency and booking ID once', () => {
    const result = run('1090.0');
    const event = result.context.window.dataLayer.find(e => e.event === 'purchase');
    assert.equal(event.value,1090); assert.equal(event.currency,'SAR');
    assert.equal(event.transaction_id,'test-booking'); assert.equal(result.acknowledgements,1);
    assert.equal(event.ecommerce.value,1090);
    assert.equal(event.ecommerce.transaction_id,'test-booking');
    result.consent();
    assert.equal(result.context.window.dataLayer.filter(e=>e.event==='purchase').length,1);
});
test('invalid, zero and missing amounts never emit or acknowledge purchase', () => {
    for (const value of ['1090,0','NaN','Infinity','',undefined,'0','-1']) {
        const result = run(value);
        assert.equal(result.context.window.dataLayer.length,0);
        assert.equal(result.acknowledgements,0);
    }
});
test('consent granted after page load emits purchase; denied does not', () => {
    const result = run('1090',false);
    assert.equal(result.acknowledgements,0);
    result.consent(); assert.equal(result.acknowledgements,1);
});
test('already pushed transaction is not sent twice', () => {
    assert.equal(run('1090',true,true).context.window.dataLayer.length,0);
});
