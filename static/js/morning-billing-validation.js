/* MorningStay-only client hints. Django must independently validate every POST. */
(() => {
  "use strict";
  const pattern = /^[A-Za-z0-9]+(?:[ -][A-Za-z0-9]+)*$/;
  const fields = document.querySelectorAll("[data-morning-billing]");
  if (!fields.length) return;

  const ruleFor = (field) => {
    const text = field.value.trim();
    if (!text) return "";
    const minimum = Number(field.getAttribute("minlength") || 0);
    const maximum = Number(field.getAttribute("maxlength") || 9999);
    if (text.length < minimum || text.length > maximum) {
      return "راجع عدد الأحرف المطلوب لهذا الحقل؛ لا يمكن المتابعة قبل التصحيح.";
    }
    if (field.name !== "billing_postcode") return "";
    const form = field.closest("form");
    const country = form?.querySelector("[name='billing_country']")?.value || "";
    if (country === "SA" && !/^[0-9]{5}$/.test(text)) {
      return "الرمز البريدي السعودي يجب أن يتكون من 5 أرقام فقط، مثل 12345. لا تدخل اسم شخص أو شارع.";
    }
    if (country !== "SA" && !pattern.test(text)) {
      return "أدخل رمزًا بريديًا صحيحًا: أحرف لاتينية وأرقام ومسافات أو شرطة فقط، بحد أقصى 10.";
    }
    return "";
  };

  const validate = (field, show = false) => {
    const error = ruleFor(field);
    field.setCustomValidity(error);
    if (!show && !field.dataset.morningTouched) return error;
    const container = field.closest(".form-field, .form-row") || field.parentElement;
    let text = container.querySelector(".morning-billing-live-error");
    if (!text) {
      text = document.createElement("p");
      text.className = "morning-billing-live-error";
      text.setAttribute("role", "alert");
      container.appendChild(text);
    }
    text.textContent = error;
    text.hidden = !error;
    field.setAttribute("aria-invalid", error ? "true" : "false");
    return error;
  };

  for (const field of fields) {
    field.addEventListener("input", () => validate(field));
    field.addEventListener("change", () => validate(field));
    field.addEventListener("blur", () => {
      field.dataset.morningTouched = "1";
      validate(field, true);
    });
  }
  for (const form of new Set([...fields].map(f => f.form).filter(Boolean))) {
    form.querySelector("[name='billing_country']")?.addEventListener("change", () => {
      const postcode = form.querySelector("[name='billing_postcode']");
      if (!postcode) return;
      const isSaudi = form.querySelector("[name='billing_country']").value === "SA";
      postcode.placeholder = isSaudi ? "12345" : "Postal / ZIP code";
      if (postcode.dataset.morningTouched) validate(postcode, true);
    });
    form.addEventListener("submit", (event) => {
      const invalid = [...form.querySelectorAll("[data-morning-billing]")]
        .find(field => {
          field.dataset.morningTouched = "1";
          return Boolean(validate(field, true));
        });
      if (invalid) {
        event.preventDefault();
        invalid.focus();
        invalid.reportValidity();
      }
    });
  }
})();
