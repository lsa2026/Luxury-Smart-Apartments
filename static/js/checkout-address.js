// Optional checkout enhancement: no Google SDK, browser key, storage or analytics.
const fields = {
    billing_street1: 100, billing_city: 80, billing_state: 50,
    billing_country: 2, billing_postcode: 16,
};

export function addressSnapshot(form) {
    return Object.fromEntries(Object.keys(fields).map((name) => [name, form.elements[name]?.value]));
}

export function applyAddressFields(form, values, snapshot = addressSnapshot(form)) {
    // A country change invalidates the whole pending selection. Do not mix
    // one country's street/postcode with the country chosen manually later.
    if (form.elements.billing_country?.value !== snapshot.billing_country) return 0;
    let updated = 0;
    for (const [name, max] of Object.entries(fields)) {
        const field = form.elements[name];
        const value = values?.[name];
        if (!field || field.value !== snapshot[name] || typeof value !== "string"
            || !value.trim() || value.length > max) continue;
        if (name === "billing_country" && !Array.from(field.options).some(
            (option) => option.value === value
        )) continue;
        if (name === "billing_postcode" && !/^[A-Za-z0-9]+(?:[ -][A-Za-z0-9]+)*$/.test(value)) continue;
        field.value = value;
        field.dispatchEvent(new Event("input", { bubbles: true }));
        field.dispatchEvent(new Event("change", { bubbles: true }));
        updated += 1;
    }
    return updated;
}

export function initCheckoutAddress(panel, {
    fetcher = globalThis.fetch, delay = 600,
    schedule = globalThis.setTimeout, cancel = globalThis.clearTimeout,
} = {}) {
    const form = panel.closest("form");
    const input = panel.querySelector("[data-address-search]");
    const list = panel.querySelector("[data-address-options]");
    const results = panel.querySelector("[data-address-results]");
    const status = panel.querySelector("[data-address-status]");
    if (!form || !input || !list || !results || !status || !fetcher) return null;
    let ticket = panel.dataset.addressTicket;
    let sequence = 0;
    let timer, controller;
    let suggestions = [];
    let active = -1;
    let composing = false;
    let stopped = false;
    // Optional control is hidden without JavaScript; manual fields never are.
    panel.hidden = false;

    function close() {
        suggestions = [];
        active = -1;
        list.replaceChildren();
        results.hidden = true;
        input.setAttribute("aria-expanded", "false");
        input.removeAttribute("aria-activedescendant");
    }

    function stopPending() {
        sequence += 1;
        cancel(timer);
        controller?.abort();
        close();
        return sequence;
    }

    function announce(message) { status.textContent = panel.dataset[message] || ""; }

    async function request(url, body, signal) {
        const response = await fetcher(url, {
            method: "POST", credentials: "same-origin", cache: "no-store", signal,
            headers: { "Content-Type": "application/json", "X-CSRFToken": form.elements.csrfmiddlewaretoken.value },
            body: JSON.stringify({ ticket, ...body }),
        });
        const data = await response.json();
        return { response, data };
    }

    async function select(suggestion) {
        const current = stopPending();
        const snapshot = addressSnapshot(form);
        controller = new AbortController();
        announce("addressLoading");
        try {
            const { response, data } = await request(
                panel.dataset.addressDetailsUrl, { place_id: suggestion.place_id }, controller.signal
            );
            if (current !== sequence) return;
            if (data.next_ticket) ticket = data.next_ticket;
            if (!response.ok) throw new Error("unavailable");
            // Manual changes made during the request always win. Missing
            // components never erase a value already entered by the guest.
            applyAddressFields(form, data.fields, snapshot);
            input.value = suggestion.label;
            announce("addressReview");
            form.elements.billing_street1?.focus();
        } catch (error) {
            if (current === sequence && error.name !== "AbortError") announce("addressFallback");
        }
    }

    function render(items) {
        close();
        suggestions = items.filter((item) => typeof item.place_id === "string"
            && typeof item.label === "string").slice(0, 5);
        for (const [index, suggestion] of suggestions.entries()) {
            const option = panel.ownerDocument.createElement("button");
            option.type = "button";
            option.dir = "auto";
            option.role = "option";
            option.id = `${list.id}-${index}`;
            option.tabIndex = -1;
            option.setAttribute("aria-selected", "false");
            option.textContent = suggestion.label; // Never interpolate provider HTML.
            option.addEventListener("pointerdown", (event) => event.preventDefault());
            option.addEventListener("click", () => select(suggestion));
            list.append(option);
        }
        results.hidden = !suggestions.length;
        input.setAttribute("aria-expanded", String(Boolean(suggestions.length)));
        announce(suggestions.length ? "addressChoose" : "addressEmpty");
    }

    async function search(value, current, renewed = false) {
        controller = new AbortController();
        announce("addressLoading");
        try {
            const { response, data } = await request(
                panel.dataset.addressSuggestionsUrl, { input: value }, controller.signal
            );
            if (current !== sequence) return;
            if (data.next_ticket) ticket = data.next_ticket;
            // Expired/consumed signed token: renew once, only when the server
            // confirms it made no provider call. Never retry provider failures.
            if (response.status === 409 && data.detail === "session_expired"
                && data.next_ticket && !renewed) return search(value, current, true);
            if (!response.ok) {
                if (response.status === 429 || response.status === 503) stopped = true;
                throw new Error("unavailable");
            }
            render(Array.isArray(data.suggestions) ? data.suggestions : []);
        } catch (error) {
            if (current === sequence && error.name !== "AbortError") announce("addressFallback");
        }
    }

    function onInput() {
        const current = stopPending();
        status.textContent = "";
        if (stopped) { announce("addressFallback"); return; }
        const value = input.value.trim();
        if (!composing && value.length >= 3 && value.length <= 150) {
            timer = schedule(() => search(value, current), delay);
        }
    }
    input.addEventListener("input", onInput);
    input.addEventListener("compositionstart", () => { composing = true; stopPending(); });
    input.addEventListener("compositionend", () => { composing = false; onInput(); });
    input.addEventListener("keydown", (event) => {
        if (composing || event.isComposing) return;
        if (event.key === "Escape") { stopPending(); return; }
        if (event.key === "Enter") {
            event.preventDefault();
            if (active >= 0) select(suggestions[active]);
            return;
        }
        if (!suggestions.length || !["ArrowDown", "ArrowUp"].includes(event.key)) return;
        event.preventDefault();
        active = active < 0 ? (event.key === "ArrowDown" ? 0 : suggestions.length - 1)
            : (active + (event.key === "ArrowDown" ? 1 : -1) + suggestions.length) % suggestions.length;
        Array.from(list.children).forEach((option, index) => {
            option.setAttribute("aria-selected", String(index === active));
        });
        input.setAttribute("aria-activedescendant", list.children[active].id);
        list.children[active].scrollIntoView({ block: "nearest" });
    });
    panel.addEventListener("focusout", (event) => {
        if (!panel.contains(event.relatedTarget)) stopPending();
    });
    form.addEventListener("submit", stopPending);
    globalThis.addEventListener?.("pagehide", stopPending);
    return { stopPending };
}

if (typeof document !== "undefined") {
    document.querySelectorAll("[data-checkout-address]").forEach((panel) => initCheckoutAddress(panel));
}
