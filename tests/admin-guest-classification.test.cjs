const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("static/js/admin-guest-classification.js", "utf8");

function setup(states) {
    const timers = [], requests = [], output = {}, listeners = {};
    const panel = { dataset: { guestClassificationState: "queued", statusUrl: "/admin/operations/bookings/?classification_status=1" }, querySelector() { return output; } };
    const form = { addEventListener(name, handler) { listeners[name] = handler; } };
    let reloads = 0;
    const document = { hidden: false, querySelector(selector) { return selector.startsWith("[data-") ? panel : form; } };
    vm.runInNewContext(source, { document, window: { setTimeout(fn) { timers.push(fn); }, location: { reload() { reloads++; } } }, fetch: async (url, options) => {
        requests.push({ url, options });
        return { ok: true, json: async () => ({ status: states.shift() }) };
    } });
    return { timers, requests, output, listeners, document, reloads: () => reloads };
}

test("pending source polls cached private status and reloads only after completion", async () => {
    const app = setup(["running", "complete"]);
    await app.timers.shift()();
    assert.equal(app.reloads(), 0);
    await app.timers.shift()();
    assert.equal(app.reloads(), 1);
    assert.equal(app.requests.length, 2);
    assert.ok(app.requests.every(r => r.options.credentials === "same-origin" && r.options.cache === "no-store"));
    assert.ok(app.requests.every(r => r.url.endsWith("?classification_status=1") && !r.options.body));
});
test("editing search prevents automatic replacement of the user's work", async () => {
    const app = setup(["complete"]);
    app.listeners.input();
    await app.timers.shift()();
    assert.equal(app.reloads(), 0);
    assert.match(app.output.textContent, /اكتمل/);
});
test("source failure or hidden page stops polling without claiming new", async () => {
    for (const hidden of [false, true]) {
        const app = setup(["unavailable"]);
        app.document.hidden = hidden;
        await app.timers.shift()();
        assert.equal(app.timers.length, 0);
        assert.equal(app.reloads(), 0);
        assert.ok(app.output.textContent);
    }
});
