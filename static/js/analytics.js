"use strict";

(function initializeAnalytics() {
    const body = document.body;
    const enabled = body.dataset.googleIntegrationsEnabled === "true";
    const debug = body.dataset.analyticsDebug === "true";
    const forbiddenKeys = new Set([
        "email", "phone", "first_name", "last_name", "guest_name", "address",
        "special_requests", "session_key", "session_key_hash", "hostaway_id",
        "hostaway_listing_id", "hostaway_listing_map_id", "reservation_id", "authorization",
    ]);
    const itemKeys = new Set([
        "item_id", "item_name", "item_brand", "item_category", "item_category2",
        "item_category3", "index", "quantity", "currency", "price",
    ]);
    const propertyContextKeys = ["property_id", "property_name", "page_language"];
    const schemas = {
        view_item_list: new Set(["item_list_name", "items", "page_language"]),
        select_item: new Set(["item_list_name", "items", ...propertyContextKeys]),
        view_item: new Set(["currency", "value", "items", ...propertyContextKeys]),
        view_all_reviews: new Set([
            "language", "review_count", "items", ...propertyContextKeys,
        ]),
        begin_checkout: new Set([
            "currency", "value", "items", "nights", "guests", ...propertyContextKeys,
        ]),
        generate_lead: new Set(["lead_source", "page_language"]),
        check_availability: new Set([
            "city", "nights", "guests", "items", ...propertyContextKeys,
        ]),
        availability_result: new Set([
            "available", "reason_code", "city", "nights", "items", ...propertyContextKeys,
        ]),
        quote_created: new Set([
            "currency", "value", "nights", "guests", "items", ...propertyContextKeys,
        ]),
        quote_expired: new Set(["reason_code", "items", ...propertyContextKeys]),
        booking_intent_created: new Set([
            "currency", "value", "items", ...propertyContextKeys,
        ]),
        modification_request_created: new Set(["request_type"]),
        cancellation_request_created: new Set(["request_type"]),
        contact_form_submitted: new Set(["lead_source", "page_language"]),
        whatsapp_click: new Set([
            "lead_source", "language", "contact_placement", ...propertyContextKeys,
        ]),
        phone_click: new Set([
            "lead_source", "language", "contact_placement", ...propertyContextKeys,
        ]),
        language_changed: new Set(["language"]),
        cookie_consent_updated: new Set(["analytics", "marketing", "version"]),
        purchase: new Set([
            "transaction_id", "value", "currency", "items", "tax", "coupon",
            ...propertyContextKeys,
        ]),
        refund: new Set(["transaction_id", "value", "currency"]),
    };

    function cleanItem(item) {
        const clean = {};
        Object.entries(item || {}).forEach(([key, value]) => {
            if (!itemKeys.has(key) || forbiddenKeys.has(key)) {
                return;
            }
            if (key === "item_id" && !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(String(value))) {
                return;
            }
            if (["string", "number", "boolean"].includes(typeof value)) {
                clean[key] = typeof value === "string" ? value.slice(0, 200) : value;
            }
        });
        return clean;
    }

    function sanitize(eventName, payload) {
        const schema = schemas[eventName];
        if (!schema || !payload || typeof payload !== "object") {
            return null;
        }
        if (Object.keys(payload).some((key) => forbiddenKeys.has(key.toLowerCase()))) {
            return null;
        }
        if (eventName === "purchase" && (
            typeof payload.transaction_id !== "string" || !payload.transaction_id.trim()
            || !Number.isFinite(payload.value) || payload.value <= 0
            || !/^[A-Z]{3}$/.test(payload.currency || "")
        )) {
            return null;
        }
        const clean = {};
        Object.entries(payload).forEach(([key, value]) => {
            if (!schema.has(key)) {
                return;
            }
            if (key === "property_id" && !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(String(value))) {
                return;
            }
            if (key === "page_language" && !/^[a-z]{2,3}$/.test(String(value))) {
                return;
            }
            if (key === "items" && Array.isArray(value)) {
                clean.items = value.slice(0, 20).map(cleanItem).filter(
                    (item) => Object.keys(item).length,
                );
            } else if (["string", "number", "boolean"].includes(typeof value)
                && (typeof value !== "number" || Number.isFinite(value))) {
                clean[key] = typeof value === "string" ? value.slice(0, 200) : value;
            }
        });
        return clean;
    }

    function pushEvent(eventName, payload, onProcessed) {
        const clean = sanitize(eventName, payload);
        if (!clean) {
            return false;
        }
        if (debug) {
            console.info("LSA analytics event", eventName, Object.keys(clean));
        }
        if (!enabled) {
            return false;
        }
        const consent = window.LSAConsent?.parseConsent();
        const anonymousPurchase = eventName === "purchase"
            && window.LSAConsent?.loadPurchaseTracker?.();
        if (!consent?.analytics && !anonymousPurchase) {
            return false;
        }
        window.dataLayer = window.dataLayer || [];
        // Clear on EVERY event: a contact click must not inherit an earlier
        // apartment, checkout amount or transaction from GTM's merged model.
        window.dataLayer.push({ecommerce: null});
        const ecommerce = {};
        const ecommerceKeys = eventName === "purchase"
            ? ["transaction_id", "value", "currency", "items", "tax", "coupon"]
            : ["item_list_name", "value", "currency", "items"];
        ecommerceKeys.forEach((key) => {
            if (Object.hasOwn(clean, key)) {
                ecommerce[key] = clean[key];
            }
        });
        const event = {event: eventName, ...clean,
            ecommerce: Object.keys(ecommerce).length ? ecommerce : null};
        if (eventName === "purchase") {
            if (typeof onProcessed === "function") {
                event.eventCallback = (containerId) => {
                    if (/^GTM-[A-Z0-9]{4,}$/.test(containerId || "")
                        && containerId === body.dataset.gtmContainerId) {
                        onProcessed();
                    }
                };
                // No eventTimeout: a timeout must never close a purchase receipt.
                // This callback confirms GTM processing, NOT Google Ads attribution
                // or server-side receipt of the conversion.
            }
        }
        window.dataLayer.push(event);
        return clean;
    }

    let acknowledgementInFlight = false;
    let acknowledgementAttempts = 0;
    let acknowledgementTimer = null;
    function acknowledgePurchaseReceipt(element, transactionId) {
        const token = element.dataset.analyticsReceiptToken;
        const url = element.dataset.analyticsReceiptUrl;
        if (!token || !url || !transactionId || acknowledgementInFlight
            || acknowledgementAttempts >= 3) {
            return;
        }
        const storageKey = `lsa:purchase:v2:${transactionId}`;
        try {
            if (window.sessionStorage.getItem(storageKey) === "acknowledged") {
                return;
            }
        } catch (error) {
            // Storage can be unavailable in privacy modes; the server receipt remains the fallback.
        }
        acknowledgementInFlight = true;
        acknowledgementAttempts += 1;
        const controller = new AbortController();
        const timeout = window.setTimeout(() => controller.abort(), 8000);
        fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "X-CSRFToken": element.dataset.analyticsReceiptCsrf || "",
            },
            body: new URLSearchParams({receipt_token: token}),
            signal: controller.signal,
            keepalive: true,
        }).then((response) => {
            if (!response.ok) {
                throw new Error("Purchase acknowledgement failed");
            }
            try {
                window.sessionStorage.setItem(storageKey, "acknowledged");
            } catch (error) {
                // The server receipt now prevents emission on a later page load.
            }
        }).catch(() => {
            // Retry only the acknowledgement, never the already processed purchase.
            if (acknowledgementAttempts < 3) {
                acknowledgementTimer = window.setTimeout(() => {
                    acknowledgementTimer = null;
                    acknowledgePurchaseReceipt(element, transactionId);
                }, acknowledgementAttempts * 2000);
            }
        }).finally(() => {
            window.clearTimeout(timeout);
            acknowledgementInFlight = false;
        });
    }

    function itemFromElement(element, index = 0) {
        return cleanItem({
            item_id: element.dataset.itemId,
            item_name: element.dataset.itemName,
            item_brand: "Luxury Smart Apartments",
            item_category: "vacation_rental",
            item_category2: element.dataset.itemCity,
            item_category3: element.dataset.itemType,
            index,
            quantity: 1,
        });
    }

    function pageLanguage(element) {
        const value = element?.dataset?.analyticsPageLanguage
            || element?.dataset?.analyticsLanguage
            || document.documentElement?.lang
            || "";
        return String(value).split("-")[0].toLowerCase();
    }

    function propertyElement(element) {
        const local = typeof element?.closest === "function"
            ? element.closest("[data-analytics-property-id], [data-item-id]")
            : null;
        return local || document.querySelector("[data-analytics-view-item]");
    }

    function propertyContext(element) {
        const source = propertyElement(element);
        return {
            property_id: element?.dataset?.analyticsPropertyId
                || source?.dataset?.analyticsPropertyId
                || source?.dataset?.itemId,
            property_name: element?.dataset?.analyticsPropertyName
                || source?.dataset?.analyticsPropertyName
                || source?.dataset?.itemName,
            page_language: pageLanguage(element),
        };
    }

    function propertyItem(element) {
        const source = propertyElement(element);
        const context = propertyContext(element);
        if (!context.property_id && !context.property_name) {
            return null;
        }
        return cleanItem({
            item_id: context.property_id,
            item_name: context.property_name,
            item_brand: "Luxury Smart Apartments",
            item_category: "vacation_rental",
            item_category2: element?.dataset?.analyticsPropertyCity
                || source?.dataset?.analyticsPropertyCity
                || source?.dataset?.itemCity,
            item_category3: element?.dataset?.analyticsPropertyType
                || source?.dataset?.analyticsPropertyType
                || source?.dataset?.itemType,
            quantity: 1,
        });
    }

    document.querySelectorAll("[data-analytics-list]").forEach((list) => {
        const items = [...list.querySelectorAll("[data-analytics-item]")].map(itemFromElement);
        if (items.length) {
            pushEvent("view_item_list", {
                item_list_name: list.dataset.analyticsList,
                items,
                page_language: pageLanguage(list),
            });
        }
    });
    document.querySelectorAll("[data-analytics-item] a").forEach((link) => {
        link.addEventListener("click", () => {
            const item = link.closest("[data-analytics-item]");
            pushEvent("select_item", {
                item_list_name: item.closest("[data-analytics-list]")?.dataset.analyticsList || "properties",
                items: [itemFromElement(item)],
                ...propertyContext(item),
            });
        });
    });
    const detail = document.querySelector("[data-analytics-view-item]");
    if (detail) {
        pushEvent("view_item", {
            items: [itemFromElement(detail)],
            ...propertyContext(detail),
        });
    }
    const reviewPage = document.querySelector("[data-analytics-view-all-reviews]");
    if (reviewPage) {
        pushEvent("view_all_reviews", {
            language: reviewPage.dataset.analyticsLanguage,
            review_count: Number(reviewPage.dataset.analyticsReviewCount || 0),
            items: [itemFromElement(reviewPage)],
            ...propertyContext(reviewPage),
        });
    }
    const purchase = document.querySelector("[data-analytics-purchase-event]");
    let purchasePushed = false;
    let purchaseProcessed = false;
    function emitPurchase() {
        if (!purchase) {
            return;
        }
        const transactionId = purchase.dataset.analyticsTransactionId;
        const storageKey = `lsa:purchase:v2:${transactionId}`;
        try {
            const status = window.sessionStorage.getItem(storageKey);
            if (status === "acknowledged") {
                return;
            }
            if (status === "processed") {
                purchaseProcessed = true;
            }
        } catch (error) {
            // Server receipts still prevent emission on a later acknowledged load.
        }
        if (purchaseProcessed) {
            acknowledgePurchaseReceipt(purchase, transactionId);
            return;
        }
        if (purchasePushed) {
            return;
        }
        // Set before push: GTM can invoke callbacks synchronously.
        purchasePushed = true;
        const value = Number(purchase.dataset.analyticsValue || 0);
        const currency = purchase.dataset.analyticsCurrency;
        const purchasedItem = propertyItem(purchase);
        const pushed = pushEvent("purchase", {
            transaction_id: transactionId,
            value,
            currency,
            ...(purchasedItem ? {items: [{...purchasedItem, price: value, currency}]} : {}),
            ...propertyContext(purchase),
        }, () => {
            if (purchaseProcessed) {
                return;
            }
            purchaseProcessed = true;
            try {
                window.sessionStorage.setItem(storageKey, "processed");
            } catch (error) {
                // The server receipt remains the cross-page fallback.
            }
            acknowledgePurchaseReceipt(purchase, transactionId);
        });
        if (!pushed) {
            purchasePushed = false;
        }
    }
    emitPurchase();
    window.addEventListener("online", () => {
        if (!purchase) {
            return;
        }
        if (purchasePushed && !purchaseProcessed) {
            // The original dataLayer event stays queued; do not enqueue it twice.
            window.LSAConsent?.loadPurchaseTracker?.();
        }
        if (purchaseProcessed && !acknowledgementInFlight) {
            window.clearTimeout(acknowledgementTimer);
            acknowledgementTimer = null;
            acknowledgementAttempts = 0;
            acknowledgePurchaseReceipt(purchase, purchase.dataset.analyticsTransactionId);
        }
    });
    document.querySelectorAll("[data-analytics-event]").forEach((element) => {
        const eventName = element.dataset.analyticsEvent;
        const trigger = element.matches("form") ? "submit" : (
            element.matches("a,button") ? "click" : null
        );
        const emit = () => {
            const item = propertyItem(element);
            pushEvent(eventName, {
                lead_source: element.dataset.analyticsLeadSource,
                language: element.dataset.analyticsLanguage,
                contact_placement: element.dataset.analyticsContactPlacement
                    || element.dataset.analyticsLeadSource,
                reason_code: element.dataset.analyticsReason,
                available: element.dataset.analyticsAvailable === "true",
                nights: Number(element.dataset.analyticsNights || 0),
                guests: Number(element.dataset.analyticsGuests || 0),
                currency: element.dataset.analyticsCurrency,
                value: Number(element.dataset.analyticsValue || 0),
                request_type: element.dataset.analyticsRequestType,
                items: item ? [item] : undefined,
                ...propertyContext(element),
            });
        };
        if (trigger) {
            element.addEventListener(trigger, emit);
        } else {
            emit();
        }
    });
    document.addEventListener("lsa:consent-updated", (event) => {
        emitPurchase();
        pushEvent("cookie_consent_updated", {
            analytics: event.detail.analytics,
            marketing: event.detail.marketing,
            version: event.detail.version,
        });
    });
    document.querySelectorAll("[data-language-select]").forEach((select) => {
        select.addEventListener("change", () => {
            pushEvent("language_changed", {language: select.value});
        });
    });
    if (body.dataset.pendingAnalyticsEvent === "generate_lead") {
        const page_language = pageLanguage(body);
        pushEvent("generate_lead", {lead_source: "contact_form", page_language});
        pushEvent("contact_form_submitted", {lead_source: "contact_form", page_language});
    }
    window.LSAAnalytics = {pushEvent, sanitize};
}());
