const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const analytics = fs.readFileSync('static/js/analytics.js', 'utf8');
const journey = fs.readFileSync('static/js/payment-journey.js', 'utf8');

function run(consent = true) {
    const listeners = {}, rootListeners = {}, observers = {};
    let allowed = consent;
    const tabs = ['mada', 'card', 'apple-pay'].map(name => ({
        dataset: {checkoutMethod: name}, hidden: false, selected: name === 'mada',
        getAttribute() { return this.selected ? 'true' : 'false'; },
    }));
    const root = {
        dataset: {analyticsPropertyId: 'e12', analyticsPropertyName: 'E12',
            analyticsPageLanguage: 'ar', analyticsValue: '1100', analyticsCurrency: 'SAR',
            analyticsCheckoutKind: 'booking'},
        querySelector: () => tabs.find(t => t.selected),
        querySelectorAll: () => tabs,
        addEventListener(name, fn) { (rootListeners[name] ||= []).push(fn); },
    };
    const document = {
        body: {dataset: {googleIntegrationsEnabled: 'true'}},
        readyState: 'loading', documentElement: {lang: 'ar'},
        querySelector: selector => selector === '[data-payment-journey]' ? root : null,
        querySelectorAll: () => [],
        addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
    };
    const window = {dataLayer: [], addEventListener() {},
        LSAConsent: {parseConsent: () => ({analytics: allowed})}};
    const sandbox = {document, window, MutationObserver: class {
        constructor(fn) { this.fn = fn; }
        observe(tab) { observers[tab.dataset.checkoutMethod] = this.fn; }
    }};
    vm.runInNewContext(journey, sandbox);
    window.LSAPaymentJourney.ready(); // before the deferred analytics script
    vm.runInNewContext(analytics, sandbox);
    listeners.DOMContentLoaded.forEach(fn => fn());
    return {
        window, root, tabs, rootListeners, observers,
        consent() { allowed = true; listeners['lsa:consent-updated'].forEach(fn => fn({
            detail: {analytics: true, marketing: true, version: 1},
        })); },
        select(name, trusted = true) {
            const tab = tabs.find(t => t.dataset.checkoutMethod === name);
            rootListeners.click.forEach(fn => fn({
                isTrusted: trusted, target: {closest: selector =>
                    selector === '[data-checkout-method]' ? tab : null},
            }));
            tabs.forEach(t => { t.selected = t === tab; });
            observers[name]();
        },
        events(name) { return window.dataLayer.filter(e => e.event === name); },
    };
}

test('arrival and widget readiness are separate, consented, once-per-page events', () => {
    const r = run();
    assert.equal(r.events('secure_payment_view').length, 1);
    assert.equal(r.events('payment_widget_ready').length, 1);
    assert.equal(r.events('payment_method_selected').length, 0);
    r.window.LSAPaymentJourney.ready();
    assert.equal(r.events('payment_widget_ready').length, 1);
    const event = r.events('secure_payment_view')[0];
    assert.equal(event.ecommerce.items[0].item_id, 'e12');
    assert.equal(event.value, 1100);
    assert.equal(event.payment_brand, 'mada'); // default, NOT a deliberate choice
    assert.equal(r.events('purchase').length, 0);
});

test('no denied-consent history is replayed; only the current view can start after consent', () => {
    const r = run(false);
    r.select('apple-pay');
    r.window.LSAPaymentJourney.error({name: 'WidgetError', message: 'private'});
    assert.equal(r.window.dataLayer.length, 0);
    r.consent();
    assert.equal(r.events('secure_payment_view').length, 1);
    assert.equal(r.events('payment_method_selected').length, 0);
    assert.equal(r.events('payment_widget_error').length, 0);
});

test('method switches are distinct, programmatic wallet fallback is not a user choice', () => {
    const r = run();
    r.select('apple-pay');
    r.select('card');
    r.select('mada', false);
    assert.equal(r.events('payment_method_selected').length, 2);
    assert.equal(r.events('payment_method_selected')[0].payment_type, 'apple_pay');
    assert.equal(r.events('payment_method_selected')[1].payment_brand, 'unknown');
});

test('gateway submission is not a purchase and raw errors/tokens never enter analytics', () => {
    const r = run();
    r.select('apple-pay');
    r.window.LSAPaymentJourney.submitted();
    assert.equal(r.events('add_payment_info').length, 1);
    r.window.LSAPaymentJourney.error({name: 'WidgetError', event: 'onValidateMerchant-fail',
        message: 'secret-token', brand: 'arbitrary-private-string'});
    assert.equal(r.events('payment_widget_error')[0].error_category, 'merchant_validation');
    assert.equal(JSON.stringify(r.window.dataLayer).includes('secret-token'), false);
    assert.equal(JSON.stringify(r.window.dataLayer).includes('arbitrary-private'), false);
    assert.equal(r.events('purchase').length, 0);
});

test('payment metadata rejects arbitrary free text and cannot fabricate purchases', () => {
    const r = run();
    r.window.LSAAnalytics.pushEvent('payment_result', {payment_status: 'failed',
        payment_type: 'card', payment_brand: '4111111111111111', transaction_id: 'not-a-purchase',
        error_category: 'private-bank-error'});
    const event = r.events('payment_result')[0];
    assert.equal(event.payment_status, 'failed');
    assert.equal(event.payment_brand, null);
    assert.equal(event.transaction_id, undefined);
    assert.equal(event.error_category, null);
});

test('telemetry failure cannot throw into a gateway callback', () => {
    const r = run();
    r.window.LSAAnalytics.pushEvent = () => { throw new Error('blocked'); };
    assert.doesNotThrow(() => r.window.LSAPaymentJourney.submitted());
    assert.doesNotThrow(() => r.window.LSAPaymentJourney.error({}));
});
