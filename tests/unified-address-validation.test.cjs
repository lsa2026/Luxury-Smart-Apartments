const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { attach, fieldError } = require("../static/js/morning-validation.js");

test("invalid billing reveals its checkout step and collapsed edit section before reporting", () => {
    const details = { setAttribute() { this.open = true; } };
    const panels = [1, 2].map(number => ({
        dataset: { journeyPanel: String(number) }, hidden: false,
        classList: { toggle() {} }, querySelectorAll() { return number === 2 ? [field] : []; },
    }));
    const field = new EventTarget();
    Object.assign(field, {
        id: "id_billing_state", name: "billing_state", value: "Invalid Region",
        maxLength: -1, required: true, willValidate: true, validationMessage: "",
        dataset: { morningPattern: "[a-zA-Z0-9.]{1,50}", morningError: "No spaces" },
        ownerDocument: { documentElement: { lang: "en" } },
        validity: { valid: true },
        hasAttribute() { return false; }, getAttribute() { return ""; }, setAttribute() {},
        closest(selector) { return selector === "details" ? details : panels[1]; },
        setCustomValidity(message) { this.validationMessage = message; this.validity.valid = !message; },
        checkValidity() { return this.validity.valid; },
    });
    const form = new EventTarget();
    Object.assign(form, {
        dataset: {}, classList: { add() {} },
        querySelector(selector) {
            if (selector === "[data-journey-panel='1']") return panels[0];
            if (selector === "[data-journey-panel='2']") return panels[1];
            return selector.startsWith("#") ? {} : null;
        },
        querySelectorAll(selector) {
            if (selector === "[data-journey-panel]") return panels;
            if (selector === "[data-journey-indicator]") return [];
            return [field];
        },
        checkValidity() { return field.checkValidity(); },
        reportValidity() {
            assert.equal(panels[1].hidden, false);
            assert.equal(details.open, true);
            this.reported = true;
        },
    });
    const site = fs.readFileSync("static/js/site.js", "utf8");
    const start = site.indexOf('document.querySelectorAll("[data-guest-journey]")');
    const end = site.indexOf('document.querySelectorAll("[data-card-image]")', start);
    assert.ok(start > 0 && end > start);
    vm.runInNewContext(site.slice(start, end), { document: { querySelectorAll() { return [form]; } } });
    attach(form);
    assert.equal(panels[1].hidden, true);
    const submit = new Event("submit", { cancelable: true });
    form.dispatchEvent(submit);
    assert.equal(submit.defaultPrevented, true);
    assert.equal(form.reported, true);
    assert.equal(field.validationMessage, "No spaces");
    field.value = "Riyadh";
    field.dispatchEvent(new Event("input"));
    assert.equal(field.validationMessage, "");
});

test("French native field guidance uses French and preserves foreign validation ownership", () => {
    const field = {
        value: "", dataset: {}, ownerDocument: { documentElement: { lang: "fr" } },
        validity: { valueMissing: true, valid: false },
    };
    assert.equal(fieldError(field), "Remplissez ce champ.");
    field.validity.customError = true;
    field.validationMessage = "Phone validator error";
    assert.equal(fieldError(field), "Phone validator error");
});
