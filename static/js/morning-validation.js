/* Only MorningStay forms carry these attributes. Rules come from the server. */
(function (root) {
    "use strict";
    function fieldError(field) {
        if (field.dataset.morningConsent !== undefined) return field.checked ? "" : field.dataset.morningError;
        const value = field.value.replace(/^[ \t\r\n\f\v]+|[ \t\r\n\f\v]+$/g, "");
        const pattern = field.dataset.morningPattern;
        if (!pattern) return "";
        if (!field.required && !value) return "";
        if (value.includes("\0") || !(new RegExp("^(?:" + pattern + ")(?![\\s\\S])", "u")).test(value)) return field.dataset.morningError;
        return "";
    }
    function attach(form) {
        const fields = Array.from(form.querySelectorAll("[data-morning-pattern], [data-morning-consent]"));
        const optionalBilling = fields.filter(field => field.name.startsWith("billing_") && !field.required);
        function billingRequired() {
            const supplied = optionalBilling.some(field => field.name !== "billing_country" && field.value.trim());
            optionalBilling.forEach(field => { field.required = supplied; });
        }
        function check(field) {
            billingRequired();
            const message = fieldError(field);
            field.setCustomValidity(message);
            field.setAttribute("aria-invalid", message ? "true" : "false");
            const output = form.querySelector("#" + field.id + "_error");
            if (output) output.textContent = message;
            return !message;
        }
        fields.forEach(field => {
            ["input", "change", "blur"].forEach(name => field.addEventListener(name, () => check(field)));
        });
        form.addEventListener("submit", event => {
            const valid = fields.map(check).every(Boolean);
            if (!valid || !form.checkValidity()) {
                event.preventDefault();
                event.stopImmediatePropagation();
                form.reportValidity();
            }
        }, true);
        form.noValidate = true;
    }
    if (typeof module !== "undefined") module.exports = { fieldError, attach };
    if (root.document) root.document.querySelectorAll("form[data-morning-validation]").forEach(attach);
})(typeof window === "undefined" ? globalThis : window);
