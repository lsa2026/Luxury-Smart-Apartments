const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("static/js/admin-guest-check.js", "utf8");
const flush = () => new Promise(resolve => setImmediate(resolve));
function setup(fetch, {booking = false} = {}) {
    const button = new EventTarget(), output = {};
    const form = {
        elements: {guest_email: {value: "guest@example.invalid"}, guest_phone: {value: "+966501234567"}, guest_phone_country: {value: "SA"}},
        querySelector: () => ({value: "csrf"}),
        validateGuestPhone: async () => true,
    };
    const panel = {dataset: {checkUrl: "/admin/operations/guest-classification/check/"},
        closest: () => booking ? null : form,
        querySelector: selector => selector === "[data-check-guest]" ? button : selector === "[name=csrfmiddlewaretoken]" ? {value: "csrf"} : output,
    };
    if (booking) panel.dataset.bookingId = "synthetic-booking";
    const timers = [];
    vm.runInNewContext(source, {document: {querySelectorAll: () => [panel]}, window: {setTimeout: fn => timers.push(fn)}, fetch, URLSearchParams});
    return {button, output, form, timers};
}
const response = data => ({ok: true, json: async () => data});
test("private POST refreshes Hostaway, polls and reports without a form submission", async () => {
    let calls = 0;
    const env = setup(async (url, options) => {
        assert.equal(url, "/admin/operations/guest-classification/check/");
        assert.equal(options.method, "POST");
        assert.equal(options.credentials, "same-origin");
        assert.equal(options.cache, "no-store");
        assert.equal(options.headers["X-CSRFToken"], "csrf");
        assert.equal(options.body.get("phone"), "+966501234567");
        assert.equal(options.body.get("refresh"), calls++ ? "" : "1");
        return response(calls === 1 ? {status: "queued"} : {status: "complete", classification: {label: "returning", basis: "Hostaway"}});
    });
    env.button.dispatchEvent(new Event("click"));
    await flush();
    assert.equal(env.button.disabled, true);
    env.timers.shift()(); await flush();
    assert.equal(env.output.textContent, "returning — Hostaway");
    assert.equal(env.button.disabled, false);
    assert.equal(calls, 2);
});
test("edited contacts discard stale classification and permit another check", async () => {
    let resolve;
    const env = setup(() => new Promise(done => {resolve = done;}));
    env.button.dispatchEvent(new Event("click")); await flush();
    env.form.elements.guest_phone.value = "+442079460958";
    resolve(response({status: "complete", classification: {label: "stale", basis: "stale"}}));
    await flush();
    assert.match(env.output.textContent, /تغيرت بيانات الضيف/);
    assert.equal(env.button.disabled, false);
});
test("existing booking sends only its reference and failure allows retry", async () => {
    const env = setup(async (url, options) => {
        assert.deepEqual([...options.body.keys()].sort(), ["booking_id", "refresh"]);
        throw Error("offline");
    }, {booking: true});
    env.button.dispatchEvent(new Event("click")); await flush();
    assert.match(env.output.textContent, /تعذر الاتصال/);
    assert.equal(env.button.disabled, false);
});
