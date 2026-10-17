const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const code = fs.readFileSync('static/js/measurement-privacy.js', 'utf8');

function run(href, referrer = '') {
    const window = {location: {href}};
    vm.runInNewContext(code, {window, document: {referrer}, URL});
    return window;
}

test('private return URL and previous checkout are redacted without changing navigation', () => {
    const href = 'https://example.com/payments/hyperpay/result/private-id/'
        + '?return_token=secret&id=gateway-id&resourcePath=private-path#secret';
    const window = run(href, 'https://example.com/payments/hyperpay/booking/private-reference/');
    const fields = window.LSAMeasurementPrivacy.fields();
    assert.equal(fields.page_location, 'https://example.com/payments/hyperpay/result/');
    assert.equal(fields.page_referrer, 'https://example.com/payments/hyperpay/booking/');
    assert.equal(window.location.href, href);
    assert.equal(window.dataLayer.length, 1);
    assert.equal(window.dataLayer[0][0], 'set');
    assert.equal(window.dataLayer.some(v => v.event === 'purchase'), false);
});

test('public campaign attribution remains; unknown queries, credentials and fragments do not', () => {
    const window = run('https://example.com/ar/properties/stay/?utm_source=google'
        + '&utm_campaign=arabic&gclid=advertising-click&return_token=secret&email=private#private');
    const location = window.LSAMeasurementPrivacy.fields().page_location;
    assert.match(location, /utm_source=google/);
    assert.match(location, /utm_campaign=arabic/);
    assert.match(location, /gclid=advertising-click/);
    assert.doesNotMatch(location, /secret|private|email|return_token|#/);
    assert.equal(window.LSAMeasurementPrivacy.safeUrl('javascript:alert(1)'), '');
    assert.equal(window.LSAMeasurementPrivacy.safeUrl(''), '');
});

test('all private booking and payment routes hide path identifiers and queries', () => {
    const window = run('https://example.com/ar/');
    for (const path of ['/payments/pay/private/', '/reservations/private/review/',
        '/accounts/private/', '/admin/private/', '/payments/hyperpay/modification/private/']) {
        const safe = window.LSAMeasurementPrivacy.safeUrl(`https://example.com${path}?token=secret`);
        assert.doesNotMatch(safe, /private|secret|token/);
    }
});

test('metadata refreshes on later events without consent or event side effects', () => {
    const window = run('https://example.com/ar/');
    window.location.href = 'https://example.com/payments/hyperpay/result/private/?id=secret';
    window.LSAMeasurementPrivacy.apply();
    assert.equal(window.dataLayer.at(-1)[1].page_location,
        'https://example.com/payments/hyperpay/result/');
    assert.equal(window.dataLayer.some(v => v[0] === 'consent' || v[0] === 'event'), false);
});
