/* Display names only: validation is the same authoritative server phone parser.
 * No phone storage, external requests, location lookup or checkout changes. */
document.querySelectorAll("[data-invoice-phone-country]").forEach((select) => {
    try {
        const names = new Intl.DisplayNames(["ar"], {type: "region"});
        Array.from(select.options).forEach((option) => {
            const callingCode = option.textContent.match(/\(\+\d+\)/)?.[0] || "";
            const name = option.value === "SA" ? "السعودية" : names.of(option.value);
            option.textContent = `${name} \u2066${callingCode}\u2069`;
        });
    } catch (_) { /* Country codes remain readable without browser support. */ }
});
