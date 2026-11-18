"use strict";

// Public pp1 only. No Google/provider requests, dates, guest fields or private references.
(function initializePrepurchase() {
    const page = document.body.dataset.prepurchasePageKind || "unknown";
    const pages = new Set(["home", "property_list", "property_detail", "availability_results", "quote"]);
    if (!window.LSAAnalytics) return;
    const prefix = "lsa:pp1:";
    const ttl = 30 * 60 * 1000;
    const dwell = 1000;
    const contexts = ["measurement_version", "attempt_id", "parent_attempt_id", "request_kind",
        "entry_page", "journey_entry", "page_kind", "stay_kind", "property_id", "property_name", "page_language"];
    const records = new Map();
    let context = {};
    try { context = JSON.parse(document.getElementById("lsa-prepurchase-context")?.textContent || "{}"); }
    catch (_) { /* Unrecognized context is not a valid result. */ }
    const allowed = () => document.body.dataset.googleIntegrationsEnabled === "true"
        && window.LSAConsent?.parseConsent()?.analytics === true;
    const visible = () => document.visibilityState === "visible";
    const uuid = () => window.crypto?.randomUUID?.().replaceAll("-", "");
    const language = () => (document.documentElement.lang || "").split("-")[0];
    const field = (form, name) => form.elements.namedItem(name);
    const write = (form, name, value) => { const input = field(form, name); if (input) input.value = value || ""; };

    function storageRead(key, fallback) {
        if (!allowed()) return fallback;
        try { return JSON.parse(window.sessionStorage.getItem(prefix + key) || "null") || fallback; }
        catch (_) { return fallback; }
    }
    function storageWrite(key, value) {
        if (!allowed()) return false;
        try { window.sessionStorage.setItem(prefix + key, JSON.stringify(value)); return true; }
        catch (_) { return false; }
    }
    function claim(key) {
        const now = Date.now();
        const stored = storageRead("seen", {});
        const entries = Object.entries(stored).filter(([, at]) => Number.isFinite(at) && now - at < ttl).slice(-299);
        const clean = Object.fromEntries(entries);
        if (clean[key]) return false;
        clean[key] = now;
        return storageWrite("seen", clean);
    }
    function property(element) {
        const source = element.closest?.("[data-analytics-property-id], [data-item-id]")
            || document.querySelector("[data-analytics-view-item]");
        const data = source?.dataset || {};
        return {
            property_id: element.dataset.ppPropertyId || data.analyticsPropertyId || data.itemId,
            property_name: element.dataset.ppPropertyName || data.analyticsPropertyName || data.itemName,
        };
    }
    function base(element) {
        const safe = Object.fromEntries(contexts.filter(key => context[key] !== undefined).map(key => [key, context[key]]));
        const ownProperty = Object.fromEntries(Object.entries(property(element)).filter(([, value]) => value !== undefined));
        return {...safe, measurement_version: "pp1", request_kind: safe.request_kind || "browse",
            entry_page: safe.entry_page || page, journey_entry: safe.journey_entry || safe.entry_page || page,
            page_kind: page, stay_kind: "overnight", page_language: language(), ...ownProperty};
    }
    function emitView(element, event, payload, key) {
        if (!allowed() || !visible() || !element.isConnected || !claim(`${event}:${key}`)) return;
        window.LSAAnalytics.pushEvent(event, payload);
    }

    function price(element) {
        const data = element.dataset;
        if (data.ppValidUntil && !(Date.parse(data.ppValidUntil) > Date.now())) return;
        const value = Number(data.ppValue);
        if (!Number.isFinite(value) || value <= 0 || !/^[A-Z]{3}$/.test(data.ppCurrency || "")) return;
        const payload = {...base(element), price_context: data.ppPriceContext, currency: data.ppCurrency, value};
        if (data.ppSourceCurrency && data.ppSourceValue) {
            payload.source_currency = data.ppSourceCurrency;
            payload.source_value = Number(data.ppSourceValue);
        }
        const surface = context.attempt_id || context.surface_key || page;
        const group = data.ppGroup || `${value}:${data.ppCurrency}`;
        emitView(element, "price_view", payload,
            `${surface}:${payload.property_id || ""}:${payload.price_context}:${group}`);
    }
    function result(element) {
        if (element.dataset.ppValidUntil && !(Date.parse(element.dataset.ppValidUntil) > Date.now())) return;
        if (!context.result_status || !context.display_status || !context.attempt_id) return;
        const payload = {...base(element)};
        for (const key of ["city", "nights", "guests", "result_status", "display_status", "result_count", "checked_count", "error_count", "error_category"]) {
            if (context[key] !== undefined) payload[key] = context[key];
        }
        emitView(element, "availability_result_view", payload, `${context.attempt_id}:${context.result_status}:${context.display_status}`);
    }
    function formView(element) {
        const payload = {...base(element), request_kind: field(element, "property")?.value ? "property_quote" : "portfolio"};
        emitView(element, "availability_view", payload, `${page}:${payload.property_id || "portfolio"}`);
    }
    function arm(record) {
        if (!record.inView || !allowed() || !visible() || record.timer) return;
        record.timer = window.setTimeout(() => {
            record.timer = null;
            if (record.inView && allowed() && visible()) record.emit(record.element);
        }, dwell);
    }
    const observer = typeof window.IntersectionObserver === "function" ? new window.IntersectionObserver(entries => {
        for (const entry of entries) {
            const record = records.get(entry.target);
            if (!record) continue;
            record.inView = entry.isIntersecting && entry.intersectionRatio >= 0.5;
            if (!record.inView) { window.clearTimeout(record.timer); record.timer = null; }
            else arm(record);
        }
    }, {threshold: [0, 0.5]}) : null;
    function scan() {
        if (!observer || !pages.has(page)) return; // No unconditional/unknown-page fallback.
        for (const [selector, emit] of [["[data-pp-price]", price], ["[data-pp-result]", result], ["[data-pp-search]", formView]]) {
            document.querySelectorAll(selector).forEach(element => {
                if (records.has(element)) return;
                records.set(element, {element, emit, inView: false, timer: null});
                observer.observe(element);
            });
        }
        for (const [element, record] of records) {
            if (!element.isConnected) { window.clearTimeout(record.timer); observer.unobserve(element); records.delete(element); }
        }
    }
    function city(value) {
        const text = String(value || "").toLowerCase();
        return !text ? "all" : text.includes("riyadh") ? "riyadh" : text.includes("marrak") ? "marrakech" : "other";
    }
    function parentFor(form) {
        if (context.request_kind === "portfolio" && context.attempt_id) return context;
        const slug = property(form).property_id;
        const pending = storageRead("parent", {});
        return pending.property_id === slug && Date.now() - pending.at < ttl ? pending : {};
    }
    document.querySelectorAll("[data-pp-search]").forEach(form => {
        form.addEventListener("submit", event => {
            // Run after ALL validation/submit-once listeners, before native navigation.
            window.queueMicrotask(() => {
                if (event.defaultPrevented || !form.checkValidity()) return;
                if (!allowed()) {
                    for (const name of ["measurement_attempt", "measurement_parent", "measurement_journey"]) write(form, name, "");
                    return;
                }
                const arrival = field(form, "check_in")?.value;
                const departure = field(form, "check_out")?.value;
                const nights = (Date.parse(departure) - Date.parse(arrival)) / 86400000;
                const guests = Number(field(form, "guests")?.value);
                if (!Number.isInteger(nights) || nights < 1 || nights > 3660
                    || !Number.isInteger(guests) || guests < 1 || guests > 100) return;
                const attempt = uuid();
                if (!attempt) return; // Do not use a persistent/user identity as fallback.
                const requestKind = field(form, "property")?.value ? "property_quote" : "portfolio";
                const parent = requestKind === "property_quote" ? parentFor(form) : {};
                const payload = {...base(form), attempt_id: attempt, request_kind: requestKind,
                    entry_page: page, journey_entry: parent.journey_entry || parent.entry_page || page};
                delete payload.parent_attempt_id;
                if (parent.attempt_id) payload.parent_attempt_id = parent.attempt_id;
                payload.nights = nights;
                payload.guests = guests;
                payload.city = city(field(form, "city")?.value);
                write(form, "measurement_attempt", attempt);
                write(form, "measurement_entry", page);
                write(form, "measurement_parent", payload.parent_attempt_id);
                write(form, "measurement_journey", payload.journey_entry);
                window.LSAAnalytics.pushEvent("availability_request", payload);
            });
        });
    });
    document.addEventListener("click", event => {
        const link = event.target.closest?.("[data-analytics-item] a");
        if (!link || context.request_kind !== "portfolio" || !context.attempt_id || !allowed()) return;
        storageWrite("parent", {attempt_id: context.attempt_id, entry_page: context.entry_page,
            journey_entry: context.journey_entry, property_id: property(link).property_id, at: Date.now()});
    });
    document.addEventListener("visibilitychange", () => {
        for (const record of records.values()) {
            window.clearTimeout(record.timer); record.timer = null;
            if (visible()) arm(record);
        }
    });
    document.addEventListener("lsa:consent-updated", () => {
        if (!allowed()) {
            for (const record of records.values()) { window.clearTimeout(record.timer); record.timer = null; }
            try {
                for (let i = window.sessionStorage.length - 1; i >= 0; i--) {
                    const key = window.sessionStorage.key(i);
                    if (key?.startsWith(prefix)) window.sessionStorage.removeItem(key);
                }
            } catch (_) { /* No storage means no persisted view claim. */ }
        } else { scan(); for (const record of records.values()) arm(record); }
    });
    document.addEventListener("lsa:prepurchase-surfaces-changed", scan);
    window.addEventListener("pagehide", () => {
        for (const record of records.values()) { window.clearTimeout(record.timer); record.timer = null; }
    });
    window.addEventListener("pageshow", () => {
        scan();
        for (const record of records.values()) arm(record);
    });
    scan();
}());
