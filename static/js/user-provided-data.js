"use strict";

// Independent, consented GA4 UPD bridge. Never alters purchase or its receipt/deduplication.
(function initializeProvidedData() {
    const form = document.querySelector("[data-upd-guest-form]");
    const body = document.body;
    if (!form || body.dataset.googleIntegrationsEnabled !== "true"
        || body.dataset.gtmEnabled !== "true" || body.dataset.ga4Enabled !== "true") {
        return;
    }
    // Scope deliberately excludes the footer, contact forms and booking lookup.
    const input = form.querySelector('input[name="guest_email"]');
    if (!input || !window.crypto?.subtle || typeof TextEncoder === "undefined") {
        return;
    }
    let revision = 0;
    let currentHash = null;
    let currentValue = null;
    let lastSentHash = null;

    function allowed() {
        return window.LSAConsent?.isUserProvidedDataAllowed?.() === true;
    }

    function clear() {
        revision += 1;
        currentHash = null;
        currentValue = null;
    }

    window.LSAUserProvidedData = {
        read() {
            if (!allowed() || !currentHash || !input.checkValidity()
                || input.value.trim().toLowerCase() !== currentValue) {
                return undefined;
            }
            return {sha256_email_address: currentHash};
        },
    };

    async function collect() {
        clear();
        if (!allowed() || !input.checkValidity()) {
            return;
        }
        const normalized = input.value.trim().toLowerCase();
        if (!normalized || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(normalized)) {
            return;
        }
        const attempt = revision;
        try {
            const digest = await window.crypto.subtle.digest(
                "SHA-256", new TextEncoder().encode(normalized),
            );
            // Re-check consent and input after asynchronous work, including revocation.
            if (attempt !== revision || !allowed()
                || input.value.trim().toLowerCase() !== normalized || !input.checkValidity()) {
                return;
            }
            currentHash = [...new Uint8Array(digest)]
                .map((byte) => byte.toString(16).padStart(2, "0")).join("");
            currentValue = normalized;
            if (currentHash === lastSentHash) {
                return;
            }
            // Identity is ONLY exposed through the dedicated GTM UPD variable.
            // Do not put raw identity, hashes or ecommerce/purchase fields in dataLayer.
            window.dataLayer = window.dataLayer || [];
            window.dataLayer.push({event: "lsa_user_data_provided"});
            lastSentHash = currentHash;
        } catch {
            // Optional measurement must never block the guest journey or payment.
            clear();
        }
    }

    input.addEventListener("input", clear);
    input.addEventListener("blur", collect);
    input.addEventListener("change", collect);
    form.addEventListener("submit", collect); // No await, preventDefault or redirect.
    document.addEventListener("lsa:consent-updated", () => {
        if (!allowed()) {
            clear();
            return;
        }
        collect();
    });
    window.addEventListener("pagehide", clear);
    collect();
}());
