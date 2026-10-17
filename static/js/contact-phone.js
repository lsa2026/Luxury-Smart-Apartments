// Presentation only. The shared server parser handles national/international
// numbers even without JavaScript. No requests, storage, or submit interception.
document.querySelectorAll("[data-contact-phone-country]").forEach((select) => {
    try {
        const names = new Intl.DisplayNames([document.documentElement.lang || "ar"], {type: "region"});
        Array.from(select.options).forEach((option) => {
            const code = option.textContent.match(/\(\+\d+\)/)?.[0] || "";
            option.textContent = `${names.of(option.value)} \u2066${code}\u2069`;
        });
    } catch (_) { /* Country codes remain usable if localized names are unavailable. */ }
});
