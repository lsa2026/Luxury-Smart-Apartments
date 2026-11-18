const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const analytics = fs.readFileSync('static/js/analytics.js', 'utf8');
const prepurchase = fs.readFileSync('static/js/prepurchase.js', 'utf8');
const sourceSite = fs.readFileSync('static/js/site.js', 'utf8');
let serial = 0;

function element(dataset = {}, fields = null) {
    const handlers = new Map();
    const e = {dataset, isConnected: true, handlers,
        elements: {namedItem: name => fields?.[name]},
        closest() { return this; },
        addEventListener(name, fn) { handlers.set(name, [...handlers.get(name) || [], fn]); },
        checkValidity: () => true, querySelector: () => null,
        fire(name, event = {}) { event.preventDefault ||= () => { event.defaultPrevented = true; };
            for (const fn of handlers.get(name) || []) fn(event); return event; },
    };
    return e;
}
function form(selected = '') {
    const fields = Object.fromEntries(Object.entries({property: selected, city: 'Riyadh',
        check_in: '2030-01-10', check_out: '2030-01-12', guests: '2',
        measurement_attempt: '', measurement_entry: '', measurement_parent: '', measurement_journey: '',
        guest_phone: 'PRIVATE-NOT-TO-READ', guest_email: 'PRIVATE-NOT-TO-READ',
    }).map(([key, value]) => [key, {value}]));
    return element({analyticsPropertyId: selected ? 'synthetic-apartment' : undefined,
        analyticsPropertyName: selected ? 'Synthetic Apartment' : undefined}, fields);
}
function price(value = '500', currency = 'SAR', type = 'quote_total') {
    return element({ppPrice: '', ppValue: value, ppCurrency: currency, ppPriceContext: type,
        ppPropertyId: 'synthetic-apartment', ppPropertyName: 'Synthetic Apartment'});
}
const id = 'a'.repeat(32);
function run({forms = [], prices = [], results = [], context = {}, consent = true,
    page = 'quote', store = new Map(), storageBlocked = false, observer = true, guard = false} = {}) {
    let now = 10000, choice = consent;
    const timers = new Map(), microtasks = [], documentHandlers = new Map(), windowHandlers = new Map();
    const document = {body: {dataset: {googleIntegrationsEnabled: 'true', prepurchasePageKind: page}},
        documentElement: {lang: 'en'}, visibilityState: 'visible',
        getElementById: () => ({textContent: JSON.stringify(context)}),
        querySelector: () => null,
        querySelectorAll(selector) { return selector === '[data-pp-price]' ? prices :
            selector === '[data-pp-result]' ? results :
            ['[data-pp-search]', '[data-submit-once]'].includes(selector) ? forms : []; },
        addEventListener(name, fn) { documentHandlers.set(name, [...documentHandlers.get(name) || [], fn]); },
    };
    const callbacks = [];
    class IntersectionObserver {
        constructor(callback) { this.callback = callback; callbacks.push(callback); }
        observe() {} unobserve() {}
    }
    const window = {dataLayer: [], crypto: {randomUUID: () => `00000000-0000-4000-8000-${String(++serial).padStart(12, '0')}`},
        LSAConsent: {parseConsent: () => ({analytics: choice})},
        setTimeout(fn, ms) { const key = Symbol(); timers.set(key, {fn, at: now + ms}); return key; },
        clearTimeout(key) { timers.delete(key); }, queueMicrotask(fn) { microtasks.push(fn); },
        addEventListener(name, fn) { windowHandlers.set(name, fn); },
        sessionStorage: {getItem(key) { if (storageBlocked) throw Error('blocked'); return store.get(key) || null; },
            setItem(key, value) { if (storageBlocked) throw Error('blocked'); store.set(key, value); },
            removeItem: key => store.delete(key), key: i => [...store.keys()][i], get length() { return store.size; }},
    };
    if (observer) window.IntersectionObserver = IntersectionObserver;
    class Clock extends Date { static now() { return now; } }
    const sandbox = {window, document, Date: Clock};
    vm.runInNewContext(analytics, sandbox);
    if (guard) {
        const start = sourceSite.indexOf('document.querySelectorAll("[data-submit-once]")');
        vm.runInNewContext(sourceSite.slice(start, sourceSite.indexOf('\n});', start) + 4), sandbox);
    }
    vm.runInNewContext(prepurchase, sandbox);
    const api = {window, document, store,
        events: name => window.dataLayer.filter(e => e.event === name),
        intersect(target, ratio = 1) { for (const callback of callbacks) callback([{target, isIntersecting: ratio > 0, intersectionRatio: ratio}]); },
        advance(ms) { now += ms; for (const [key, timer] of [...timers]) if (timer.at <= now) { timers.delete(key); timer.fn(); } },
        submit(target, event) { const e = target.fire('submit', event); while (microtasks.length) microtasks.shift()(); return e; },
        dispatch(name, detail = {}) { for (const fn of documentHandlers.get(name) || []) fn({detail}); },
        click(target) { for (const fn of documentHandlers.get('click') || []) fn({target}); },
        consent(value) { choice = value; api.dispatch('lsa:consent-updated', {analytics: value, marketing: false, version: 'test'}); },
        visibility(state) { document.visibilityState = state; api.dispatch('visibilitychange'); },
        pageHide() { windowHandlers.get('pagehide')?.(); }, pageShow() { windowHandlers.get('pageshow')?.(); },
    };
    return api;
}

