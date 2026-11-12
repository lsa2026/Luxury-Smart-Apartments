const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("static/js/admin-booking-status.js", "utf8");

function setup({fingerprint = "new", fields = [], hidden = false, redirected = false, endpoint = "/bookings/", response = null} = {}) {
  const timers = [], calls = [], listeners = {};
  let reloads = 0;
  const document = {
    hidden, activeElement: null,
    querySelector: () => ({dataset: {bookingStatusUrl: endpoint}}),
    getElementById: () => ({textContent: JSON.stringify({ids: ["synthetic-booking"], fingerprint: "old"})}),
    querySelectorAll: () => fields,
    addEventListener: (name, callback) => {listeners[name] = callback;},
  };
  const window = {
    location: {origin: "https://booking.example", reload: () => {reloads++;}},
    setTimeout: callback => {timers.push(callback);},
  };
  vm.runInNewContext(source, {document, window, URL, fetch: async (url, options) => {
    calls.push({url, options});
    if (response) return response;
    return {ok: true, status: 200, redirected, json: async () => ({fingerprint})};
  }});
  return {timers, calls, listeners, document, reloads: () => reloads};
}

test("changed verified database state reloads the booking list without any POST", async () => {
  const app = setup();
  await app.timers.shift()();
  assert.equal(app.reloads(), 1);
  assert.equal(app.calls.length, 1);
  assert.equal(app.calls[0].url.searchParams.get("payment_status_snapshot"), "1");
  assert.equal(app.calls[0].options.body, undefined);
  assert.equal(app.calls[0].options.credentials, "same-origin");
});
test("unchanged or hidden pages never replace the current page", async () => {
  for (const options of [{fingerprint: "old"}, {hidden: true}]) {
    const app = setup(options);
    await app.timers.shift()();
    assert.equal(app.reloads(), 0);
    assert.equal(app.timers.length, 1);
  }
});
test("search edits survive a payment update, including selects with an implicit default", async () => {
  const select = {type: "select-one", tagName: "SELECT", options: [{defaultSelected: false}, {defaultSelected: false}], selectedIndex: 1};
  const app = setup({fields: [select]});
  await app.timers.shift()();
  assert.equal(app.reloads(), 0);
  select.selectedIndex = 0;
  await app.timers.shift()();
  assert.equal(app.reloads(), 1);
  assert.equal(app.calls.length, 1);
});
test("submission, a login redirect and foreign endpoints stop passive checks", async () => {
  const submitted = setup();
  submitted.listeners.submit();
  await submitted.timers.shift()();
  assert.equal(submitted.calls.length, 0);
  const redirected = setup({redirected: true});
  await redirected.timers.shift()();
  assert.equal(redirected.reloads(), 0);
  assert.equal(redirected.timers.length, 0);
  assert.equal(setup({endpoint: "https://untrusted.example/"}).timers.length, 0);
});
test("a status response arriving during form submission cannot interrupt the action", async () => {
  for (const delayedJson of [false, true]) {
    let release;
    const pending = new Promise(resolve => {release = resolve;});
    const reply = {ok: true, status: 200, redirected: false, json: async () => delayedJson ? pending : ({fingerprint: "new"})};
    const app = setup({response: delayedJson ? reply : pending});
    const check = app.timers.shift()();
    await Promise.resolve();
    app.listeners.submit();
    release(delayedJson ? {fingerprint: "new"} : reply);
    await check;
    assert.equal(app.reloads(), 0);
    assert.equal(app.timers.length, 0);
  }
});
