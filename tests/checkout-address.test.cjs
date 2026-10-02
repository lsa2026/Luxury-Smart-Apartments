const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const source = fs.readFileSync("static/js/checkout-address.js", "utf8");
const api = import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

class Element extends EventTarget {
    constructor(value = "") {
        super(); this.value = value; this.attrs = {}; this.children = []; this.hidden = false;
    }
    setAttribute(name, value) { this.attrs[name] = value; }
    removeAttribute(name) { delete this.attrs[name]; }
    replaceChildren() { this.children = []; }
    append(child) { this.children.push(child); child.parent = this; }
    scrollIntoView() {}
    focus() { this.focused = true; }
}
function setup() {
    const elements = Object.fromEntries([
        "billing_street1", "billing_city", "billing_state", "billing_country", "billing_postcode",
    ].map((name) => [name, new Element(`manual-${name}`)]));
    elements.billing_country.value = "SA";
    elements.billing_country.options = ["", "SA", "US", "GB", "CA", "FR", "MA", "JP"].map((value) => ({value}));
    elements.csrfmiddlewaretoken = {value: "synthetic-csrf"};
    const form = new Element(); form.elements = elements;
    const nodes = Object.fromEntries([
        "search", "options", "results", "status",
    ].map((kind) => [`[data-address-${kind}]`, new Element()]));
    nodes["[data-address-options]"].id = "suggestions";
    const panel = new Element(); panel.hidden = true;
    panel.dataset = {
        addressTicket: "ticket-1", addressSuggestionsUrl: "/address-suggestions/",
        addressDetailsUrl: "/address-details/", addressLoading: "loading", addressChoose: "choose",
        addressEmpty: "empty", addressFallback: "fallback", addressReview: "review",
    };
    panel.querySelector = (selector) => nodes[selector];
    panel.closest = () => form;
    panel.ownerDocument = {createElement: () => new Element()};
    panel.contains = (child) => Object.values(nodes).includes(child);
    const timers = new Map(); let number = 0;
    return { panel, form, nodes, elements,
        input: nodes["[data-address-search]"], list: nodes["[data-address-options]"],
        status: nodes["[data-address-status]"],
        schedule: (fn) => { timers.set(++number, fn); return number; },
        cancel: (id) => timers.delete(id),
        flush: async () => { const callbacks = [...timers.values()]; timers.clear(); for (const fn of callbacks) await fn(); },
        timers,
    };
}
const result = (body, status = 200) => ({ok: status === 200, status, json: async () => body});
const suggestion = {place_id: "ChIJ_test", label: "10 King Street, London, UK"};
const tick = () => new Promise((resolve) => setImmediate(resolve));
function type(env, value) { env.input.value = value; env.input.dispatchEvent(new Event("input")); }
function key(input, value) {
    const event = new Event("keydown", {cancelable: true}); event.key = value;
    input.dispatchEvent(event); return event;
}

test("maps only valid known components, preserves missing/manual fields and fires draft events", async () => {
    const {applyAddressFields, addressSnapshot} = await api;
    const env = setup(); const before = addressSnapshot(env.form);
    let inputs = 0, changes = 0;
    env.elements.billing_postcode.addEventListener("input", (e) => { assert.ok(e.bubbles); inputs++; });
    env.elements.billing_postcode.addEventListener("change", (e) => { assert.ok(e.bubbles); changes++; });
    assert.equal(applyAddressFields(env.form, {
        billing_country: "GB", billing_postcode: "SW1A 1AA", billing_city: "London",
        billing_state: "", billing_street1: "x".repeat(101), injected: "ignored",
    }), 3);
    assert.equal(env.elements.billing_state.value, before.billing_state);
    assert.equal(env.elements.billing_street1.value, before.billing_street1);
    assert.equal(inputs, 1); assert.equal(changes, 1);
    assert.equal(applyAddressFields(env.form, {billing_country: "XX", billing_postcode: "12 34!"}), 0);
});

test("international postcodes are preserved, not truncated or stripped", async () => {
    const {applyAddressFields} = await api;
    for (const [country, postal] of [["GB", "SW1A 1AA"], ["CA", "M5V 3L9"], ["US", "10001-1234"],
        ["SA", "12345"], ["MA", "40000"], ["FR", "75001"], ["JP", "100-0001"]]) {
        const env = setup();
        assert.equal(applyAddressFields(env.form, {billing_country: country, billing_postcode: postal}), 2);
        assert.equal(env.elements.billing_country.value, country);
        assert.equal(env.elements.billing_postcode.value, postal);
    }
});

test("a country changed during Details prevents stale cross-country autofill", async () => {
    const {applyAddressFields, addressSnapshot} = await api;
    const env = setup();
    const before = addressSnapshot(env.form);
    env.elements.billing_country.value = "CA";
    assert.equal(applyAddressFields(env.form, {
        billing_street1: "10 King Street", billing_city: "London", billing_state: "England",
        billing_country: "GB", billing_postcode: "SW1A 1AA",
    }, before), 0);
    assert.equal(env.elements.billing_country.value, "CA");
    assert.equal(env.elements.billing_postcode.value, before.billing_postcode);
    assert.equal(env.elements.billing_state.value, before.billing_state);
});

