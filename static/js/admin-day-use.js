(() => {
  "use strict";
  const form = document.querySelector("[data-day-use-availability]");
  if (!form) return;
  const select = form.querySelector("[name=property]");
  const token = form.querySelector("[name=quote_token]");
  const confirm = form.querySelector("[data-confirm-day-use]");
  const status = form.querySelector("[data-availability-status]");
  const price = form.querySelector("[data-day-use-price]");
  const check = form.querySelector("[data-check-availability]");
  const inputs = ["date", "start_time", "end_time", "guests"].map(name => form.elements[name]);
  let candidates = [], generation = 0;
  function invalidate() {
    generation += 1;
    token.value = "";
    confirm.disabled = true;
    price.textContent = "";
    select.replaceChildren(new Option("افحص التوفر أولًا", ""));
  }
  inputs.forEach(input => input.addEventListener("input", invalidate));
  select.addEventListener("change", () => {
    const row = candidates.find(item => item.id === select.value);
    token.value = row ? row.quote_token : "";
    confirm.disabled = !row;
    price.textContent = row ? `الإجمالي المعتمد: ${new Intl.NumberFormat("ar-SA", {style: "currency", currency: "SAR"}).format(row.amount_sar)}` : "";
  });
  check.addEventListener("click", async () => {
    invalidate();
    const current = generation;
    check.disabled = true;
    status.textContent = "جارٍ التحقق من التقويم المباشر والسعر…";
    const params = new URLSearchParams(inputs.map(input => [input.name, input.value]));
    try {
      const response = await fetch(`${form.dataset.dayUseAvailability}?${params}`, {credentials: "same-origin", cache: "no-store"});
      const result = await response.json();
      if (generation !== current) return;
      if (!response.ok) throw new Error(result.detail || "تعذر فحص التوفر.");
      candidates = result.properties || [];
      select.replaceChildren(new Option("اختر الشقة المتاحة", ""));
      candidates.forEach(row => select.add(new Option(row.name, row.id)));
      status.textContent = candidates.length ? "اختر الشقة ثم راجع المبلغ وأكمل بيانات الضيف." : (result.detail || "لا توجد شقة ثبت توفرها للفترة المحددة.");
    } catch (error) {
      if (generation === current) status.textContent = error.message || "تعذر فحص التوفر.";
    } finally {
      check.disabled = false;
    }
  });
  form.addEventListener("submit", event => {
    if (!token.value || confirm.disabled) event.preventDefault();
    else confirm.disabled = true;
  });
  invalidate();
})();
