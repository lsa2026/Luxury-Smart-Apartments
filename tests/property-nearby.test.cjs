const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync('static/js/property-nearby.js', 'utf8');
const modulePromise = import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

class Element {
    constructor(options = {}) { Object.assign(this, options); this.listeners = {}; this.children = []; this.attrs = {}; this.dataset ||= {}; }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    async emit(name, extra = {}) { for (const fn of this.listeners[name] || []) await fn({ target: this, ...extra }); }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    setAttribute(name, value) { this.attrs[name] = value; }
    removeAttribute(name) { delete this.attrs[name]; }
    remove() { this.removed = true; }
    focus() { this.focused = true; }
    showModal() { this.open = true; }
    close() { this.open = false; return this.emit('close'); }
}
function fixture() {
    const names = ['close', 'status', 'search', 'results', 'selected', 'details', 'directions', 'map', 'home'];
    const nodes = Object.fromEntries(names.map((x) => [x, new Element()]));
    const buttons = Object.keys({ coffee: 1, restaurant: 1, laundry: 1, supermarket: 1, mosque: 1, gym: 1, bus: 1, metro: 1 })
        .map((x) => new Element({ dataset: { nearbyCategory: x } }));
    const dialog = new Element({ dataset: Object.fromEntries(['loading', 'ready', 'searching', 'results', 'empty', 'unavailable', 'limit', 'apartment'].map((x) => [x, x])) });
    dialog.querySelector = (selector) => nodes[selector.match(/nearby-(.*)\]/)[1]];
    dialog.querySelectorAll = () => buttons;
    const opener = new Element();
    const classes = new Set();
    const doc = {
        querySelector: (selector) => selector === '[data-nearby-dialog]' ? dialog : opener,
        getElementById: () => ({ textContent: JSON.stringify({ key: 'browser', center: { lat: 31.647129, lng: -8.015223 } }) }),
        createElement: () => new Element(), activeElement: opener,
        body: { classList: { add: (x) => classes.add(x), remove: (x) => classes.delete(x) } },
    };
    const timers = new Map(); let timerId = 0;
    const win = { setTimeout: (fn) => { timers.set(++timerId, fn); return timerId; }, clearTimeout: (id) => timers.delete(id) };
    let mapCount = 0;
    class FakeMap extends Element {
        constructor(node, options) { super(options); mapCount++; }
        addListener(n, f) { this.addEventListener(n, f); }
        getCenter() { return { toJSON: () => this.center }; }
        getBounds() { return { getNorthEast: () => ({ toJSON: () => ({ lat: this.center.lat + .02, lng: this.center.lng + .02 }) }) }; }
        setCenter(x) { this.center = x; void this.emit('bounds_changed'); }
        setZoom(x) { this.zoom = x; void this.emit('bounds_changed'); }
    }
    const libraries = {
        maps: { Map: FakeMap }, marker: { AdvancedMarkerElement: Element },
        places: { PlaceSearchElement: class extends Element { constructor(o) { super(o); this.places = []; } },
            PlaceNearbySearchRequestElement: Element, PlaceDetailsCompactElement: Element, PlaceDetailsPlaceRequestElement: Element },
    };
    const maps = { importLibrary: async (name) => libraries[name] };
    return { doc, win, dialog, nodes, buttons, opener, maps, timers, classes, mapCount: () => mapCount };
}

