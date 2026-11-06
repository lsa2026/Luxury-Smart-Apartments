// Morning-only hints and early feedback. Server validation remains authoritative.
export function regionKey(value) {
    return value.normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase()
        .replace(/ province$/, '').replace(/[\s.'’-]+/gu, '');
}

export function morningError(name, value, country, regions = {}) {
    const text = value.trim();
    if (!text) return '';
    if (name.startsWith('billing_') && /[<>\p{Cc}]/u.test(text)) return 'أدخل نص العنوان دون وسوم أو رموز تحكم.';
    if (name === 'guest_name') {
        const parts = text.split(/\s+/u);
        const first = parts.shift();
        const last = parts.join(' ');
        if (!last || !/\p{L}/u.test(first) || !/\p{L}/u.test(last)) return 'أدخل الاسم الأول واسم العائلة؛ لا يُقبل اسم واحد أو أرقام فقط.';
        if (first.length > 50 || last.length > 50) return 'الاسم الأول واسم العائلة: 50 حرفًا كحد أقصى لكل جزء.';
        if (/[<>\p{Cc}]/u.test(text)) return 'أدخل الاسم الحقيقي دون وسوم أو رموز تحكم.';
    }
    if (name === 'guest_email' && (text.length < 6 || text.length > 128 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/u.test(text))) return 'أدخل بريدًا صحيحًا مثل name@example.com؛ من 6 إلى 128 حرفًا.';
    if (name === 'guest_phone' && (!/^\+[0-9 ()./-]+$/u.test(text) || text.replace(/\D/g, '').length > 15 || text.replace(/\D/g, '').length < 8)) return 'أدخل رقمًا دوليًا صحيحًا يبدأ بـ + ورمز الدولة، مثل +966501234567؛ حتى 15 رقمًا.';
    if (name === 'billing_street1' && text.length > 50) return 'عنوان الشارع يجب ألا يتجاوز 50 حرفًا؛ صحح العنوان دون حذف جزء منه عشوائيًا.';
    if (name === 'billing_city' && (text.length < 2 || text.length > 45)) return 'اسم المدينة يجب أن يكون من حرفين إلى 45 حرفًا.';
    if (name === 'billing_postcode') {
        if (country === 'SA' && !/^[0-9]{5}$/.test(text)) return 'الرمز البريدي السعودي يجب أن يتكون من 5 أرقام فقط، مثل 13221؛ لا تدخل اسم الشارع.';
        if (text.length > 10 || !/^[A-Za-z0-9]+(?:[ -][A-Za-z0-9]+)*$/.test(text)) return 'استخدم رمزًا بريديًا حتى 10 خانات: أحرف لاتينية أو أرقام، مع مسافة أو شرطة فقط.';
    }
    if (name === 'billing_state') {
        const code = text.toUpperCase().startsWith(`${country}-`) ? text.slice(3) : text;
        if (country === 'SA') {
            if (!(code in regions) && !Object.values(regions).flat().some(alias => regionKey(alias) === regionKey(code))) return 'اختر منطقة سعودية صحيحة، مثل الرياض أو الرمز 01.';
        } else if (!/^[A-Za-z0-9]{1,3}$/.test(code)) return 'أدخل رمز المنطقة من 1 إلى 3 أحرف لاتينية أو أرقام، مثل NY؛ لا تكتب اسمًا طويلًا.';
    }
    return '';
}

export function initMorningValidation(form) {
    const fields = [...form.querySelectorAll('.morning-field input, .morning-field select')];
    if (!fields.length || form.dataset.morningValidationReady) return;
    form.dataset.morningValidationReady = 'true';
    form.noValidate = true;
    let submitted = false;
    const country = () => form.elements.billing_country?.value || '';
    let regions = {};
    try { regions = JSON.parse(form.elements.billing_state?.dataset.saRegions || '{}'); } catch { /* Server still validates. */ }
    function validate(field, show = false) {
        field.setCustomValidity('');
        let error = morningError(field.name, field.value, country(), regions);
        const partialBilling = fields.some(candidate => candidate.name.startsWith('billing_') && candidate.name !== 'billing_country' && candidate.value.trim());
        if (!error && partialBilling && field.name.startsWith('billing_') && !field.value.trim()) error = field.required ? 'أكمل هذا الجزء من عنوان الفوترة للمتابعة.' : 'أكمل هذا الجزء من عنوان الفوترة، أو اترك العنوان كله لاستكماله من الضيف.';
        if (!error && !field.validity.valid) {
            const label = field.closest('.morning-field')?.querySelector('label')?.textContent.replace('*', '').trim() || 'الحقل';
            if (field.validity.valueMissing) error = `أدخل ${label} للمتابعة.`;
            else if (field.validity.rangeUnderflow) error = `${label}: الحد الأدنى ${field.min}.`;
            else if (field.validity.rangeOverflow) error = `${label}: الحد الأقصى ${field.max}.`;
            else if (field.validity.stepMismatch) error = `${label}: استخدم قيمة توافق الخطوة ${field.step}؛ للمبلغ خانتان عشريتان كحد أقصى.`;
            else error = `${label}: ${field.dataset.ruleHelp || 'راجع القيمة المدخلة وصحح تنسيقها.'}`;
        }
        field.setCustomValidity(error);
        if (show) {
            const wrapper = field.closest('.morning-field');
            const output = wrapper.querySelector('.morning-field__error');
            output.textContent = error;
            wrapper.classList.toggle('morning-field--error', Boolean(error));
            field.setAttribute('aria-invalid', String(Boolean(error)));
        }
        return !error;
    }
    for (const field of fields) {
        let touched = false;
        field.addEventListener('blur', () => { touched = true; validate(field, true); });
        field.addEventListener('input', () => validate(field, touched || submitted));
        field.addEventListener('change', () => {
            validate(field, touched || submitted);
            if (field.name === 'billing_country') {
                for (const name of ['billing_state', 'billing_postcode']) {
                    const related = form.elements[name];
                    if (related) validate(related, Boolean(related.value));
                }
            }
        });
        if (form.hasAttribute('data-morning-legacy-invalid') && field.value) validate(field, true);
    }
    form.addEventListener('submit', event => {
        submitted = true;
        let first = null;
        for (const field of fields) if (!validate(field, true) && !first) first = field;
        // Keep browser validation for checkboxes and fields outside these wrappers.
        const other = [...form.elements].find(field => field.willValidate && !fields.includes(field) && !field.checkValidity());
        if (first || other) {
            event.preventDefault();
            (first || other).focus();
            if (!first) other.reportValidity();
        }
    });
}
if (typeof document !== 'undefined') {
    for (const form of document.querySelectorAll('form')) initMorningValidation(form);
}
