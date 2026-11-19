"use strict";

// First-party consent update only. No browser/click ID, URL or guest field is posted.
(function initializeAssistedAttribution() {
    const body = document.body;
    if (body.dataset.analyticsStaffBrowser === "true") return;
    const endpoint = body.dataset.attributionConsentUrl;
    if (!endpoint) return;
    const links = () => document.querySelectorAll('[data-analytics-event="whatsapp_click"]');
    const original = body.dataset.attributionWhatsappOriginal;
    let sequence = 0;
    function csrf() {
        const value = document.cookie.split(";").map(x => x.trim()).find(x => x.startsWith("csrftoken="));
        return value ? decodeURIComponent(value.slice(10)) : document.querySelector('[name="csrfmiddlewaretoken"]')?.value || "";
    }
    document.addEventListener("lsa:consent-updated", async () => {
        const current = ++sequence;
        const allowed = window.LSAConsent?.parseConsent()?.analytics === true;
        if (!allowed && original) links().forEach(link => { link.href = original; });
        if (allowed && body.dataset.attributionPublicPage !== "true") return;
        const values = new URLSearchParams({page_kind: body.dataset.attributionPageKind || "unknown"});
        if (allowed) {
            const params = new URLSearchParams(window.location.search);
            for (const key of ["utm_source", "utm_medium", "utm_id", "gad_campaignid"]) {
                if (params.has(key)) values.set(key, params.get(key).slice(0, 100));
            }
            values.set("click_id_seen", ["gclid", "gbraid", "wbraid"].some(key => params.has(key)) ? "1" : "0");
            values.set("property_id", body.dataset.attributionPropertyId || "");
        }
        try {
            const response = await window.fetch(endpoint, {
                method: "POST", credentials: "same-origin", headers: {"X-CSRFToken": csrf()},
                body: values, cache: "no-store",
            });
            if (!response.ok) return;
            const result = await response.json();
            if (current !== sequence || window.LSAConsent?.parseConsent()?.analytics !== true) return;
            if (result.state === "captured" && result.whatsapp_url) {
                const url = new URL(result.whatsapp_url, window.location.origin);
                if (url.origin === window.location.origin && url.pathname === "/contact/whatsapp/") {
                    links().forEach(link => { link.href = url.href; });
                }
            }
        } catch (_) { /* The configured WhatsApp link remains usable without attribution. */ }
    });
}());
