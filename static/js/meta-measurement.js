"use strict";

(function initializeMetaMeasurement() {
    // This bridge sends nothing until the consent-gated GTM template calls it.
    // It deliberately does not observe forms, wrap dataLayer.push or acknowledge purchases.
    if (window.LSAMeta) {
        return;
    }
    const PIXEL_ID = "904593259205579";
    const body = document.body;
    const approvedQueryKeys = new Set([
        "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
        "fbclid", "gclid", "gbraid", "wbraid",
    ]);
    let loading = false;
    let ready = false;
    let sdkLoaded = false;
    let pageViewQueued = false;
    let pageViewSent = false;
    let pending = [];
    const sentViews = new Set();

    function safePublicUrl(value) {
        if (!value) {
            return true;
        }
        try {
            const url = new URL(value, window.location.href);
            if (!/^https?:$/.test(url.protocol) || url.hash
                || /\/(payments|reservations|booking|accounts|admin|health|integrations)(\/|$)/i.test(url.pathname)) {
                return false;
            }
            for (const [key, val] of url.searchParams) {
                if (!approvedQueryKeys.has(key) || val.length > 256 || /@|return_token|resourcePath/i.test(val)) {
                    return false;
                }
            }
            return true;
        } catch {
            return false;
        }
    }

    function allowed() {
        const choice = window.LSAConsent?.parseConsent();
        return body.dataset.metaMeasurementPageAllowed === "true"
            && body.dataset.cookieConsentEnabled === "true"
            && body.dataset.gtmEnabled === "true"
            && body.dataset.googleIntegrationsEnabled === "true"
            && choice?.marketing === true && choice?.metaMeasurement === true
            && safePublicUrl(window.location.href) && safePublicUrl(document.referrer);
    }

    function send(event) {
        if (!allowed() || !ready) {
            return;
        }
        window.fbq("consent", "grant");
        if (event.name === "PageView") {
            if (pageViewSent) {
                return;
            }
            window.fbq("trackSingle", PIXEL_ID, "PageView");
            pageViewSent = true;
        } else if (!sentViews.has(event.params.content_ids[0])) {
            window.fbq("trackSingle", PIXEL_ID, "ViewContent", event.params);
            sentViews.add(event.params.content_ids[0]);
        }
    }

    function initializeSdk() {
        if (!allowed() || !sdkLoaded || typeof window.fbq.callMethod !== "function") {
            pending = [];
            pageViewQueued = false;
            return;
        }
        window.fbq("set", "autoConfig", false, PIXEL_ID);
        window.fbq("init", PIXEL_ID);
        ready = true;
        const events = pending;
        pending = [];
        events.forEach(send);
    }

    function loadSdk() {
        if (!allowed() || loading || ready) {
            return;
        }
        if (sdkLoaded) {
            initializeSdk();
            return;
        }
        // An independently installed SDK must be audited, not silently combined.
        if (window.fbq) {
            pending = [];
            pageViewQueued = false;
            return;
        }
        const fbq = function () {
            if (fbq.callMethod) {
                fbq.callMethod.apply(fbq, arguments);
            } else {
                fbq.queue.push(arguments);
            }
        };
        fbq.push = fbq;
        fbq.loaded = true;
        fbq.version = "2.0";
        fbq.queue = [];
        fbq.disablePushState = true;
        window.fbq = fbq;
        window._fbq = fbq;
        loading = true;
        const script = document.createElement("script");
        script.async = true;
        script.dataset.lsaMetaSdk = "true";
        script.src = "https://connect.facebook.net/en_US/fbevents.js";
        script.onload = () => {
            loading = false;
            sdkLoaded = true;
            // No initialization or events if consent was withdrawn while loading.
            initializeSdk();
        };
        script.onerror = () => {
            loading = false;
            pending = [];
            pageViewQueued = false;
            // Do not retry from this page, and never affect the Google purchase path.
        };
        document.head.append(script);
    }

    function track(eventName, payload = {}) {
        if (!allowed()) {
            return true; // Deliberate suppression, not a GTM processing failure.
        }
        let event;
        if (eventName === "gtm.init" || eventName === "lsa_meta_consent") {
            if (pageViewQueued || pageViewSent) {
                return true;
            }
            pageViewQueued = true;
            event = {name: "PageView"};
        } else if (eventName === "view_item") {
            const id = payload.property_id;
            const name = payload.property_name;
            if (typeof id !== "string" || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(id)
                || typeof name !== "string" || !name.trim() || /@|https?:\/\//i.test(name)) {
                return true;
            }
            event = {name: "ViewContent", params: {
                content_ids: [id.slice(0, 150)],
                content_name: name.slice(0, 200),
                content_type: "product",
                ...(typeof payload.page_language === "string"
                    && /^[a-z]{2,3}$/.test(payload.page_language)
                    ? {page_language: payload.page_language} : {}),
            }};
        } else {
            return true; // Purchase/checkout/contact are intentionally not mapped here.
        }
        if (ready) {
            send(event);
        } else {
            if (pending.length < 20) {
                pending.push(event);
            }
            loadSdk();
        }
        return true;
    }

    document.addEventListener("lsa:consent-updated", () => {
        if (!allowed()) {
            pending = [];
            pageViewQueued = false;
            if (ready) {
                window.fbq("consent", "revoke");
            }
        }
        // The GTM trigger handles a fresh approval, including marketing-only choices.
        window.dataLayer = window.dataLayer || [];
        window.dataLayer.push({event: "lsa_meta_consent"});
    });
    window.LSAMeta = {track};
}());
