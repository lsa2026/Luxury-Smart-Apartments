const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync("static/js/apple-pay-checkout.js", "utf8");
const site = fs.readFileSync("static/js/site.js", "utf8");
const tabsSource = site.slice(site.indexOf("const checkoutMethods ="), site.indexOf("// Floating WhatsApp button."));

class Element extends EventTarget {
    constructor(name) {
        super();
        this.dataset = {checkoutMethod: name, checkoutPanel: name};
        this.attributes = {"aria-selected": name === "mada" ? "true" : "false"};
        this.hidden = name !== "mada";
        this.style = {};
        this.classList = {toggle() {}};
    }
    getAttribute(name) { return this.attributes[name.toLowerCase()] ?? null; }
    setAttribute(name, value) { this.attributes[name.toLowerCase()] = value; }
    click() { this.dispatchEvent(new Event("click")); }
    focus() { this.focused = true; }
}

function setup({loading = false, absent = false, dir = "ltr", native = false} = {}) {
    const tabs = ["mada", "card", "apple-pay"].map((name) => new Element(name));
    tabs[1].hidden = false;
    const panels = ["mada", "card", "apple-pay"].map((name) => new Element(name));
    const form = new Element("form");
    let button = null;
    let notify;
    let domReady;
    let observers = 0;
    panels[2].querySelector = (selector) => selector === "form.paymentWidgets" ? form : button;
    const document = {
        readyState: loading ? "loading" : "complete",
        documentElement: {dir},
        getElementById(id) {
            if (absent) return null;
            return ({"method-tab-mada": tabs[0], "method-tab-apple-pay": tabs[2],
                "method-panel-apple-pay": panels[2]})[id] ?? null;
        },
        querySelectorAll(selector) { return selector.includes("method") ? tabs : panels; },
        addEventListener(name, callback) { if (name === "DOMContentLoaded") domReady = callback; },
    };
    const window = {};
    if (native) window.ApplePaySession = {canMakePayments() {throw Error("Do not bypass COPYandPAY");}};
    const context = vm.createContext({document, window, MutationObserver: class {
        constructor(callback) { notify = callback; observers++; }
        observe(panel, options) {
            assert.equal(panel, panels[2]);
            assert.ok(options.attributeFilter.includes("data-applepaycapabilities"));
        }
    }});
    vm.runInContext(tabsSource, context);
    vm.runInContext(source, context);
    return {
        tabs, panels, form, window,
        domReady: () => domReady(),
        mutate: () => notify(),
        observers: () => observers,
        button(value = new Element("button")) { value.hidden = false; button = value; return value; },
        removeButton() { button = null; },
        status(value) { form.setAttribute("data-applepayCapabilities", JSON.stringify({paymentCredentialStatus: value})); },
    };
}

test("Windows: waits for SDK/capabilities and a real gateway button, not native ApplePaySession", () => {
    const env = setup();
    assert.equal(env.tabs[2].hidden, true);
    env.window.lsaApplePayCheckout.refresh();
    env.status("paymentCredentialStatusUnknown");
    env.mutate();
    assert.equal(env.tabs[2].hidden, true);
    env.button(); env.mutate();
    assert.equal(env.tabs[2].hidden, false);
    assert.equal(env.panels[2].hidden, true); // Does not auto-select or start payment.
    env.tabs[2].click();
    assert.equal(env.panels[2].hidden, false);
    assert.equal(env.panels[0].hidden, true);
    env.window.lsaApplePayCheckout.refresh();
    assert.equal(env.observers(), 1);
});

test("Safari and late DOM initialization follow the same provider availability result", () => {
    const env = setup({loading: true, native: true});
    env.status("paymentCredentialsAvailable"); env.button();
    assert.equal(env.tabs[2].hidden, true);
    env.domReady();
    assert.equal(env.tabs[2].hidden, false);
});

test("all gateway-supported capability statuses expose its button including card setup", () => {
    for (const status of ["paymentCredentialsAvailable", "paymentCredentialStatusUnknown", "paymentCredentialsUnavailable"]) {
        const env = setup(); env.status(status); env.button(); env.mutate();
        assert.equal(env.tabs[2].hidden, false);
    }
});

test("unsupported, invalid or missing capability response never advertises empty Apple Pay", () => {
    for (const value of [null, "invalid-json", JSON.stringify({paymentCredentialStatus: "applePayUnsupported"}), "{}"]) {
        const env = setup(); env.button();
        if (value) env.form.setAttribute("data-applepaycapabilities", value);
        env.mutate();
        assert.equal(env.tabs[2].hidden, true);
    }
    assert.doesNotThrow(() => setup({absent: true}));
});

test("removed, hidden or disabled button falls back to mada without destroying card widgets", () => {
    for (const change of [(env) => env.removeButton(), (env, button) => button.hidden = true,
        (env, button) => button.disabled = true, (env, button) => button.style.display = "none"]) {
        const env = setup(); env.status("paymentCredentialsAvailable");
        const button = env.button(); env.mutate(); env.tabs[2].click();
        change(env, button); env.mutate();
        assert.equal(env.tabs[2].hidden, true);
        assert.equal(env.panels[2].hidden, true);
        assert.equal(env.panels[0].hidden, false);
        assert.equal(env.tabs[0].getAttribute("aria-selected"), "true");
        env.tabs[1].click();
        assert.equal(env.panels[1].hidden, false);
    }
});

test("LTR/RTL keyboard skips unavailable wallet and includes wallet after asynchronous readiness", () => {
    for (const dir of ["ltr", "rtl"]) {
        const env = setup({dir});
        const press = (tab, key) => { const event = new Event("keydown", {cancelable: true}); event.key = key; tab.dispatchEvent(event); };
        press(env.tabs[0], "End");
        assert.equal(env.tabs[1].getAttribute("aria-selected"), "true");
        env.status("paymentCredentialStatusUnknown"); env.button(); env.mutate();
        press(env.tabs[1], dir === "rtl" ? "ArrowLeft" : "ArrowRight");
        assert.equal(env.tabs[2].getAttribute("aria-selected"), "true");
        press(env.tabs[2], "Home");
        assert.equal(env.tabs[0].getAttribute("aria-selected"), "true");
    }
});