test("debounces, uses same-origin CSRF POST and keyboard selection rotates ticket", async () => {
    const {initCheckoutAddress} = await api;
    const env = setup(); const calls = [];
    initCheckoutAddress(env.panel, {...env, fetcher: async (url, options) => {
        calls.push({url, options});
        assert.equal(options.method, "POST"); assert.equal(options.cache, "no-store");
        assert.equal(options.credentials, "same-origin");
        assert.equal(options.headers["X-CSRFToken"], "synthetic-csrf");
        return result(url.includes("suggestions") ? {suggestions: [suggestion]}
            : {fields: {billing_city: "London", billing_country: "GB", billing_postcode: "SW1A 1AA"}, next_ticket: "ticket-2"});
    }});
    assert.equal(env.panel.hidden, false);
    type(env, "Lo"); assert.equal(env.timers.size, 0);
    type(env, "Lond"); type(env, "London"); assert.equal(env.timers.size, 1);
    await env.flush(); assert.equal(calls.length, 1);
    assert.deepEqual(JSON.parse(calls[0].options.body), {ticket: "ticket-1", input: "London"});
    assert.equal(env.list.children[0].textContent, suggestion.label);
    assert.equal(env.input.attrs["aria-expanded"], "true");
    key(env.input, "ArrowDown");
    assert.equal(env.list.children[0].attrs["aria-selected"], "true");
    assert.ok(key(env.input, "Enter").defaultPrevented); await tick();
    assert.deepEqual(JSON.parse(calls[1].options.body), {ticket: "ticket-1", place_id: "ChIJ_test"});
    assert.equal(env.elements.billing_country.value, "GB");
    assert.equal(env.elements.billing_postcode.value, "SW1A 1AA");
    assert.equal(env.status.textContent, "review");
    type(env, "Paris"); await env.flush();
    assert.equal(JSON.parse(calls[2].options.body).ticket, "ticket-2");
});

test("manual edits made while Details is pending take priority", async () => {
    const {initCheckoutAddress} = await api;
    const env = setup(); let resolveDetails;
    initCheckoutAddress(env.panel, {...env, fetcher: async (url) => {
        if (url.includes("suggestions")) return result({suggestions: [suggestion]});
        return new Promise((resolve) => { resolveDetails = resolve; });
    }});
    type(env, "London"); await env.flush();
    env.list.children[0].dispatchEvent(new Event("click"));
    env.elements.billing_city.value = "My corrected city";
    resolveDetails(result({fields: {billing_city: "London", billing_country: "GB"}, next_ticket: "ticket-2"}));
    await tick();
    assert.equal(env.elements.billing_city.value, "My corrected city");
    assert.equal(env.elements.billing_country.value, "GB");
});

test("late results cannot replace newer suggestions or their signed ticket", async () => {
    const {initCheckoutAddress} = await api;
    const env = setup(); const pending = []; const calls = [];
    initCheckoutAddress(env.panel, {...env, fetcher: (url, options) => {
        calls.push(JSON.parse(options.body));
        return new Promise((resolve) => pending.push(resolve));
    }});
    type(env, "Old search"); const old = env.flush();
    type(env, "New search"); const newer = env.flush();
    pending[1](result({suggestions: [suggestion]})); await newer;
    pending[0](result({detail: "session_expired", next_ticket: "stale-ticket"}, 409)); await old;
    assert.equal(env.list.children[0].textContent, suggestion.label);
    env.list.children[0].dispatchEvent(new Event("click"));
    assert.equal(calls[2].ticket, "ticket-1");
    pending[2](result({fields: {}, next_ticket: "ticket-2"})); await tick();
});

test("service limits and failures leave manual fields usable and do not retry paid requests", async () => {
    const {initCheckoutAddress, addressSnapshot} = await api;
    for (const code of [429, 503]) {
        const env = setup(); const before = addressSnapshot(env.form); let count = 0;
        initCheckoutAddress(env.panel, {...env, fetcher: async () => { count++; return result({}, code); }});
        type(env, "London"); await env.flush();
        type(env, "Paris"); await env.flush();
        assert.equal(count, 1);
        assert.equal(env.status.textContent, "fallback");
        assert.deepEqual(addressSnapshot(env.form), before);
        assert.equal(env.elements.billing_street1.disabled, undefined);
        env.form.dispatchEvent(new Event("submit"));
    }
    assert.doesNotMatch(source, /sessionStorage|localStorage|dataLayer|gtag\(|console\.log/);
});

test("renews expired sessions once and safely renders labels as text", async () => {
    const {initCheckoutAddress} = await api;
    const env = setup(); const calls = [];
    initCheckoutAddress(env.panel, {...env, fetcher: async (url, options) => {
        calls.push(JSON.parse(options.body));
        return calls.length === 1 ? result({detail: "session_expired", next_ticket: "ticket-2"}, 409)
            : result({suggestions: [{...suggestion, label: "<img src=x onerror=alert(1)>"}]});
    }});
    type(env, "London"); await env.flush();
    assert.equal(calls.length, 2); assert.equal(calls[1].ticket, "ticket-2");
    assert.equal(env.list.children[0].textContent, "<img src=x onerror=alert(1)>");
    assert.equal(env.list.children[0].children.length, 0);
    key(env.input, "Escape"); assert.equal(env.input.attrs["aria-expanded"], "false");
});
