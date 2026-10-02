const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("static/js/site.js", "utf8");
const start = source.indexOf("// Draft autosave for the guest details step.");
assert.ok(start >= 0, "exercise the real draft initializer in site.js");
const draftCode = source.slice(start);

class Field extends EventTarget {
    constructor(name, value = "", tagName = "INPUT") {
        super(); this.name = name; this.value = value; this.tagName = tagName;
        this.type = tagName === "SELECT" ? "select-one" : "text";
        this.options = ["", "SA", "MA", "GB", "US", "CA", "FR"].map((value) => ({value}));
    }
}
function page(storage, {bound = "false", country = "SA", draftForm = "guest-details"} = {}) {
    const fields = {
        billing_country: new Field("billing_country", country, "SELECT"),
        billing_city: new Field("billing_city"),
        billing_state: new Field("billing_state"),
        billing_postcode: new Field("billing_postcode"),
    };
    const form = new EventTarget();
    form.dataset = {draftForm};
    if (bound !== undefined && bound !== null) form.dataset.draftServerBound = bound;
    form.querySelectorAll = () => Object.values(fields);
    let countryChanges = 0;
    fields.billing_country.addEventListener("change", (event) => {
        assert.equal(event.bubbles, true); countryChanges++;
    });
    vm.runInNewContext(draftCode, {
        document: {querySelectorAll: () => [form]}, window: {sessionStorage: storage}, Event,
        // Draft handling must not transmit any address or country to analytics.
        gtag() { assert.fail("unexpected analytics call"); },
        dataLayer: {push() { assert.fail("unexpected analytics event"); }},
    });
    return {fields, form, changes: () => countryChanges};
}
function store(saved) {
    const values = new Map([["lsa-draft:guest-details", JSON.stringify(saved)]]);
    return {getItem: (key) => values.get(key) || null, setItem: (key, value) => values.set(key, value), values};
}
const address = {billing_country: "GB", billing_city: "London", billing_state: "England", billing_postcode: "SW1A 2AA"};

test("saved GB remains consistent with London on AR/FR/EN fresh-page reloads", () => {
    const storage = store(address);
    for (const language of ["ar", "fr", "en"]) {
        const current = page(storage);
        for (const [name, value] of Object.entries(address)) {
            assert.equal(current.fields[name].value, value, `${language}: ${name}`);
        }
        assert.equal(current.changes(), 1);
    }
    assert.equal(page(storage, {country: "MA"}).fields.billing_country.value, "GB");
});

test("invalid saved ISO country cannot overwrite the fresh select", () => {
    for (const country of ["XX", "ZZ", "gb", "GB ", "", "United Kingdom", 123, null, {}, ["GB"]]) {
        const current = page(store({...address, billing_country: country}));
        assert.equal(current.fields.billing_country.value, "SA");
        assert.equal(current.changes(), 0);
    }
});

test("server-bound POST country always wins, including invalid/empty submissions", () => {
    for (const country of ["US", "FR", ""]) {
        const current = page(store(address), {bound: "true", country});
        assert.equal(current.fields.billing_country.value, country);
        assert.equal(current.changes(), 0);
    }
});

test("change-only country selection is saved, as are ordinary input events", () => {
    const storage = store(address);
    const current = page(storage);
    current.fields.billing_country.value = "CA";
    current.form.dispatchEvent(new Event("change", {bubbles: true}));
    assert.equal(JSON.parse(storage.getItem("lsa-draft:guest-details")).billing_country, "CA");
    current.fields.billing_city.value = "Toronto";
    current.form.dispatchEvent(new Event("input", {bubbles: true}));
    assert.equal(JSON.parse(storage.getItem("lsa-draft:guest-details")).billing_city, "Toronto");
    assert.equal(page(storage).fields.billing_country.value, "CA");
});

test("unmarked / other draft forms keep their existing blank-only behavior", () => {
    assert.equal(page(store(address), {bound: null}).fields.billing_country.value, "SA");
    const storage = store({});
    storage.setItem("lsa-draft:other-form", JSON.stringify(address));
    const current = page(storage, {draftForm: "other-form"});
    assert.equal(current.fields.billing_country.value, "SA");
    current.fields.billing_country.value = "FR";
    current.form.dispatchEvent(new Event("change", {bubbles: true}));
    assert.equal(JSON.parse(storage.getItem("lsa-draft:other-form")).billing_country, "GB");
});

test("storage blocked/unavailable does not block the manual form", () => {
    const current = page({getItem() {throw new Error("blocked");}, setItem() {throw new Error("blocked");}});
    assert.equal(current.fields.billing_country.value, "SA");
    current.form.dispatchEvent(new Event("change", {bubbles: true}));
    current.form.dispatchEvent(new Event("input", {bubbles: true}));
});
