/* Visible-page database status checks only; never initiate payment or delivery. */
(() => {
  const marker = document.querySelector("[data-booking-status-url]");
  const source = document.getElementById("booking-status-watch");
  if (!marker || !source) return;
  let watch;
  try { watch = JSON.parse(source.textContent); } catch { return; }
  if (!Array.isArray(watch.ids) || !watch.ids.length || !watch.fingerprint) return;
  const url = new URL(marker.dataset.bookingStatusUrl, window.location.origin);
  if (url.origin !== window.location.origin) return;
  url.searchParams.set("payment_status_snapshot", "1");
  url.searchParams.set("ids", watch.ids.join(","));
  let stopped = false;
  let pending = false;
  document.addEventListener("submit", () => { stopped = true; });
  const editing = () => [...document.querySelectorAll("input, textarea, select")].some(field => {
    if (field.type === "hidden") return false;
    if (field === document.activeElement) return true;
    if (field.tagName === "SELECT") {
      const initial = [...field.options].findIndex(option => option.defaultSelected);
      return field.selectedIndex !== (initial < 0 ? 0 : initial);
    }
    if (["checkbox", "radio"].includes(field.type)) return field.checked !== field.defaultChecked;
    return field.value !== field.defaultValue;
  });
  async function check() {
    if (!stopped && !document.hidden) {
      if (pending && !editing()) { window.location.reload(); return; }
      try {
        const response = await fetch(url, {credentials: "same-origin", cache: "no-store", headers: {Accept: "application/json"}});
        if (response.ok && !response.redirected) {
          const state = await response.json();
          if (typeof state.fingerprint === "string" && state.fingerprint !== watch.fingerprint) {
            pending = true;
            if (!editing()) { window.location.reload(); return; }
          }
        } else if (response.redirected || response.status === 403) { stopped = true; }
      } catch { /* Keep the current verified display; retry on the next visible-page check. */ }
    }
    if (!stopped) window.setTimeout(check, 15000);
  }
  window.setTimeout(check, 15000);
})();
