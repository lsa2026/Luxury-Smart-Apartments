"use strict";

(function initializePublicClarity() {
    // Loaded only by the dedicated GTM tag, never by base.html.
    if (window.__lsaClarityPublicInstalled) return;
    window.__lsaClarityPublicInstalled = true;
    let started = false;
    let revoked = false;
    const publicPath = /^\/(?:ar\/|en\/|fr\/)?(?:$|about\/$|faq\/$|properties\/(?:[a-z0-9-]+\/)?$|legal\/(?:terms|house-rules|privacy|cancellation|cookies)\/$)/;
    function state(value) {
        document.documentElement.setAttribute("data-lsa-clarity-state", value);
    }
    function safeUrl(raw, referral) {
        if (!raw && referral) return true;
        try {
            const url = new URL(raw);
            if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash) return false;
            if (/(^|\.)(oppwa\.com|hyperpay\.com|hyperbill\.com)$/.test(url.hostname)) return false;
            if (["luxurysmartapartments.com", "www.luxurysmartapartments.com"].includes(url.hostname)) {
                return publicPath.test(url.pathname);
            }
            return referral && url.pathname === "/";
        } catch {
            return false;
        }
    }
    function allowed() {
        const choice = window.LSAConsent?.parseConsent();
        if (choice?.analytics !== true || choice?.clarityMeasurement !== true) {
            state("no-clarity-consent");
            return false;
        }
        if (!safeUrl(window.location.href, false)) {
            state("excluded-page");
            return false;
        }
        if (!safeUrl(document.referrer, true)) {
            state("excluded-referrer");
            return false;
        }
        return true;
    }
    function stop() {
        if (!started || revoked) return;
        revoked = true;
        state("stop-requested");
        window.clarity("consentv2", {ad_Storage: "denied", analytics_Storage: "denied"});
        window.clarity("stop");
    }
    function sync() {
        if (!allowed()) {
            stop();
            return;
        }
        if (started || revoked) return;
        if (window.clarity) {
            state("existing-installation");
            return;
        }
        started = true;
        document.documentElement.setAttribute("data-clarity-mask", "true");
        document.querySelectorAll("[data-clarity-unmask]").forEach((node) => {
            node.removeAttribute("data-clarity-unmask");
        });
        window.clarity = function clarity() {
            (window.clarity.q = window.clarity.q || []).push(arguments);
        };
        window.clarity("consentv2", {ad_Storage: "denied", analytics_Storage: "granted"});
        const script = document.createElement("script");
        script.async = true;
        script.referrerPolicy = "no-referrer";
        script.dataset.lsaClarity = "public-consented";
        script.src = "https://www.clarity.ms/tag/yz1e54we6c";
        document.head.appendChild(script);
        state("script-requested");
    }
    document.addEventListener("lsa:consent-updated", sync);
    document.addEventListener("DOMContentLoaded", sync);
    window.addEventListener("load", sync);
    window.addEventListener("popstate", sync);
    window.addEventListener("hashchange", sync);
    sync();
}());