test('eight categories only, default radius, distance ranking and bounded visible-area search', async () => {
    const { CATEGORIES, searchOptions } = await modulePromise;
    assert.equal(Object.keys(CATEGORIES).length, 8);
    assert.deepEqual(CATEGORIES.coffee, ['cafe', 'coffee_shop']);
    assert.deepEqual(CATEGORIES.gym, ['gym', 'fitness_center']);
    assert.deepEqual(CATEGORIES.bus, ['bus_stop', 'bus_station']);
    assert.equal(searchOptions('metro', { lat: 24, lng: 46 }).locationRestriction.radius, 5000);
    assert.equal(searchOptions('laundry', { lat: 24, lng: 46 }).locationRestriction.radius, 1000);
    assert.equal(searchOptions('mosque', { lat: 24, lng: 46 }, { lat: 35, lng: 40 }).locationRestriction.radius, 10000);
    assert.equal(searchOptions('metro', { lat: 24, lng: 46 }, { lat: 35, lng: 40 }).locationRestriction.radius, 20000);
    assert.equal(searchOptions('coffee', { lat: 24, lng: 46 }).rankPreference, 'DISTANCE');
    assert.throws(() => searchOptions('hotel', {}));
});
test('no SDK, map or query until open; reopening reuses map and restores focus', async () => {
    const { setupNearby } = await modulePromise, f = fixture(); let loads = 0;
    const ui = setupNearby(f.doc, f.win, async () => { loads++; return f.maps; });
    assert.equal(loads, 0); assert.equal(f.mapCount(), 0);
    await ui.open(); assert.equal(loads, 1); assert.equal(f.mapCount(), 1);
    assert.equal(f.nodes.status.textContent, 'ready');
    assert.equal(f.buttons.filter(b => b.attrs['aria-pressed'] === 'true').length, 0);
    assert.equal(ui.getSearchElement(), undefined);
    assert.deepEqual(ui.getMap().center, { lat: 31.647129, lng: -8.015223 });
    await ui.close(); assert.equal(f.opener.focused, true); assert.equal(f.classes.size, 0);
    await ui.open(); assert.equal(loads, 1); assert.equal(f.mapCount(), 1);
});
test('close during lazy load does not construct map', async () => {
    const { setupNearby } = await modulePromise, f = fixture(); let resolve;
    const ui = setupNearby(f.doc, f.win, () => new Promise((r) => { resolve = r; }));
    const loading = ui.open(); ui.close(); resolve(f.maps); await loading;
    assert.equal(f.mapCount(), 0); assert.equal(f.opener.focused, true);
});
test('category queries only explicitly; map movement does not query; area button updates request', async () => {
    const { setupNearby } = await modulePromise, f = fixture();
    const ui = setupNearby(f.doc, f.win, async () => f.maps); await ui.open();
    await f.buttons[0].emit('click');
    const search = ui.getSearchElement();
    const request = search.children[1];
    assert.deepEqual(request.includedTypes, ['cafe', 'coffee_shop']);
    assert.equal(request.locationRestriction.radius, 1000);
    assert.equal(request.locationRestriction.center.lat, 31.647129);
    ui.getMap().center = { lat: 31.65, lng: -8.02 };
    await ui.getMap().emit('bounds_changed');
    assert.equal(ui.getSearchElement(), search); assert.equal(f.nodes.search.hidden, false);
    await search.emit('gmp-load');
    // Injecting time is unnecessary for map movement; explicitly exercise next query after throttle.
    await new Promise((r) => setTimeout(r, 1010));
    await f.nodes.search.emit('click');
    assert.notEqual(ui.getSearchElement(), search);
    assert.deepEqual(ui.getSearchElement().children[1].locationRestriction.center, { lat: 31.65, lng: -8.02 });
    const current = ui.getSearchElement();
    await f.nodes.home.emit('click'); assert.equal(ui.getSearchElement(), current);
    assert.equal(ui.getMap().center.lat, 31.647129);
});
test('API failure, timeout and stale result cannot trap guest or overwrite newer state', async () => {
    const { setupNearby } = await modulePromise, f = fixture();
    const ui = setupNearby(f.doc, f.win, async () => f.maps); await ui.open();
    await f.buttons[2].emit('click'); const old = ui.getSearchElement();
    await old.emit('gmp-error'); assert.equal(f.nodes.status.textContent, 'unavailable');
    assert.equal(f.buttons[2].disabled, false);
    await ui.close(); old.places = [{ id: 'stale' }]; await old.emit('gmp-load');
    assert.equal(f.dialog.open, false); assert.equal(f.opener.focused, true);
    const broken = fixture();
    const failed = setupNearby(broken.doc, broken.win, async () => { throw new Error('Quota'); });
    await failed.open(); assert.equal(broken.nodes.status.textContent, 'unavailable');
    failed.close(); assert.equal(broken.opener.focused, true);
});
test('missing UI Kit never switches to paid Nearby Pro', async () => {
    const { setupNearby } = await modulePromise, f = fixture();
    const ui = setupNearby(f.doc, f.win, async () => ({ importLibrary: async () => ({}) }));
    await ui.open(); assert.equal(f.mapCount(), 0); assert.equal(f.nodes.status.textContent, 'unavailable');
    assert.ok(!source.includes('searchNearby(')); assert.ok(!source.includes('fetchFields('));
    assert.ok(!source.includes('localStorage')); assert.ok(!source.includes('navigator.geolocation'));
});
test('timed-out search releases controls and ignores its late results', async () => {
    const { setupNearby } = await modulePromise, f = fixture();
    const ui = setupNearby(f.doc, f.win, async () => f.maps); await ui.open();
    await f.buttons[0].emit('click'); const old = ui.getSearchElement();
    for (const timer of [...f.timers.values()]) timer();
    assert.equal(f.nodes.status.textContent, 'unavailable');
    assert.equal(f.buttons[0].disabled, false); assert.equal(old.removed, true);
    old.places = [{ id: 'late' }]; await old.emit('gmp-load');
    assert.equal(f.nodes.status.textContent, 'unavailable');
    ui.close();
});
test('failed authentication leaves dialog close usable and does not issue Places queries', async () => {
    const { setupNearby } = await modulePromise, f = fixture();
    const ui = setupNearby(f.doc, f.win, async () => f.maps); await ui.open();
    f.win.gm_authFailure(); assert.equal(f.nodes.status.textContent, 'unavailable');
    assert.equal(f.buttons[0].disabled, true);
    await f.buttons[0].emit('click'); assert.equal(ui.getSearchElement(), undefined);
    ui.close(); await ui.open(); assert.equal(f.mapCount(), 1);
    assert.equal(f.nodes.status.textContent, 'unavailable'); ui.close();
});
test('selected places use Essentials details and late detail errors after closing are ignored', async () => {
    const { setupNearby } = await modulePromise, f = fixture();
    const ui = setupNearby(f.doc, f.win, async () => f.maps); await ui.open();
    await f.buttons[0].emit('click'); const search = ui.getSearchElement();
    const place = { id: 'selected', location: { toJSON: () => ({ lat: 31.1, lng: -8.1 }) } };
    search.places = [place]; await search.emit('gmp-load');
    await new Promise(r => setTimeout(r, 1010));
    await search.emit('gmp-select', { place });
    assert.equal(f.nodes.selected.hidden, false);
    assert.ok(f.nodes.directions.href.includes('destination_place_id=selected'));
    const details = f.nodes.details.children[0];
    assert.equal(details.orientation, 'vertical');
    assert.equal(details.children[0].children[0].attrs['light-scheme-color'], 'gray');
    assert.equal(details.children[1].place, place);
    ui.close(); await ui.open(); await details.emit('gmp-error');
    assert.equal(f.nodes.status.textContent, 'ready'); assert.equal(f.nodes.selected.hidden, true);
    ui.close();
});
test('throttled category click does not label old results as a new category', async () => {
    const { setupNearby } = await modulePromise, f = fixture();
    const ui = setupNearby(f.doc, f.win, async () => f.maps); await ui.open();
    await f.buttons[0].emit('click'); const search = ui.getSearchElement(); await search.emit('gmp-load');
    await f.buttons[1].emit('click');
    assert.equal(ui.getSearchElement(), search); assert.equal(f.buttons[0].attrs['aria-pressed'], 'true');
    assert.notEqual(f.buttons[1].attrs['aria-pressed'], 'true'); assert.equal(f.nodes.status.textContent, 'limit');
    ui.close();
});
test('Google directions identifies the selected place and no user origin', async () => {
    const { directionsUrl } = await modulePromise;
    const url = new URL(directionsUrl({ id: 'place&1', location: { toJSON: () => ({ lat: 31.1, lng: -8.1 }) } }));
    assert.equal(url.searchParams.get('destination_place_id'), 'place&1');
    assert.equal(url.searchParams.get('destination'), '31.1,-8.1');
    assert.equal(url.searchParams.has('origin'), false); assert.equal(directionsUrl({}), null);
});
test('per-page query throttle and count includes detail queries, not a billing cap', async () => {
    const { makeQueryBudget } = await modulePromise; let time = 0;
    const consume = makeQueryBudget(() => time, 2);
    assert.equal(consume(), true); assert.equal(consume(), false);
    time = 1000; assert.equal(consume(), true); time = 2000; assert.equal(consume(), false);
});
test('SDK injects once, uses browser key, locale, nonce and rejects loader error', async () => {
    const { loadMaps } = await modulePromise;
    const appended = [], callbacks = new Map();
    const win = { setTimeout: (fn) => { callbacks.set(1, fn); return 1; }, clearTimeout: () => callbacks.clear() };
    const doc = { createElement: () => new Element(), querySelector: () => ({ nonce: 'nonce-1' }), head: { append: (x) => appended.push(x) } };
    const a = loadMaps({ key: 'browser-only', language: 'ar', region: 'SA' }, win, doc);
    const b = loadMaps({ key: 'browser-only', language: 'ar', region: 'SA' }, win, doc);
    assert.equal(a, b); assert.equal(appended.length, 1); assert.equal(appended[0].nonce, 'nonce-1');
    assert.equal(new URL(appended[0].src).searchParams.get('language'), 'ar');
    appended[0].onerror(); await assert.rejects(a); assert.equal(appended[0].removed, true);
    await assert.rejects(loadMaps({ key: '' }, win, doc));
});
