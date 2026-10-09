/* Checkout-only enhancement. The same server parser validates the final POST,
 * so blocked JavaScript or an unavailable formatting check never loses a booking.
 * Only the country preference is remembered, never the phone number. */
document.querySelectorAll("[data-checkout-phone]").forEach((widget) => {
    const form = widget.closest("form");
    const country = widget.querySelector("[data-phone-country]");
    const phone = widget.querySelector("[data-guest-phone]");
    const error = form?.querySelector("#guest-phone-validation");
    if (!form || !country || !phone || !error) return;
    const preferenceKey = "lsa-phone-country-v1";
    const validCountries = new Set(Array.from(country.options, (option) => option.value));
    let revision = 0;
    let checked = null;
    let pending = null;
    const remember = () => {
        try { localStorage.setItem(preferenceKey, country.value); } catch (_) { /* Optional. */ }
    };
    try {
        const names = new Intl.DisplayNames([document.documentElement.lang || "en"], {type: "region"});
        Array.from(country.options).forEach((option) => {
            const callingCode = option.textContent.match(/\(\+\d+\)/)?.[0] || "";
            option.textContent = `${names.of(option.value)} \u2066${callingCode}\u2069`;
        });
    } catch (_) { /* ISO names and calling codes remain available. */ }
    if (form.dataset.draftServerBound !== "true" && !phone.value) {
        try {
            const saved = localStorage.getItem(preferenceKey);
            if (validCountries.has(saved)) country.value = saved;
        } catch (_) { /* Saudi default is still valid. */ }
    }
    function clearError() {
        revision += 1;
        checked = null;
        phone.setCustomValidity("");
        phone.removeAttribute("aria-invalid");
        error.hidden = true;
        error.textContent = "";
    }
    async function validate() {
        const key = `${country.value}:${phone.value}`;
        if (!phone.value.trim()) return true; // Native required-field validation.
        if (checked?.key === key) return checked.valid;
        if (pending?.key === key && pending.revision === revision) return pending.promise;
        const currentRevision = revision;
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 4000);
        const promise = (async () => {
            try {
                const response = await fetch(widget.dataset.checkUrl, {
                    method: "POST",
                    credentials: "same-origin",
                    headers: {"X-CSRFToken": form.querySelector("[name=csrfmiddlewaretoken]").value},
                    body: new URLSearchParams({phone: phone.value, country: country.value}),
                    signal: controller.signal,
                });
                const data = await response.json();
                if (revision !== currentRevision) return validate();
                if (response.ok && typeof data.phone === "string") {
                    phone.value = data.phone;
                    if (validCountries.has(data.country)) country.value = data.country;
                    remember();
                    phone.setCustomValidity("");
                    phone.removeAttribute("aria-invalid");
                    error.hidden = true;
                    checked = {key: `${country.value}:${phone.value}`, valid: true};
                    phone.dispatchEvent(new Event("change", {bubbles: true}));
                    return true;
                }
                if (response.status === 400 && data.detail === "invalid_phone") {
                    error.textContent = widget.dataset.invalidMessage;
                    error.hidden = false;
                    phone.setCustomValidity(error.textContent);
                    phone.setAttribute("aria-invalid", "true");
                    checked = {key, valid: false};
                    return false;
                }
            } catch (_) { /* Final server form validation remains authoritative. */ }
            finally { clearTimeout(timeout); }
            return true;
        })();
        pending = {key, promise, revision: currentRevision};
        const valid = await promise;
        if (pending?.promise === promise) pending = null;
        return valid;
    }
    form.validateGuestPhone = async () => {
        const valid = await validate();
        if (!valid) phone.focus();
        return valid;
    };
    phone.addEventListener("input", clearError);
    phone.addEventListener("blur", validate);
    country.addEventListener("change", () => {
        clearError();
        remember();
        if (phone.value.trim()) validate();
    });
});