test('price requires50percent continuous1000ms visibility, not initialization', () => {
    const p = price(), r = run({prices: [p]});
    assert.equal(r.events('price_view').length, 0);
    r.intersect(p, .49); r.advance(2000); assert.equal(r.events('price_view').length, 0);
    r.intersect(p, .5); r.advance(999); assert.equal(r.events('price_view').length, 0);
    r.intersect(p, 0); r.advance(1000); assert.equal(r.events('price_view').length, 0);
    r.intersect(p, .5); r.advance(1000); assert.equal(r.events('price_view').length, 1);
});
test('background time cannot satisfy dwell; returning starts a full new second', () => {
    const p = price(), r = run({prices: [p]});
    r.intersect(p); r.advance(500); r.visibility('hidden'); r.advance(5000);
    assert.equal(r.events('price_view').length, 0);
    r.visibility('visible'); r.advance(999); assert.equal(r.events('price_view').length, 0);
    r.advance(1); assert.equal(r.events('price_view').length, 1);
});
test('pagehide/BFCache pauses dwell and pageshow does not duplicate a completed view', () => {
    const p = price(), r = run({prices: [p]});
    r.intersect(p); r.advance(500); r.pageHide(); r.advance(10000);
    assert.equal(r.events('price_view').length, 0);
    r.pageShow(); r.advance(1000); assert.equal(r.events('price_view').length, 1);
    r.pageHide(); r.pageShow(); r.advance(1000); assert.equal(r.events('price_view').length, 1);
});
test('denied events are not stored/exported; late consent can observe the current price', () => {
    const p = price(), r = run({prices: [p], consent: false});
    r.intersect(p); r.advance(2000); assert.equal(r.store.size, 0);
    assert.equal(r.events('price_view').length, 0);
    r.consent(true); r.advance(1000); assert.equal(r.events('price_view').length, 1);
    r.consent(false); assert.equal(r.store.size, 0);
});
test('withdrawal clears pp storage even on an untracked public page', () => {
    const store = new Map([['lsa:pp1:seen', '{}'], ['unrelated', 'keep']]);
    const r = run({store, page: 'unknown'}); r.consent(false);
    assert.equal(store.has('lsa:pp1:seen'), false); assert.equal(store.get('unrelated'), 'keep');
});
test('no visibility API or blocked storage never becomes an unconditional view', () => {
    const p = price();
    for (const options of [{observer: false}, {storageBlocked: true}]) {
        const r = run({prices: [p], ...options}); r.intersect(p); r.advance(2000);
        assert.equal(r.events('price_view').length, 0);
    }
});
test('refresh/back dedup and display currency changes preserve meaningful observations', () => {
    const store = new Map();
    for (const currency of ['SAR', 'SAR', 'EUR']) {
        const p = price('500', currency), r = run({prices: [p], store,
            context: {attempt_id: id, request_kind: 'property_quote', entry_page: 'home'}});
        r.intersect(p); r.advance(1000);
        assert.equal(r.events('price_view').length, currency === 'SAR' && store.has('tested') ? 0 : 1);
        store.set('tested', 'true');
    }
});
test('invalid/zero/nonfinite prices cannot emit', () => {
    for (const amount of ['0', '-1', 'NaN', 'Infinity', '']) {
        const p = price(amount), r = run({prices: [p]}); r.intersect(p); r.advance(1000);
        assert.equal(r.events('price_view').length, 0);
    }
});
test('an offer expiring before dwell completes is not a valid price/result view and its deadline is never exported', () => {
    const p = price(), marker = element();
    p.dataset.ppValidUntil = marker.dataset.ppValidUntil = new Date(10500).toISOString();
    const r = run({prices: [p], results: [marker], context: {
        attempt_id: id, result_status: 'available', display_status: 'available',
    }});
    r.intersect(p); r.intersect(marker); r.advance(1000);
    assert.equal(r.events('price_view').length, 0);
    assert.equal(r.events('availability_result_view').length, 0);
    assert.equal(JSON.stringify(r.window.dataLayer).includes('1970-'), false);
});
test('valid submission reads approved controls and ignores guest fields/dates in payload', () => {
    const f = form('private-internal-property-key'), r = run({forms: [f], page: 'home'});
    r.submit(f);
    const e = r.events('availability_request')[0];
    assert.equal(e.entry_page, 'home'); assert.equal(e.request_kind, 'property_quote');
    assert.equal(e.nights, 2); assert.equal(e.guests, 2); assert.equal(e.city, 'riyadh');
    const payload = JSON.stringify(e);
    assert.equal(payload.includes('PRIVATE'), false); assert.equal(payload.includes('2030-01-'), false);
    assert.equal(payload.includes('private-internal-property-key'), false);
});
test('all validation/default prevention completes before request; denied submit is not replayed', () => {
    const f = form(), r = run({forms: [f], page: 'home'});
    f.addEventListener('submit', e => e.preventDefault()); r.submit(f);
    assert.equal(r.events('availability_request').length, 0);
    const f2 = form(), denied = run({forms: [f2], page: 'home', consent: false});
    denied.submit(f2); denied.consent(true);
    assert.equal(denied.events('availability_request').length, 0);
});
test('invalid cross-field dates are not accepted requests even when native controls are individually valid', () => {
    const f = form(); f.elements.namedItem('check_out').value = '2030-01-09';
    const r = run({forms: [f], page: 'home'}); r.submit(f);
    assert.equal(r.events('availability_request').length, 0);
});
test('same-tab result-card navigation transfers parent to a later property-detail request', () => {
    const store = new Map(), parent = run({store, page: 'availability_results', context: {
        attempt_id: id, request_kind: 'portfolio', entry_page: 'home', journey_entry: 'home',
    }});
    const link = element({ppPropertyId: 'synthetic-apartment'});
    parent.click(link);
    const f = form('internal'), detail = run({store, forms: [f], page: 'property_detail'});
    detail.submit(f);
    const event = detail.events('availability_request')[0];
    assert.equal(event.parent_attempt_id, id);
    assert.equal(event.entry_page, 'property_detail'); assert.equal(event.journey_entry, 'home');
});
test('actual submit-once guard rejects double click while a fresh accepted retry gets a new attempt', () => {
    const f = form(), r = run({forms: [f], page: 'home', guard: true});
    r.submit(f); r.submit(f);
    assert.equal(r.events('availability_request').length, 1);
    f.dataset.submitting = 'false'; r.submit(f);
    assert.equal(r.events('availability_request').length, 2);
    assert.notEqual(r.events('availability_request')[0].attempt_id, r.events('availability_request')[1].attempt_id);
});
test('home portfolio -> results property request -> visible quote preserves parent and separate origins', () => {
    const f = form(), home = run({forms: [f], page: 'home'}); home.submit(f);
    const first = home.events('availability_request')[0];
    const f2 = form('internal'), result = run({forms: [f2], page: 'availability_results',
        context: {attempt_id: first.attempt_id, request_kind: 'portfolio', entry_page: 'home', journey_entry: 'home'}});
    result.submit(f2); const second = result.events('availability_request')[0];
    assert.notEqual(second.attempt_id, first.attempt_id);
    assert.equal(second.parent_attempt_id, first.attempt_id);
    assert.equal(second.entry_page, 'availability_results'); assert.equal(second.journey_entry, 'home');
    const p = price(), review = run({prices: [p], context: {
        attempt_id: second.attempt_id, parent_attempt_id: second.parent_attempt_id,
        request_kind: 'property_quote', entry_page: second.entry_page, journey_entry: second.journey_entry,
    }});
    review.intersect(p); review.advance(1000);
    const e = review.events('price_view')[0];
    assert.equal(e.attempt_id, second.attempt_id); assert.equal(e.parent_attempt_id, first.attempt_id);
    assert.equal(e.page_kind, 'quote'); assert.equal(e.entry_page, 'availability_results');
});
test('old valid quote has no fabricated request/result/parent or exported surface key', () => {
    const p = price(), r = run({prices: [p], context: {surface_key: 'opaque-private-dedup', entry_page: 'unknown'}});
    r.intersect(p); r.advance(1000);
    assert.equal(r.events('availability_request').length, 0); assert.equal(r.events('availability_result_view').length, 0);
    assert.equal(r.events('price_view')[0].attempt_id, null);
    assert.equal(JSON.stringify(r.events('price_view')[0]).includes('opaque-private-dedup'), false);
});
test('internal and displayed status are separate and require visible message', () => {
    const target = element(), r = run({results: [target], page: 'availability_results', context: {
        attempt_id: id, request_kind: 'portfolio', result_status: 'upstream_error',
        display_status: 'verification_error', error_category: 'provider', result_count: 0, error_count: 2,
    }});
    assert.equal(r.events('availability_result_view').length, 0);
    r.intersect(target); r.advance(1000);
    const e = r.events('availability_result_view')[0];
    assert.equal(e.result_status, 'upstream_error'); assert.equal(e.display_status, 'verification_error');
    assert.equal(e.error_category, 'provider'); assert.equal(e.error_count, 2);
});
test('all four price contexts stay distinct, calendar grid counts one visible cell', () => {
    const calendar1 = price('400', 'SAR', 'calendar_nightly'), calendar2 = price('450', 'SAR', 'calendar_nightly');
    calendar1.dataset.ppGroup = calendar2.dataset.ppGroup = 'non-exported-month';
    const prices = [calendar1, calendar2, price('300', 'SAR', 'indicative_nightly'), price('500', 'SAR', 'search_total'), price()];
    const r = run({prices}); for (const p of prices) r.intersect(p); r.advance(1000);
    assert.equal(r.events('price_view').length, 4);
    assert.equal(JSON.stringify(r.events('price_view')).includes('non-exported-month'), false);
});
test('new/legacy events reset stale custom data and typed pp1 rejects unsafe inputs', () => {
    const r = run(); const push = r.window.LSAAnalytics.pushEvent;
    assert.ok(push('price_view', {measurement_version: 'pp1', price_context: 'quote_total', currency: 'SAR', value: 500, property_id: 'synthetic-apartment'}));
    push('availability_result_view', {measurement_version: 'pp1', result_status: 'no_results', display_status: 'no_results'});
    const latest = r.events('availability_result_view')[0];
    assert.equal(latest.value, null); assert.equal(latest.property_id, null); assert.equal(latest.price_context, null);
    for (const payload of [{value: NaN}, {currency: 'bad'}, {result_status: 'made-up'}, {attempt_id: 'raw-id'}, {extra: {}}, {guest_name: 'private'}]) {
        assert.equal(push('price_view', {measurement_version: 'pp1', price_context: 'quote_total', currency: 'SAR', value: 1, ...payload}), false);
    }
    push('phone_click', {lead_source: 'footer'});
    assert.equal(r.events('phone_click')[0].result_status, null);
    assert.equal(r.events('purchase').length, 0);
});

