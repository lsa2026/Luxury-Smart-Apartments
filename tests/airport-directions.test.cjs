const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const modulePromise = import(`data:text/javascript;base64,${Buffer.from(fs.readFileSync("static/js/airport-directions.js", "utf8")).toString("base64")}`);
const nodes = () => ({
    "[data-route-status]": {textContent: ""},
    "[data-route-updated]": {textContent: "", hidden: true},
    "[data-route-attribution]": {hidden: true},
});
function makePanel() {
    const elements = nodes();
    return {
        dataset: {
            routeUrl: "/ar/properties/darat/airport-route/", routeTicket: "signed-test-ticket",
            csrfToken: "csrf-test-token", loading: "loading", unavailable: "unavailable",
            estimate: "Now {minutes} min / {distance} km", updated: "Calculated at {time}",
        },
        querySelector: (selector) => elements[selector], elements,
    };
}

test("one-shot POST is same-origin, no-store, ticket-only and uses real values", async () => {
    const {loadAirportEstimate} = await modulePromise;
    global.document = {documentElement: {lang: "en"}};
    const panel = makePanel();
    let calls = 0;
    const request = async (url, options) => {
        calls++;
        assert.equal(url, panel.dataset.routeUrl);
        assert.equal(options.method, "POST");
        assert.equal(options.credentials, "same-origin");
        assert.equal(options.cache, "no-store");
        assert.deepEqual(JSON.parse(options.body), {ticket: "signed-test-ticket"});
        assert.equal(options.headers["X-CSRFToken"], "csrf-test-token");
        return {ok: true, json: async () => ({duration_minutes: 28, distance_km: 35.1,
            calculated_at: "2026-10-02T06:00:00Z", attribution: "Google Maps"})};
    };
    await loadAirportEstimate(panel, request);
    await loadAirportEstimate(panel, request);
    assert.equal(calls, 1);
    assert.equal(panel.elements["[data-route-status]"].textContent, "Now 28 min / 35.1 km");
    assert.match(panel.elements["[data-route-updated]"].textContent, /09:00/);
    assert.equal(panel.elements["[data-route-attribution]"].hidden, false);
});

test("failed or malformed response preserves links and never substitutes a static estimate", async () => {
    const {loadAirportEstimate} = await modulePromise;
    for (const result of [{ok: false}, {ok: true, json: async () => ({duration_minutes: 0})}]) {
        const panel = makePanel();
        await loadAirportEstimate(panel, async () => result);
        assert.equal(panel.elements["[data-route-status]"].textContent, "unavailable");
        assert.equal(panel.elements["[data-route-updated]"].hidden, true);
        assert.equal(panel.elements["[data-route-attribution]"].hidden, true);
    }
});

test("intersection observation does not request anything before the panel is visible", async () => {
    const {observeAirportPanel} = await modulePromise;
    global.document = {hidden: false, addEventListener() {}, removeEventListener() {}};
    let callback;
    class Observer {
        constructor(handler) { callback = handler; }
        observe() {}
        disconnect() {}
    }
    const panel = makePanel();
    observeAirportPanel(panel, Observer);
    callback([{isIntersecting: false}]);
    assert.equal(panel.dataset.requested, undefined);
    global.document.hidden = true;
    callback([{isIntersecting: true}]);
    assert.equal(panel.dataset.requested, undefined);
});
