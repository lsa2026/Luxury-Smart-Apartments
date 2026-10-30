(function () {
  "use strict";
  const root = document.querySelector("[data-loyalty-credential-copy]");
  if (!root) return;
  const input = root.querySelector("#loyalty-credential-value");
  const copy = root.querySelector("[data-copy-credential]");
  const show = root.querySelector("[data-show-credential]");
  const status = root.querySelector("[data-copy-status]");
  if (!input || !copy || !show || !status) return;
  copy.addEventListener("click", async () => {
    try {
      if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error("unavailable");
      await navigator.clipboard.writeText(input.value);
      status.textContent = "تم النسخ. ألصق القيمة في n8n ثم احفظ الاعتماد.";
    } catch {
      input.type = "text";
      show.setAttribute("aria-pressed", "true");
      show.textContent = "إخفاء المفتاح";
      input.focus();
      input.select();
      status.textContent = "تعذر النسخ التلقائي. القيمة محددة؛ انسخها يدويًا باستخدام Ctrl+C أو قائمة النسخ.";
    }
  });
  show.addEventListener("click", () => {
    const visible = input.type === "password";
    input.type = visible ? "text" : "password";
    show.setAttribute("aria-pressed", String(visible));
    show.textContent = visible ? "إخفاء المفتاح" : "إظهار المفتاح مؤقتًا";
  });
  // No network, logging, persistence, background clipboard access or automatic copy.
})();