test('website schemas match every exact reviewed contract parameter and budget', () => {
    const contract = JSON.parse(fs.readFileSync('docs/prepurchase-measurement-contract.json', 'utf8'));
    const values = {measurement_version: 'pp1', attempt_id: id, parent_attempt_id: 'b'.repeat(32),
        request_kind: 'property_quote', entry_page: 'home', journey_entry: 'home', page_kind: 'quote',
        stay_kind: 'overnight', property_id: 'synthetic-apartment', property_name: 'Synthetic Apartment',
        page_language: 'en', city: 'riyadh', nights: 2, guests: 2, result_status: 'available', display_status: 'available',
        result_count: 1, checked_count: 1, error_count: 0, error_category: 'none', price_context: 'quote_total',
        currency: 'EUR', value: 100, source_currency: 'SAR', source_value: 500};
    const r = run();
    for (const event of contract.events) {
        assert.ok(event.parameters.length <= 25);
        const input = Object.fromEntries(event.parameters.map(key => [key, values[key]]));
        const sanitized = r.window.LSAAnalytics.sanitize(event.name, input);
        assert.ok(sanitized);
        assert.deepEqual(Object.keys(sanitized).sort(), [...event.parameters].sort());
    }
});

if (process.env.LSA_PREPURCHASE_FIXTURES) {
    const f = form(), start = run({forms: [f], page: 'home'}); start.submit(f);
    const first = start.events('availability_request')[0];
    const marker = element(), selected = form('internal');
    const results = run({forms: [selected], results: [marker], page: 'availability_results', context: {
        attempt_id: first.attempt_id, request_kind: 'portfolio', entry_page: 'home', journey_entry: 'home',
        result_status: 'partial_error', display_status: 'partial_results', result_count: 1, checked_count: 2,
        error_count: 1, error_category: 'provider',
    }});
    results.intersect(marker); results.advance(1000); results.submit(selected);
    const second = results.events('availability_request')[0];
    const p = price('100', 'EUR'); p.dataset.ppSourceCurrency = 'SAR'; p.dataset.ppSourceValue = '500';
    const review = run({prices: [p], context: {attempt_id: second.attempt_id,
        parent_attempt_id: first.attempt_id, request_kind: 'property_quote', entry_page: 'availability_results', journey_entry: 'home'}});
    review.intersect(p); review.advance(1000);
    fs.writeFileSync(process.env.LSA_PREPURCHASE_FIXTURES, JSON.stringify({synthetic: true, networkCalls: 0,
        events: [first, results.events('availability_result_view')[0], second, review.events('price_view')[0]]}, null, 2));
}
