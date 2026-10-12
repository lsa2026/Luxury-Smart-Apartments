/* COPYandPAY owns Apple Pay availability, merchant validation and authorization.
 * We only expose its independently mounted wallet when it has rendered a button.
 * No second SDK load, capability request, payment session or user-agent sniffing.
 */
(() => {
    "use strict";

    const supportedStatuses = new Set([
        "paymentCredentialsAvailable",
        "paymentCredentialStatusUnknown",
        "paymentCredentialsUnavailable",
    ]);
    let observedPanel = null;

    function refresh() {
        const tab = document.getElementById("method-tab-apple-pay");
        const panel = document.getElementById("method-panel-apple-pay");
        if (!tab || !panel) return;

        if (observedPanel !== panel) {
            observedPanel = panel;
            new MutationObserver(refresh).observe(panel, {
                childList: true,
                subtree: true,
                attributes: true,
                attributeFilter: ["data-applepaycapabilities", "hidden", "style", "disabled"],
            });
        }

        // COPYandPAY replaces the original .paymentWidgets form. Read the
        // capability marker on its rendered form, not the removed placeholder.
        const form = panel.querySelector("[data-applepaycapabilities]");
        const button = panel.querySelector("apple-pay-button, .wpwl-apple-pay-button");
        let status;
        try {
            status = JSON.parse(form?.getAttribute("data-applepaycapabilities") || "null")
                ?.paymentCredentialStatus;
        } catch (_) {
            // A failed/unsupported SDK never leaves an empty payment method.
        }
        const ready = supportedStatuses.has(status) && Boolean(button)
            && !button.hidden && !button.disabled && button.style.display !== "none";
        if (tab.hidden === ready) tab.hidden = !ready;

        if (!ready && tab.getAttribute("aria-selected") === "true") {
            document.getElementById("method-tab-mada")?.click();
            // Also hide the panel if the main tab handler hasn't initialized yet.
            panel.hidden = true;
        }
    }

    window.lsaApplePayCheckout = { refresh };
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", refresh, { once: true });
    } else {
        refresh();
    }
})();
