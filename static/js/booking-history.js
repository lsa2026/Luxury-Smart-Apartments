// Status reads only: never starts, repeats, or performs a recovery in the browser.
export function watchBookingHistory(panel, {
    fetcher = globalThis.fetch?.bind(globalThis),
    schedule = globalThis.setTimeout, cancel = globalThis.clearTimeout,
    page = globalThis.document, reload = () => globalThis.location.reload(),
    lifecycle = globalThis,
} = {}) {
    const form = panel.querySelector('[data-history-form]');
    const submit = panel.querySelector('[data-history-submit]');
    const status = panel.querySelector('[data-history-status]');
    form?.addEventListener('submit', () => {
        // Let the native CSRF-protected form submit first; button has no payload.
        if (submit) { submit.disabled = true; submit.setAttribute('aria-busy', 'true'); }
    });
    lifecycle.addEventListener?.('pageshow', event => {
        if (event.persisted) reload(); // Fresh server state after back/forward cache.
    });
    if (panel.dataset.running !== 'true' || !fetcher || !status) return;
    let timer, stopped = false, requests = 0;
    function stop() { stopped = true; cancel(timer); }
    async function poll() {
        if (stopped) return;
        if (requests >= 120) { status.textContent = panel.dataset.waiting; stop(); return; }
        if (!page?.hidden) {
            requests++;
            try {
                const response = await fetcher(panel.dataset.statusUrl, {
                    credentials: 'same-origin', cache: 'no-store', headers: {Accept: 'application/json'},
                });
                if (response.redirected) { stop(); reload(); return; }
                if (!response.ok) { stop(); return; }
                const result = await response.json();
                if (stopped) return;
                if (typeof result.message === 'string') status.textContent = result.message;
                if (result.running === false) { stop(); reload(); return; }
            } catch { /* Temporary read failure never starts another import. */ }
        }
        if (!stopped) timer = schedule(poll, 2500);
    }
    lifecycle.addEventListener?.('pagehide', stop, {once: true});
    timer = schedule(poll, 1000);
    return {stop};
}
if (typeof document !== 'undefined') {
    document.querySelectorAll('[data-booking-history]').forEach(panel => watchBookingHistory(panel));
}
