/* Localize native validation without changing constraints or custom phone errors. */
(() => {
    "use strict";
    const language = (document.documentElement.lang || "en").split("-")[0];
    const copy = {
        ar: {
            required: "يرجى تعبئة هذا الحقل.", select: "يرجى اختيار أحد الخيارات.",
            checked: "يرجى تحديد هذا الخيار للمتابعة.", email: "يرجى إدخال بريد إلكتروني صحيح.",
            url: "يرجى إدخال رابط صحيح.", pattern: "يرجى استخدام الصيغة المطلوبة لهذا الحقل.",
            number: "يرجى إدخال رقم صحيح.", min: "يجب ألا تقل القيمة عن {value}.",
            max: "يجب ألا تزيد القيمة عن {value}.", short: "أدخل {value} أحرف على الأقل.",
            long: "أدخل {value} أحرف كحد أقصى.", invalid: "يرجى التحقق من القيمة المدخلة."
        },
        fr: {
            required: "Veuillez remplir ce champ.", select: "Veuillez sélectionner une option.",
            checked: "Veuillez cocher cette option pour continuer.",
            email: "Veuillez saisir une adresse e-mail valide.", url: "Veuillez saisir une URL valide.",
            pattern: "Veuillez respecter le format demandé pour ce champ.",
            number: "Veuillez saisir un nombre valide.", min: "La valeur doit être au moins {value}.",
            max: "La valeur ne doit pas dépasser {value}.", short: "Saisissez au moins {value} caractères.",
            long: "Saisissez au maximum {value} caractères.", invalid: "Veuillez vérifier la valeur saisie."
        },
        en: {
            required: "Please fill out this field.", select: "Please select an option.",
            checked: "Please check this option to continue.", email: "Please enter a valid email address.",
            url: "Please enter a valid URL.", pattern: "Please use the requested format for this field.",
            number: "Please enter a valid number.", min: "The value must be at least {value}.",
            max: "The value must not exceed {value}.", short: "Enter at least {value} characters.",
            long: "Enter no more than {value} characters.", invalid: "Please check the entered value."
        }
    }[language];
    if (!copy) return;
    const ownedMessages = new WeakMap();
    const clearOwned = (field) => {
        const message = ownedMessages.get(field);
        if (message && field.validationMessage === message) field.setCustomValidity("");
        ownedMessages.delete(field);
    };
    document.addEventListener("invalid", (event) => {
        const field = event.target;
        if (!field || typeof field.setCustomValidity !== "function") return;
        clearOwned(field);
        const state = field.validity;
        // Existing server-assisted phone validation owns its own error text.
        if (state.customError || state.valid) return;
        let message = copy.invalid;
        if (state.valueMissing) {
            message = field.type === "checkbox" || field.type === "radio" ? copy.checked
                : field.tagName === "SELECT" ? copy.select : copy.required;
        } else if (state.typeMismatch) {
            message = field.type === "email" ? copy.email : field.type === "url" ? copy.url : copy.invalid;
        } else if (state.patternMismatch) message = copy.pattern;
        else if (state.badInput || state.stepMismatch) message = copy.number;
        else if (state.rangeUnderflow) message = copy.min.replace("{value}", field.min);
        else if (state.rangeOverflow) message = copy.max.replace("{value}", field.max);
        else if (state.tooShort) message = copy.short.replace("{value}", field.minLength);
        else if (state.tooLong) message = copy.long.replace("{value}", field.maxLength);
        field.setCustomValidity(message);
        ownedMessages.set(field, message);
    }, true);
    for (const name of ["input", "change"]) {
        document.addEventListener(name, (event) => clearOwned(event.target), true);
    }
})();
