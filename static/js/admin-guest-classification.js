/* Finite private-page polling of cached status; no guest contacts in requests. */
(function () {
    const panel = document.querySelector("[data-guest-classification-state]");
    if (!panel || !["queued", "running"].includes(panel.dataset.guestClassificationState)) return;
    const output = panel.querySelector("[data-classification-status]");
    let attempts = 0, edited = false;
    document.querySelector(".lsa-booking-list__search form")?.addEventListener("input", () => { edited = true; });
    async function poll() {
        if (document.hidden || ++attempts > 30) {
            output.textContent = "قد يستمر الفحص في الخلفية؛ حدّث القائمة للاطلاع على آخر نتيجة.";
            return;
        }
        try {
            const response = await fetch(panel.dataset.statusUrl, { credentials: "same-origin", cache: "no-store", headers: { Accept: "application/json" } });
            if (!response.ok) throw new Error("status_unavailable");
            const state = await response.json();
            if (state.status === "complete") {
                if (!edited) { window.location.reload(); return; }
                output.textContent = "اكتمل فحص هوستاوي؛ حدّث نتائج البحث لعرض التصنيف.";
                return;
            }
            if (!["queued", "running"].includes(state.status)) throw new Error("source_unavailable");
            window.setTimeout(poll, 4000);
        } catch (_) {
            output.textContent = "تعذر تأكيد اكتمال فحص هوستاوي؛ حدّث القائمة أو أعد المحاولة.";
        }
    }
    window.setTimeout(poll, 4000);
})();
