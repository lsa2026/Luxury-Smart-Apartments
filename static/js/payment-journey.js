/* Observe the merchant checkout, never PCI fields, Apple tokens or bank screens.
 * Analytics failure must never change the gateway's payment behavior.
 */
(() => {
    "use strict";
    let initialized = false;
    let widgetReady = false;
    let viewSent = false;
    let readySent = false;
    let lastSelection = null;
    let userSelection = null;
    const methods = {mada: ["card", "mada"], card: ["card", "unknown"],
        "apple-pay": ["apple_pay", "apple_pay"]};

    function root() {
        return document.querySelector("[data-payment-journey]");
    }
    function context() {
        const source = root();
        if (!source) return null;
        const data = source.dataset;
        const active = source.querySelector('[data-checkout-method][aria-selected="true"]');
        const method = methods[active?.dataset.checkoutMethod] || ["unknown", "unknown"];
        return {
            property_id: data.analyticsPropertyId,
            property_name: data.analyticsPropertyName,
            page_language: String(data.analyticsPageLanguage || "").split("-")[0],
            checkout_kind: data.analyticsCheckoutKind,
            value: Number(data.analyticsValue),
            currency: data.analyticsCurrency,
            items: [{item_id: data.analyticsPropertyId, item_name: data.analyticsPropertyName,
                quantity: 1}],
            payment_type: method[0],
            payment_brand: method[1],
        };
    }
    function emit(name, extra = {}) {
        try {
            const payload = context();
            return payload && window.LSAAnalytics?.pushEvent(name, {...payload, ...extra});
        } catch (_) {
            return false;
        }
    }
    function currentView() {
        if (!initialized) return;
        if (!viewSent) viewSent = Boolean(emit("secure_payment_view"));
        if (widgetReady && !readySent) readySent = Boolean(emit("payment_widget_ready"));
    }
    function initialize() {
        const source = root();
        if (!source || initialized) return;
        initialized = true;
        currentView();
        source.addEventListener("click", event => {
            if (!event.isTrusted) return;
            userSelection = event.target?.closest?.("[data-checkout-method]")?.dataset.checkoutMethod || null;
        }, true);
        source.addEventListener("keydown", event => {
            if (event.isTrusted && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) {
                userSelection = "keyboard";
            }
        }, true);
        // Attribute observation also covers arrow-key tab selection. The
        // initial default and the Apple availability fallback aren't choices.
        source.querySelectorAll("[data-checkout-method]").forEach(tab => {
            new MutationObserver(() => {
                if (tab.hidden || tab.getAttribute("aria-selected") !== "true") return;
                const method = tab.dataset.checkoutMethod;
                if (userSelection !== method && userSelection !== "keyboard") return;
                userSelection = null;
                if (method === lastSelection) return;
                lastSelection = method;
                emit("payment_method_selected");
            }).observe(tab, {attributes: true, attributeFilter: ["aria-selected"]});
        });
        source.addEventListener("click", event => {
            if (!event.isTrusted) return;
            const target = event.target?.closest?.(".wpwl-button-pay, apple-pay-button, .wpwl-apple-pay-button");
            if (target && !target.disabled) emit("payment_submit_clicked");
        }, true);
    }
    window.LSAPaymentJourney = {
        ready() { widgetReady = true; currentView(); },
        submitted() {
            // COPYandPAY onAfterSubmit signals submission, not approval.
            emit("add_payment_info");
        },
        bankVerification() { emit("payment_verification_started"); },
        error(error) {
            const categories = {InvalidCheckoutIdError: "checkout_expired",
                PciIframeSubmitError: "gateway_communication",
                PciIframeCommunicationError: "gateway_communication"};
            const category = error?.event === "closed" ? "wallet_closed"
                : error?.event === "onValidateMerchant-fail" ? "merchant_validation"
                    : categories[error?.name] || "widget_error";
            // Never forward raw errors/messages, which can contain IDs/tokens.
            emit("payment_widget_error", {error_category: category});
        },
    };
    document.addEventListener("lsa:consent-updated", currentView);
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initialize, {once: true});
    } else {
        initialize();
    }
})();
