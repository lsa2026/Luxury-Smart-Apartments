const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("static/js/checkout-phone.js", "utf8");
class Element extends EventTarget {
    constructor(value = "") { super(); this.value = value; this.attributes = {}; }
    setAttribute(key, value) { this.attributes[key] = value; }
    removeAttribute(key) { delete this.attributes[key]; }
    setCustomValidity(message) { this.validityMessage = message; }
    focus() { this.focused = true; }
}
function setup(fetch, {saved = null, bound = false, initial = ""} = {}) {
    const country = new Element("SA");
    country.options = ["SA", "US", "FR", "MA", "IT"].map((value) => ({value, textContent: `${value} (+1)`}));
    const phone = new Element(initial);
    const error = new Element(); error.hidden = true;
    const storage = new Map(saved ? [["lsa-phone-country-v1", saved]] : []);
    const form = new Element(); form.dataset = {draftServerBound: String(bound)};
    form.querySelector = (selector) => selector === "#guest-phone-validation" ? error : {value: "csrf"};
    const widget = new Element();
    widget.dataset = {checkUrl: "/phone-check/", invalidMessage: "invalid number"};
    widget.closest = () => form;
    widget.querySelector = (selector) => selector === "[data-phone-country]" ? country : phone;
    vm.runInNewContext(source, {document: {documentElement: {lang: "en"}, querySelectorAll: () => [widget]},
        localStorage: {getItem: (key) => storage.get(key), setItem: (key, value) => storage.set(key, value)},
        Intl, Event, URLSearchParams, AbortController, setTimeout, clearTimeout, fetch});
    return {form, country, phone, error, storage};
}
const response = (body, status = 200) => ({ok: status === 200, status, json: async () => body});
test("defaults to Saudi; remembers only country and never overwrites a bound form", () => {
    assert.equal(setup().country.value, "SA");
    assert.equal(setup(null, {saved: "US"}).country.value, "US");
    assert.equal(setup(null, {saved: "US", bound: true}).country.value, "SA");
    assert.equal(setup(null, {saved: "US", initial: "0501234567"}).country.value, "SA");
});
test("normalizes once via protected POST, updates pasted international country and reuses result", async () => {
    let calls = 0;
    const env = setup(async (url, options) => {
        calls++;
        assert.equal(url, "/phone-check/");
        assert.equal(options.method, "POST");
        assert.equal(options.headers["X-CSRFToken"], "csrf");
        assert.equal(options.body.get("phone"), "+1 212 555 1234");
        return response({phone: "+12125551234", country: "US"});
    });
    env.phone.value = "+1 212 555 1234";
    assert.equal(await env.form.validateGuestPhone(), true);
    assert.equal(env.phone.value, "+12125551234");
    assert.equal(env.country.value, "US");
    assert.equal(await env.form.validateGuestPhone(), true);
    assert.equal(calls, 1);
    assert.deepEqual([...env.storage], [["lsa-phone-country-v1", "US"]]);
});
test("invalid number is shown inline and prevents next step; typing clears it", async () => {
    const env = setup(async () => response({detail: "invalid_phone"}, 400));
    env.phone.value = "12345";
    assert.equal(await env.form.validateGuestPhone(), false);
    assert.equal(env.error.hidden, false);
    assert.equal(env.phone.focused, true);
    assert.equal(env.phone.attributes["aria-invalid"], "true");
    env.phone.dispatchEvent(new Event("input"));
    assert.equal(env.phone.validityMessage, "");
    assert.equal(env.error.hidden, true);
});
test("network failure and unavailable quotes leave final server validation available", async () => {
    for (const fetch of [async () => {throw Error("offline");}, async () => response({}, 409)]) {
        const env = setup(fetch); env.phone.value = "0501234567";
        assert.equal(await env.form.validateGuestPhone(), true);
        assert.equal(env.phone.value, "0501234567");
    }
});
test("stale response cannot overwrite edited phone; including edit-away-then-back", async () => {
    let release; let calls = 0;
    const env = setup(async () => {
        if (++calls === 1) await new Promise((resolve) => {release = resolve;});
        return response({phone: "+966501234567", country: "SA"});
    });
    env.phone.value = "0501234567";
    const pending = env.form.validateGuestPhone();
    env.phone.value = "0507654321"; env.phone.dispatchEvent(new Event("input"));
    env.phone.value = "0501234567"; env.phone.dispatchEvent(new Event("input"));
    release();
    assert.equal(await pending, true);
    assert.equal(calls, 2);
    assert.equal(env.phone.value, "+966501234567");
});
