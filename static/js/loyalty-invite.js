/* Guest-initiated clipboard only: no requests, tracking, storage, or form submission. */
export function bindInviteCopy(panel, {clipboard = globalThis.navigator?.clipboard, page = globalThis.document} = {}) {
    if (!panel || panel.dataset.copyBound) return;
    const input = panel.querySelector('[data-invite-link]');
    const button = panel.querySelector('[data-invite-copy-button]');
    const status = panel.querySelector('[data-invite-copy-status]');
    if (!input || !button || !status) return;
    panel.dataset.copyBound = 'true';
    let copying = false;
    async function copy() {
        if (copying || !input.value) return;
        copying = true;
        button.disabled = true;
        status.textContent = '';
        status.removeAttribute('data-copy-error');
        let copied = false;
        try {
            if (clipboard?.writeText) {
                try { await clipboard.writeText(input.value); copied = true; } catch { /* Try selection fallback. */ }
            }
            if (!copied) {
                input.focus({preventScroll: true});
                input.select();
                input.setSelectionRange(0, input.value.length);
                try { copied = page?.execCommand?.('copy') === true; } catch { /* Keep selected for manual copying. */ }
            }
            status.textContent = copied ? panel.dataset.copySuccess : panel.dataset.copyFailed;
            if (copied) button.focus({preventScroll: true});
            else status.setAttribute('data-copy-error', '');
        } finally {
            button.disabled = false;
            copying = false;
        }
    }
    button.addEventListener('click', copy);
    return {copy};
}

if (typeof document !== 'undefined') {
    document.querySelectorAll('[data-invite-copy]').forEach(panel => bindInviteCopy(panel));
}
