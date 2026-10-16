/* Read-only delivery feedback for an authenticated mobile-linking request. */
(() => {
  const status = document.querySelector('[data-phone-delivery]');
  if (!status || status.dataset.state !== 'waiting') return;
  const form = document.querySelector('[data-phone-code-form]');
  let attempts = 0;
  let stopped = false;
  let timer;
  async function check() {
    attempts += 1;
    try {
      if (document.hidden) return;
      const response = await fetch(status.dataset.url, {
        credentials: 'same-origin', cache: 'no-store',
        headers: { Accept: 'application/json' },
      });
      if (!response.ok) return;
      const { state } = await response.json();
      if (!['waiting', 'accepted', 'failed', 'expired'].includes(state)) return;
      status.textContent = status.dataset[state];
      status.dataset.state = state;
      if (form) form.hidden = state === 'failed' || state === 'expired';
    } catch (_) {
      // A missed status read must never resend a code or claim delivery.
    } finally {
      if (!stopped && status.dataset.state === 'waiting' && attempts < 30) {
        timer = setTimeout(check, 2000);
      }
    }
  }
  timer = setTimeout(check, 2000);
  window.addEventListener('pagehide', () => {
    stopped = true;
    clearTimeout(timer);
  }, { once: true });
})();
