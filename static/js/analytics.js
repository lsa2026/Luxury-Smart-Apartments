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
    const schemas = {
        view_item_list: new Set(["item_list_name", "items"]),
        select_item: new Set(["item_list_name", "items"]),
        view_item: new Set(["currency", "value", "items"]),
        begin_checkout: new Set(["currency", "value", "items", "nights", "guests"]),
        generate_lead: new Set(["lead_source"]),
        check_availability: new Set(["city", "nights", "guests"]),
        availability_result: new Set(["available", "reason_code", "city", "nights"]),
        quote_created: new Set(["currency", "value", "nights", "guests", "items"]),
        quote_expired: new Set(["reason_code"]),
        booking_intent_created: new Set(["currency", "value", "items"]),
        modification_request_created: new Set(["request_type"]),
        cancellation_request_created: new Set(["request_type"]),
        contact_form_submitted: new Set(["lead_source"]),
        language_changed: new Set(["language"]),
        cookie_consent_updated: new Set(["analytics", "marketing", "version"]),
        purchase: new Set(["transaction_id", "value", "currency", "items", "tax", "coupon"]),
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
        const clean = {};
        Object.entries(payload).forEach(([key, value]) => {
            if (!schema.has(key)) {
                return;
            }
            if (key === "items" && Array.isArray(value)) {
                clean.items = value.slice(0, 20).map(cleanItem).filter(
                    (item) => Object.keys(item).length,
                );
            } else if (["string", "number", "boolean"].includes(typeof value)) {
                clean[key] = typeof value === "string" ? value.slice(0, 200) : value;
            }
        });
        return clean;
    }

    function pushEvent(eventName, payload) {
        const clean = sanitize(eventName, payload);
        if (!clean) {
            return false;
        }
        if (debug) {
            console.info("LSA analytics event", eventName, Object.keys(clean));
        }
        if (!enabled) {
            return clean;
        }
        const consent = window.LSAConsent?.parseConsent();
        if (!consent?.analytics) {
            return clean;
        }
        window.dataLayer = window.dataLayer || [];
        window.dataLayer.push({event: eventName, ...clean});
        return clean;
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

    document.querySelectorAll("[data-analytics-list]").forEach((list) => {
        const items = [...list.querySelectorAll("[data-analytics-item]")].map(itemFromElement);
        if (items.length) {
            pushEvent("view_item_list", {
                item_list_name: list.dataset.analyticsList,
                items,
            });
        }
    });
    document.querySelectorAll("[data-analytics-item] a").forEach((link) => {
        link.addEventListener("click", () => {
            const item = link.closest("[data-analytics-item]");
            pushEvent("select_item", {
                item_list_name: item.closest("[data-analytics-list]")?.dataset.analyticsList || "properties",
                items: [itemFromElement(item)],
            });
        });
    });
    const detail = document.querySelector("[data-analytics-view-item]");
    if (detail) {
        pushEvent("view_item", {items: [itemFromElement(detail)]});
    }
    document.querySelectorAll("[data-analytics-event]").forEach((element) => {
        const eventName = element.dataset.analyticsEvent;
        const trigger = element.matches("form") ? "submit" : (
            element.matches("a,button") ? "click" : null
        );
        const emit = () => pushEvent(eventName, {
            lead_source: element.dataset.analyticsLeadSource,
            language: element.dataset.analyticsLanguage,
            reason_code: element.dataset.analyticsReason,
            available: element.dataset.analyticsAvailable === "true",
            nights: Number(element.dataset.analyticsNights || 0),
            guests: Number(element.dataset.analyticsGuests || 0),
            currency: element.dataset.analyticsCurrency,
            value: Number(element.dataset.analyticsValue || 0),
            request_type: element.dataset.analyticsRequestType,
        });
        if (trigger) {
            element.addEventListener(trigger, emit);
        } else {
            emit();
        }
    });
    document.addEventListener("lsa:consent-updated", (event) => {
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
        pushEvent("generate_lead", {lead_source: "contact_form"});
        pushEvent("contact_form_submitted", {lead_source: "contact_form"});
    }
    window.LSAAnalytics = {pushEvent, sanitize};
}());
