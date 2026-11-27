import { addressSnapshot, applyAddressFields } from "./checkout-address.js";

const contactFields = ["guest_first_name", "guest_last_name", "guest_name", "guest_email", "guest_phone_country", "guest_phone", "guest_language"];
const contacts = form => Object.fromEntries(contactFields.map(name => [name, form.elements[name]?.value]));
const query = form => ({ email: form.elements.guest_email?.value.trim() || "", phone: form.elements.guest_phone?.value.trim() || "", country: form.elements.guest_phone_country?.value || "SA" });
const queryKey = form => JSON.stringify(query(form));

export function applySavedGuest(form, values, snapshot, { addressOnly = false } = {}) {
    let changed = 0;
    if (!addressOnly) {
        for (const name of contactFields) {
            const field = form.elements[name], value = values?.[name];
            if (!field || field.value !== snapshot.contact[name] || typeof value !== "string" || !value.trim()) continue;
            if (field.tagName === "SELECT" && !Array.from(field.options).some(option => option.value === value)) continue;
            field.value = value;
            field.dispatchEvent(new Event("input", { bubbles: true }));
            field.dispatchEvent(new Event("change", { bubbles: true }));
            changed++;
        }
    }
    return changed + applyAddressFields(form, values, snapshot.address);
}

export function initSavedGuest(panel, { fetcher = globalThis.fetch } = {}) {
    const form = panel.closest("form");
    const button = panel.querySelector("[data-load-saved-guest]");
    const choices = panel.querySelector("[data-saved-guest-choices]");
    const select = panel.querySelector("[data-saved-guest-choice]");
    const preview = panel.querySelector("[data-saved-guest-preview]");
    const status = panel.querySelector("[data-saved-guest-status]");
    const all = panel.querySelector("[data-apply-saved-guest]");
    const address = panel.querySelector("[data-apply-saved-address]");
    if (!form || !button || !choices || !select || !status || !preview || !all || !address || !fetcher) return null;
    let version = 0, controller, candidates = [], snapshot, lookupKey, applying = false;
    button.hidden = false;
    const close = () => { choices.hidden = true; candidates = []; select.replaceChildren(); preview.replaceChildren(); };
    function changedQuery() {
        if (applying || !lookupKey || queryKey(form) === lookupKey) return;
        version++; controller?.abort(); close(); button.disabled = false;
        status.textContent = "تغير البريد أو الجوال؛ أعد البحث للبيانات الحالية.";
    }
    for (const name of ["guest_email", "guest_phone", "guest_phone_country"]) {
        form.elements[name]?.addEventListener("input", changedQuery);
        form.elements[name]?.addEventListener("change", changedQuery);
    }
    function show() {
        const item = candidates[Number(select.value)];
        preview.replaceChildren();
        if (!item) return;
        const fields = item.fields;
        const lines = [fields.guest_name, [fields.guest_email, fields.guest_phone].filter(Boolean).join(" · "),
            [fields.billing_street1, fields.billing_city, fields.billing_state, fields.billing_postcode, fields.billing_country].filter(Boolean).join(" · "),
            item.address_kind === "residence" ? "المصدر: عنوان السكن في ملف الضيف؛ راجع مطابقته لعنوان الدفع." : item.address_kind === "contact" ? "المصدر: عنوان تواصل هوستاوي؛ راجع مطابقته لعنوان الدفع." : "المصدر: عنوان فوترة محفوظ من حجز سابق."];
        if (!item.address_available) lines.push("لا يوجد عنوان محفوظ في هذا السجل؛ يمكن استرجاع بيانات التواصل.");
        else if (!item.address_complete) lines.push("العنوان المحفوظ يحتاج استكمالًا أو تصحيحًا قبل الاعتماد.");
        for (const line of lines.filter(Boolean)) { const p = document.createElement("p"); p.textContent = line; preview.append(p); }
        address.disabled = !item.address_available;
    }
    select.addEventListener("change", show);
    button.addEventListener("click", async () => {
        if (form.elements.guest_phone?.value.trim() && form.validateGuestPhone && !(await form.validateGuestPhone())) return;
        const current = ++version;
        controller?.abort(); controller = new AbortController(); close();
        lookupKey = queryKey(form);
        snapshot = { contact: contacts(form), address: addressSnapshot(form) };
        button.disabled = true; status.textContent = "جارٍ البحث في بيانات الضيف المحفوظة…";
        try {
            const response = await fetcher(panel.dataset.checkUrl, {
                method: "POST", credentials: "same-origin", cache: "no-store", signal: controller.signal,
                headers: { "X-CSRFToken": form.elements.csrfmiddlewaretoken.value },
                body: new URLSearchParams({ action: "lookup_saved_guest", ...query(form) }),
            });
            const result = await response.json();
            if (current !== version || queryKey(form) !== lookupKey) return;
            if (!response.ok) { status.textContent = result.errors ? Object.values(result.errors).flat().join(" ") : "تعذر استرجاع البيانات الآن."; return; }
            if (result.status !== "found" || !Array.isArray(result.candidates) || !result.candidates.length) {
                status.textContent = result.status === "unavailable" ? "تعذر تحديث البيانات الآن؛ لم تُغيّر حقول الحجز." : "لم أجد سجلًا محفوظًا يطابق بيانات البحث معًا. يمكنك البحث بالجوال أو البريد وحده، أو تحديث فحص هوستاوي أدناه.";
                return;
            }
            candidates = result.candidates.slice(0, 5);
            if (candidates.some(item => !item || typeof item.fields !== "object" || !item.fields)) throw Error("invalid_data");
            candidates.forEach((item, index) => { const option = document.createElement("option"); option.value = String(index); option.textContent = [item.source_label, item.fields.guest_name, item.saved_at].filter(Boolean).join(" — "); select.append(option); });
            select.value = "0"; choices.hidden = false; show();
            status.textContent = result.remote_unavailable ? "هذه البيانات المحفوظة متاحة؛ تعذر تحديث سجل هوستاوي الآن. اختر السجل وراجع البيانات." : "اختر السجل المناسب، ثم استرجع البيانات أو العنوان فقط. ستظل جميع الحقول قابلة للتعديل.";
        } catch (_) { if (current === version) status.textContent = "تعذر الاتصال؛ لم تُغيّر حقول الحجز."; }
        finally { if (current === version) button.disabled = false; }
    });
    function apply(addressOnly) {
        if (queryKey(form) !== lookupKey) { changedQuery(); return; }
        const selected = candidates[Number(select.value)];
        if (!selected) return;
        applying = true;
        const count = applySavedGuest(form, selected.fields, snapshot, { addressOnly });
        applying = false; close(); lookupKey = null;
        status.textContent = count ? "استُرجعت البيانات المتاحة. راجعها وأكمل الناقص؛ احتُفظ بأي حقل عدّلته بعد البحث. لم يُعتمد الحجز أو يُنشأ دفع." : "لم تُستبدل الحقول التي عدّلتها. راجع البيانات أو أعد البحث.";
    }
    all.addEventListener("click", () => apply(false));
    address.addEventListener("click", () => apply(true));
    return { close };
}

if (typeof document !== "undefined") document.querySelectorAll("[data-admin-guest-check]").forEach(panel => initSavedGuest(panel));
