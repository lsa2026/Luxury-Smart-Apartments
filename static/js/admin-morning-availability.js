"use strict";

(() => {
  const form = document.querySelector("[data-morning-availability]");
  if (!form) return;
  const check = form.querySelector("[data-morning-check]");
  const status = form.querySelector("[data-morning-status]");
  const results = form.querySelector("[data-morning-results]");
  const property = form.elements.namedItem("property");
  const fields = ["date", "start_time", "end_time", "guests"];
  let generation = 0;
  const invalidate = () => {
    generation += 1;
    results.replaceChildren();
    results.removeAttribute("aria-busy");
    status.textContent = "تغيّرت الفترة أو عدد الضيوف؛ اضغط التحقق لتحديث الشقق المتاحة.";
    check.disabled = false;
  };
  fields.forEach(name => form.elements.namedItem(name).addEventListener("input", invalidate));
  check.addEventListener("click", async () => {
    const parameters = new URLSearchParams();
    for (const name of fields) {
      const field = form.elements.namedItem(name);
      if (!field.reportValidity()) return;
      parameters.set(name, field.value);
    }
    const requestGeneration = ++generation;
    check.disabled = true;
    results.replaceChildren();
    status.textContent = "جارٍ التحقق من الشقق المتاحة…";
    results.setAttribute("aria-busy", "true");
    try {
      const response = await fetch(`${form.dataset.morningAvailability}?${parameters}`, {
        credentials: "same-origin", cache: "no-store", headers: {Accept: "application/json"}
      });
      if (requestGeneration !== generation) return;
      if (response.redirected) throw new Error("انتهت جلسة الإدارة؛ أعد تسجيل الدخول.");
      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || "تعذر فحص التوفر؛ أعد المحاولة.");
      }
      const data = await response.json();
      if (requestGeneration !== generation) return;
      const checkedAt = new Intl.DateTimeFormat("ar-SA", {
        hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "Asia/Riyadh"
      }).format(new Date(data.checked_at));
      status.textContent = data.detail || (data.properties.length
        ? `تم التحقق الساعة ${checkedAt} بتوقيت الرياض. اختر إحدى الشقق التالية. يعاد فحص التوفر عند إنشاء الطلب وقبل الدفع.`
        : `لا توجد شقق ثبت توفرها للفترة المحددة. آخر فحص: ${checkedAt} بتوقيت الرياض.`);
      if (data.unverified_count) status.textContent += " تعذر التحقق من بعض الشقق؛ لم تُعرض ضمن المتاح.";
      for (const item of data.properties) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "lsa-morning-availability__property";
        button.setAttribute("aria-pressed", "false");
        const name = document.createElement("strong");
        name.textContent = item.name;
        const detail = document.createElement("span");
        detail.textContent = `${parameters.get("date")} · ${parameters.get("start_time")}–${parameters.get("end_time")} · ${item.time_zone}`;
        const action = document.createElement("b");
        action.textContent = "اختيار الشقة";
        button.append(name, detail, action);
        button.addEventListener("click", () => {
          property.value = item.id;
          property.dispatchEvent(new Event("change", {bubbles: true}));
          results.querySelectorAll("button").forEach(other => {
            other.setAttribute("aria-pressed", String(other === button));
            other.querySelector("b").textContent = "اختيار الشقة";
          });
          action.textContent = "تم اختيار الشقة";
          property.focus();
        });
        results.append(button);
      }
    } catch (error) {
      if (requestGeneration === generation) status.textContent = error instanceof TypeError || error instanceof SyntaxError
        ? "تعذر الاتصال للتحقق من التوفر؛ أعد المحاولة."
        : error.message || "تعذر التحقق من التوفر؛ أعد المحاولة.";
    } finally {
      if (requestGeneration === generation) {
        check.disabled = false;
        results.removeAttribute("aria-busy");
      }
    }
  });
})();
