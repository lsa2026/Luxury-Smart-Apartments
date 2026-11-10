/* Private POST lookup. No contacts in URLs, browser storage or analytics. */
document.querySelectorAll("[data-admin-guest-check]").forEach(panel => {
    const button = panel.querySelector("[data-check-guest]");
    const output = panel.querySelector("[data-guest-check-result]");
    const form = panel.closest("form");
    let run = 0;
    const payload = () => panel.dataset.bookingId ? { booking_id: panel.dataset.bookingId } : {
        email: form.elements.guest_email.value,
        phone: form.elements.guest_phone.value,
        country: form.elements.guest_phone_country?.value || "SA",
    };
    const key = value => JSON.stringify(value);
    button.addEventListener("click", async () => {
        if (form?.validateGuestPhone && !(await form.validateGuestPhone())) return;
        const current = ++run, data = payload(), initial = key(data);
        button.disabled = true;
        output.textContent = "جارٍ تحديث الفحص من سجلات هوستاوي…";
        async function check(refresh, attempts) {
            if (run !== current) return;
            try {
                const token = (form || panel).querySelector("[name=csrfmiddlewaretoken]").value;
                const response = await fetch(panel.dataset.checkUrl, { method: "POST", credentials: "same-origin", cache: "no-store", headers: { "X-CSRFToken": token }, body: new URLSearchParams({ ...data, refresh: refresh ? "1" : "" }) });
                const result = await response.json();
                if (run !== current) return;
                if (key(payload()) !== initial) { output.textContent = "تغيرت بيانات الضيف؛ أعد الفحص للبيانات الحالية."; button.disabled = false; return; }
                if (!response.ok) {
                    output.textContent = result.errors ? Object.values(result.errors).flat().join(" ") : "تعذر فحص الضيف الآن.";
                } else if (["queued", "running"].includes(result.status)) {
                    if (attempts < 30 && !document.hidden) { window.setTimeout(() => check(false, attempts + 1), 4000); return; }
                    output.textContent = "الفحص لم يكتمل بعد؛ اضغط التحديث لاحقًا لعرض النتيجة.";
                } else if (result.status === "complete") {
                    output.textContent = `${result.classification.label} — ${result.classification.basis}`;
                } else output.textContent = "غير متحقق؛ تعذر تأكيد اكتمال قراءة هوستاوي.";
            } catch (_) { if (run === current) output.textContent = "تعذر الاتصال؛ أعد المحاولة."; }
            if (run === current) button.disabled = false;
        }
        await check(true, 0);
    });
});
