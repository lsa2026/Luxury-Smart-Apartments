const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const code = fs.readFileSync('static/js/consent.js', 'utf8');
function run(stored) {
    const control = () => ({checked: false, listeners: {},
        addEventListener(n, fn) {this.listeners[n] = fn;}, focus() {}});
    const analytics = control(), marketing = control(), upd = control(), checkoutUpd = control();
    const accept = control(), reject = control(), save = control();
    class Dialog {
        open = false;
        querySelector(s) {return {'[data-consent-analytics]': analytics,
            '[data-consent-marketing]': marketing, '[data-consent-upd]': upd,
            '[data-consent-save]': save}[s] || null;}
        querySelectorAll() {return [];}
        addEventListener() {} showModal() {this.open = true;} close() {this.open = false;}
    }
    const dialog = new Dialog();
    const banner = {hidden: true, querySelector(s) {return {'[data-consent-accept]': accept,
        '[data-consent-reject]': reject}[s] || null;}};
    let cookie = stored ? 'lsa_cookie_consent=' + encodeURIComponent(JSON.stringify(stored)) : '';
    const document = {body: {dataset: {cookieConsentEnabled: 'true', consentModeEnabled: 'true',
        googleIntegrationsEnabled: 'true', cookieConsentVersion: '1'}},
        get cookie() {return cookie;}, set cookie(v) {cookie = v.split(';')[0];},
        querySelector(s) {return s === '[data-consent-banner]' ? banner : dialog;},
        querySelectorAll(s) {return s === '[data-consent-upd]' ? [upd, checkoutUpd] : [];},
        dispatchEvent() {}};
    const window = {dataLayer: []};
    vm.runInNewContext(code, {window, document, HTMLDialogElement: Dialog,
        CustomEvent: class {constructor(n, v) {this.type = n; this.detail = v.detail;}}});
    return {window, analytics, marketing, upd, checkoutUpd, accept, reject, save,
        choice: () => window.LSAConsent.parseConsent()};
}
test('legacy choices and Accept all cannot silently grant UPD', () => {
    const r = run({version: 1, analytics: true, marketing: true});
    assert.equal(r.choice().userProvidedData, false);
    r.accept.listeners.click(); assert.equal(r.choice().userProvidedData, false);
    assert.equal(r.window.LSAConsent.isUserProvidedDataAllowed(), false);
});
test('explicit UPD selection without analytics/marketing does not grant cookie consent', () => {
    const r = run(); r.checkoutUpd.checked = true; r.checkoutUpd.listeners.change();
    assert.equal(r.choice().userProvidedData, true);
    assert.equal(r.choice().analytics, false); assert.equal(r.choice().marketing, false);
    assert.equal(r.window.LSAConsent.isUserProvidedDataAllowed(), false);
});
test('all three opt-ins are required and Reject clears the separate choice', () => {
    const r = run(); r.analytics.checked = true; r.marketing.checked = true;
    r.upd.checked = true; r.save.listeners.click();
    assert.equal(r.window.LSAConsent.isUserProvidedDataAllowed(), true);
    assert.equal(r.checkoutUpd.checked, true);
    r.reject.listeners.click();
    assert.equal(r.window.LSAConsent.isUserProvidedDataAllowed(), false);
    assert.equal(r.choice().userProvidedData, false);
});
test('malformed and unversioned independent permissions fail closed', () => {
    for (const data of [{userProvidedData: true},
        {userProvidedData: 'true', userProvidedDataVersion: 1},
        {userProvidedData: true, userProvidedDataVersion: 2}]) {
        const r = run({version: 1, analytics: true, marketing: true, ...data});
        assert.equal(r.window.LSAConsent.isUserProvidedDataAllowed(), false);
    }
});
test('new permission changes do not modify purchase loader or cookie consent mode mapping', () => {
    const r = run(); r.analytics.checked = true; r.marketing.checked = true;
    r.save.listeners.click();
    const update = r.window.dataLayer.filter(v => v[0] === 'consent' && v[1] === 'update').at(-1)[2];
    assert.equal(update.analytics_storage, 'granted'); assert.equal(update.ad_user_data, 'granted');
    assert.equal(typeof r.window.LSAConsent.loadPurchaseTracker, 'function');
    assert.equal(r.window.LSAConsent.loadPurchaseTracker(), false);
    assert.equal(r.choice().userProvidedData, false);
});
