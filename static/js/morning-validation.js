/* Shared billing/customer validation. Rules come from the server. */
(function (root) {
    "use strict";
    function fieldError(field) {
        if (field.dataset.morningConsent !== undefined) return field.checked ? "" : field.dataset.morningError;
        const value = field.value.replace(/^[ \t\r\n\f\v]+|[ \t\r\n\f\v]+$/g, "");
        const pattern = field.dataset.morningPattern;
        const language = field.ownerDocument?.documentElement.lang;
        const message = (ar, en, fr) => language === "fr" ? fr : language === "en" ? en : ar;
        const maxLength = Number(field.dataset.morningMaxLength || -1);
        if (maxLength >= 0 && [...value].length > maxLength) return message(`الحد الأقصى ${maxLength} حرفًا. قلّل النص للمتابعة.`, `Use at most ${maxLength} characters.`, `Utilisez au maximum ${maxLength} caractères.`);
        if (!pattern) {
            const validity = field.validity || {};
            if (validity.customError) return field.validationMessage;
            if (validity.valueMissing) return message("أكمل هذا الحقل للمتابعة.", "Complete this field.", "Remplissez ce champ.");
            if (validity.rangeOverflow) return message(`القيمة القصوى ${field.max}.`, `Maximum: ${field.max}.`, `Maximum : ${field.max}.`);
            if (validity.rangeUnderflow) return message(`القيمة الدنيا ${field.min}.`, `Minimum: ${field.min}.`, `Minimum : ${field.min}.`);
            if (!validity.valid && field.validity) return message("تحقق من صيغة القيمة المدخلة.", "Check this value.", "Vérifiez cette valeur.");
            return "";
        }
        if (!field.required && !value) return "";
        if (value.includes("\0") || !(new RegExp("^(?:" + pattern + ")(?![\\s\\S])", "u")).test(value)) return field.dataset.morningError;
        return "";
    }
    function attach(form) {
        const fields = Array.from(form.querySelectorAll("input:not([type=hidden]), select, textarea"));
        const owned = new WeakMap();
        fields.forEach(field => {
            if (field.maxLength >= 0) {
                field.dataset.morningMaxLength = String(field.maxLength);
                field.removeAttribute("maxlength");
            }
            if (!field.id) return;
            if (!form.querySelector("#" + field.id + "_error") && !field.hasAttribute("data-guest-phone")) {
                const output = field.ownerDocument.createElement("p");
                output.id = field.id + "_error";
                output.className = "form-error";
                output.setAttribute("aria-live", "polite");
                field.after(output);
                field.setAttribute("aria-describedby", ((field.getAttribute("aria-describedby") || "") + " " + output.id).trim());
            }
        });
        const optionalBilling = fields.filter(field => field.name.startsWith("billing_") && !field.required);
        function billingRequired() {
            const supplied = optionalBilling.some(field => field.name !== "billing_country" && field.value.trim());
            optionalBilling.forEach(field => { field.required = supplied; });
        }
        function check(field) {
            billingRequired();
            if (owned.has(field) && field.validationMessage === owned.get(field)) field.setCustomValidity("");
            const message = fieldError(field);
            field.setCustomValidity(message);
            owned.set(field, message);
            field.setAttribute("aria-invalid", message ? "true" : "false");
            const output = field.hasAttribute("data-guest-phone") ? form.querySelector("#guest-phone-validation") : form.querySelector("#" + field.id + "_error");
            if (output) { output.textContent = message; output.hidden = !message; }
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
                const invalid = fields.find(field => !field.disabled && field.willValidate && !field.validity.valid);
                invalid?.closest("details")?.setAttribute("open", "");
                if (invalid) form.dispatchEvent(new CustomEvent("billing-validation-invalid", { detail: { field: invalid } }));
                form.reportValidity();
            }
        }, true);
        form.noValidate = true;
        // Revalidate restored browser autofill and programmatic replacements too.
        fields.filter(field => field.getAttribute("aria-invalid") === "true").forEach(check);
    }
    if (typeof module !== "undefined") module.exports = { fieldError, attach };
    if (root.document) root.document.querySelectorAll("form[data-morning-validation]").forEach(attach);
})(typeof window === "undefined" ? globalThis : window);
