"use strict";

// Measurement metadata only. Never rewrite the functional payment return URL,
// inspect form/card data, grant consent, or transmit an event here.
(function initializeMeasurementPrivacy() {
    const campaignKeys = new Set([
        "utm_source", "utm_medium", "utm_campaign", "utm_id", "utm_term", "utm_content",
        "gclid", "dclid", "gbraid", "wbraid", "gad_source", "gad_campaignid", "page",
    ]);

    function safeUrl(value) {
        if (!value) return "";
        try {
            const url = new URL(value, window.location.href);
            if (!["https:", "http:"].includes(url.protocol)) return "";
            const path = url.pathname;
            // Private booking/payment references can also be embedded in paths.
            const privatePath = path.match(
                /^\/(payments\/(?:hyperpay\/(?:result|booking|modification)|sandbox\/(?:booking|modification)|pay)|reservations|accounts|account|admin)(?:\/|$)/i,
            );
            if (privatePath) {
                url.pathname = `/${privatePath[1]}/`;
                url.search = "";
            } else {
                [...url.searchParams.keys()].forEach(key => {
                    if (!campaignKeys.has(key.toLowerCase())) url.searchParams.delete(key);
                });
            }
            url.hash = "";
            url.username = "";
            url.password = "";
            return url.href;
        } catch (_) {
            return "";
        }
    }

    function fields() {
        return {
            page_location: safeUrl(window.location.href),
            page_referrer: safeUrl(document.referrer),
        };
    }

    function apply() {
        window.gtag("set", fields());
    }

    window.dataLayer = window.dataLayer || [];
    window.gtag = window.gtag || function gtag() { window.dataLayer.push(arguments); };
    window.LSAMeasurementPrivacy = {safeUrl, fields, apply};
    // Queue before any destination loads or sends its automatic page_view.
    apply();
}());
