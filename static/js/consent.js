"use strict";

(function initializeConsent() {
    const body = document.body;
    const consentEnabled = body.dataset.cookieConsentEnabled === "true";
    const consentModeEnabled = body.dataset.consentModeEnabled === "true";
    const googleEnabled = body.dataset.googleIntegrationsEnabled === "true";
    const version = Number(body.dataset.cookieConsentVersion || "1");
    const maxAge = Number(body.dataset.cookieConsentMaxAge || "15552000");
    const secure = body.dataset.cookieConsentSecure === "true";
    const cookieName = "lsa_cookie_consent";

    window.dataLayer = window.dataLayer || [];
    window.gtag = window.gtag || function gtag() {
        window.dataLayer.push(arguments);
    };

    const defaultConsent = {
        analytics_storage: body.dataset.consentDefaultAnalytics || "denied",
        ad_storage: body.dataset.consentDefaultAd || "denied",
        ad_user_data: body.dataset.consentDefaultAdUserData || "denied",
        ad_personalization: body.dataset.consentDefaultAdPersonalization || "denied",
        wait_for_update: 500,
    };
    if (consentModeEnabled) {
        window.gtag("consent", "default", defaultConsent);
    }

    function parseConsent() {
        const prefix = `${cookieName}=`;
        const part = document.cookie.split(";").map((item) => item.trim()).find(
            (item) => item.startsWith(prefix),
        );
        if (!part) {
            return null;
        }
        try {
            const parsed = JSON.parse(decodeURIComponent(part.slice(prefix.length)));
            if (
                parsed.version !== version
                || typeof parsed.analytics !== "boolean"
                || typeof parsed.marketing !== "boolean"
            ) {
                return null;
            }
            return parsed;
        } catch {
            return null;
        }
    }

    function writeConsent(choice) {
        const value = {
            version,
            analytics: Boolean(choice.analytics),
            marketing: Boolean(choice.marketing),
            timestamp: new Date().toISOString(),
        };
        const attributes = [
            `${cookieName}=${encodeURIComponent(JSON.stringify(value))}`,
            "path=/",
            `max-age=${maxAge}`,
            "samesite=lax",
        ];
        if (secure) {
            attributes.push("secure");
        }
        document.cookie = attributes.join("; ");
        return value;
    }

    function loadGoogle(choice) {
        if (!googleEnabled) {
            return;
        }
        const gtmEnabled = body.dataset.gtmEnabled === "true";
        const ga4Enabled = body.dataset.ga4Enabled === "true";
        if (!choice.analytics && !choice.marketing) {
            return;
        }
        if (gtmEnabled && /^GTM-[A-Z0-9]{4,}$/.test(body.dataset.gtmContainerId || "")) {
            const id = body.dataset.gtmContainerId;
            if (!document.querySelector("script[data-lsa-google-script='gtm']")) {
                window.dataLayer.push({"gtm.start": Date.now(), event: "gtm.js"});
                const script = document.createElement("script");
                script.async = true;
                script.dataset.lsaGoogleScript = "gtm";
                script.src = `https://www.googletagmanager.com/gtm.js?id=${encodeURIComponent(id)}`;
                document.head.append(script);
            }
            return;
        }
        if (
            choice.analytics
            && ga4Enabled
            && /^G-[A-Z0-9]{4,}$/.test(body.dataset.ga4MeasurementId || "")
            && !document.querySelector("script[data-lsa-google-script='ga4']")
        ) {
            const id = body.dataset.ga4MeasurementId;
            const script = document.createElement("script");
            script.async = true;
            script.dataset.lsaGoogleScript = "ga4";
            script.src = `https://www.googletagmanager.com/gtag/js?id=${encodeURIComponent(id)}`;
            document.head.append(script);
            window.gtag("js", new Date());
            window.gtag("config", id, {debug_mode: body.dataset.ga4Debug === "true"});
        }
    }

    function applyConsent(choice, emitUpdate) {
        if (consentModeEnabled) {
            window.gtag("consent", "update", {
                analytics_storage: choice.analytics ? "granted" : "denied",
                ad_storage: choice.marketing ? "granted" : "denied",
                ad_user_data: choice.marketing ? "granted" : "denied",
                ad_personalization: choice.marketing ? "granted" : "denied",
            });
        }
        loadGoogle(choice);
        if (emitUpdate) {
            document.dispatchEvent(new CustomEvent("lsa:consent-updated", {detail: choice}));
        }
    }

    const banner = document.querySelector("[data-consent-banner]");
    const dialog = document.querySelector("[data-consent-dialog]");
    const analyticsInput = dialog?.querySelector("[data-consent-analytics]");
    const marketingInput = dialog?.querySelector("[data-consent-marketing]");
    let returnFocus = null;

    function openSettings() {
        if (!(dialog instanceof HTMLDialogElement)) {
            return;
        }
        const current = parseConsent() || {analytics: false, marketing: false};
        analyticsInput.checked = current.analytics;
        marketingInput.checked = current.marketing;
        returnFocus = document.activeElement;
        dialog.showModal();
        analyticsInput.focus();
    }

    function closeSettings() {
        if (dialog instanceof HTMLDialogElement && dialog.open) {
            dialog.close();
            returnFocus?.focus();
        }
    }

    function choose(analytics, marketing) {
        const choice = writeConsent({analytics, marketing});
        applyConsent(choice, true);
        if (banner) {
            banner.hidden = true;
        }
        closeSettings();
    }

    document.querySelectorAll("[data-consent-settings]").forEach((button) => {
        button.addEventListener("click", openSettings);
    });
    banner?.querySelector("[data-consent-accept]")?.addEventListener(
        "click",
        () => choose(true, true),
    );
    banner?.querySelector("[data-consent-reject]")?.addEventListener(
        "click",
        () => choose(false, false),
    );
    banner?.querySelector("[data-consent-customize]")?.addEventListener(
        "click",
        openSettings,
    );
    dialog?.querySelector("[data-consent-close]")?.addEventListener("click", closeSettings);
    dialog?.querySelector("[data-consent-save]")?.addEventListener(
        "click",
        () => choose(analyticsInput.checked, marketingInput.checked),
    );
    dialog?.addEventListener("cancel", (event) => {
        event.preventDefault();
        closeSettings();
    });
    dialog?.addEventListener("keydown", (event) => {
        if (event.key !== "Tab") {
            return;
        }
        const controls = [...dialog.querySelectorAll("button,input:not([disabled])")];
        const first = controls[0];
        const last = controls[controls.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    });

    const stored = parseConsent();
    if (stored) {
        applyConsent(stored, false);
    } else if (consentEnabled && banner) {
        banner.hidden = false;
    }
    window.LSAConsent = {applyConsent, parseConsent, openSettings};
}());
