const test = require('node:test');
const assert = require('node:assert/strict');
const {initializeBookingDetail} = require('../static/js/admin-booking-detail.js');

function element(dataset = {}) {
  return {dataset, attributes: {}, listeners: {}, hidden: false,
    setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(name, fn) { this.listeners[name] = fn; },
    focus() { this.focused = true; }};
}
function fixture(initial = 'overview', hash = '') {
  const names = ['overview', 'invoice', 'payment', 'edit'];
  const root = element({defaultPanel: initial});
  const tabs = names.map(bookingTab => element({bookingTab}));
  const panels = names.map(bookingPanel => element({bookingPanel}));
  const link = element({bookingOpen: 'invoice'});
  const nav = element();
  root.querySelectorAll = selector => ({'[data-booking-tab]': tabs,
    '[data-booking-panel]': panels, '[data-booking-open]': [link]}[selector] || []);
  root.querySelector = selector => selector === '[data-booking-tabs]' ? nav : root.error || null;
  const browser = element();
  browser.location = {hash};
  browser.history = {replaceState(a, b, url) { browser.location.hash = url; }};
  return {root, browser, tabs, panels, link};
}
function event(extra = {}) {
  return {prevented: false, preventDefault() { this.prevented = true; }, ...extra};
}
function active(f) { return f.panels.filter(p => !p.hidden).map(p => p.dataset.bookingPanel); }

test('progressively enhances only complete markup, initializes once', () => {
  const f = fixture();
  assert.deepEqual(active(f), ['overview', 'invoice', 'payment', 'edit']);
  assert.ok(initializeBookingDetail(f.root, f.browser));
  assert.deepEqual(active(f), ['overview']);
  assert.equal(initializeBookingDetail(f.root, f.browser), null);
  const broken = fixture();
  broken.panels.pop();
  assert.equal(initializeBookingDetail(broken.root, broken.browser), null);
  assert.equal(broken.root.dataset.tabsReady, undefined);
  assert.ok(broken.panels.every(p => !p.hidden));
});
test('tab and quick invoice links navigate without submitting any form', () => {
  const f = fixture(); initializeBookingDetail(f.root, f.browser);
  const click = event(); f.tabs[2].listeners.click(click);
  assert.ok(click.prevented); assert.deepEqual(active(f), ['payment']);
  assert.equal(f.browser.location.hash, '#booking-payment');
  f.link.listeners.click(event()); assert.deepEqual(active(f), ['invoice']);
  assert.equal(f.tabs[1].attributes['aria-selected'], 'true');
});
test('RTL keyboard navigation, first and last tab remain accessible', () => {
  const f = fixture(); initializeBookingDetail(f.root, f.browser);
  for (const [index, key, expected] of [[0,'ArrowLeft','invoice'], [0,'ArrowRight','edit'],
    [1,'Home','overview'], [1,'End','edit']]) {
    const e = event({key}); f.tabs[index].listeners.keydown(e);
    assert.ok(e.prevented); assert.deepEqual(active(f), [expected]);
  }
  const enter = event({key:'Enter'}); f.tabs[0].listeners.keydown(enter);
  assert.equal(enter.prevented, false);
});
test('only known hash panels accepted and server workflow takes priority', () => {
  const f = fixture('overview', '#booking-invoice'); initializeBookingDetail(f.root, f.browser);
  assert.deepEqual(active(f), ['invoice']);
  f.browser.location.hash = '#booking-<script>'; f.browser.listeners.hashchange();
  assert.deepEqual(active(f), ['invoice']);
  const pending = fixture('edit', '#booking-payment');
  initializeBookingDetail(pending.root, pending.browser);
  assert.deepEqual(active(pending), ['edit']);
});
test('invalid fields and server errors reveal their tab and closed details', () => {
  const f = fixture();
  const fold = {open: false, parentElement: null};
  const field = {closest(selector) {return selector === 'details' ? fold : f.panels[2];}};
  f.root.error = field; initializeBookingDetail(f.root, f.browser);
  assert.deepEqual(active(f), ['payment']); assert.equal(fold.open, true);
  f.tabs[0].listeners.click(event()); fold.open = false;
  f.root.listeners.invalid(event({target:field}));
  assert.deepEqual(active(f), ['payment']); assert.equal(fold.open, true);
});
test('genuine form submission is never prevented or rewritten', () => {
  const f = fixture(); initializeBookingDetail(f.root, f.browser);
  const form = {action:'/real-action/', method:'post', value:'unchanged',
    closest(selector) { return selector === 'details' ? null : f.panels[3]; }};
  const e = event({target:form}); f.root.listeners.submit(e);
  assert.equal(e.prevented, false); assert.equal(form.action, '/real-action/');
  assert.equal(form.method, 'post'); assert.equal(form.value, 'unchanged');
  assert.deepEqual(active(f), ['edit']);
});
