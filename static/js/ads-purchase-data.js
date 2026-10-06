"use strict";

// Only the Ads purchase tag reads this accessor. Never set global Google
// user_data, enqueue identity in dataLayer, or persist it in browser storage.
(function initializeAdsPurchaseData() {
    const element = document.querySelector("[data-ads-purchase-email-hash]");
    if (!element) {
        return;
    }
    let hash = element.dataset.adsPurchaseEmailHash;
    element.removeAttribute("data-ads-purchase-email-hash");
    const allowed = () => document.body.dataset.googleAdsEnabled === "true"
        && window.LSAConsent?.isUserProvidedDataAllowed?.() === true;
    const clear = () => {hash = undefined;};
    if (!allowed() || !/^[a-f0-9]{64}$/.test(hash || "")) {
        clear();
    }
    window.LSAAdsPurchaseData = {
        read() {
            if (!allowed()) {
                clear();
            }
            // Empty object explicitly overrides tag-wide identity when absent.
            return hash ? {sha256_email_address: hash} : {};
        },
    };
    document.addEventListener("lsa:consent-updated", () => {
        if (!allowed()) {
            clear();
        }
    });
    window.addEventListener("pagehide", clear);
}());
