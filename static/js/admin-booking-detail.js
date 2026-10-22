/* Progressive, presentation-only tabs. Never submit, fetch, or rewrite forms. */
(function () {
  "use strict";

  function initializeBookingDetail(root, browser) {
    if (!root || root.dataset.tabsReady) return null;
    const tabs = Array.from(root.querySelectorAll("[data-booking-tab]"));
    const panels = Array.from(root.querySelectorAll("[data-booking-panel]"));
    const navigation = root.querySelector("[data-booking-tabs]");
    if (!navigation || !tabs.length || tabs.length !== panels.length) return null;
    const names = panels.map((panel) => panel.dataset.bookingPanel);
    if (!tabs.every((tab) => names.includes(tab.dataset.bookingTab))) return null;

    function hashPanel() {
      const name = (browser.location.hash || "").replace(/^#booking-/, "");
      return names.includes(name) ? name : null;
    }

    function activate(name, options) {
      if (!names.includes(name)) return;
      panels.forEach((panel) => {
        panel.hidden = panel.dataset.bookingPanel !== name;
        panel.setAttribute("role", "tabpanel");
      });
      tabs.forEach((tab) => {
        const active = tab.dataset.bookingTab === name;
        tab.setAttribute("role", "tab");
        tab.setAttribute("aria-selected", String(active));
        tab.setAttribute("tabindex", active ? "0" : "-1");
        if (active && options?.focus) tab.focus();
      });
      if (options?.hash && browser.history?.replaceState) {
        browser.history.replaceState(null, "", "#booking-" + name);
      }
    }

    function reveal(target) {
      const panel = target.closest("[data-booking-panel]");
      if (panel) activate(panel.dataset.bookingPanel);
      let details = target.closest("details");
      while (details) {
        details.open = true;
        details = details.parentElement?.closest("details");
      }
    }

    navigation.setAttribute("role", "tablist");
    root.dataset.tabsReady = "true";
    tabs.forEach((tab, index) => {
      tab.addEventListener("click", (event) => {
        event.preventDefault();
        activate(tab.dataset.bookingTab, {hash: true});
      });
      tab.addEventListener("keydown", (event) => {
        let next;
        if (event.key === "ArrowLeft") next = (index + 1) % tabs.length;
        if (event.key === "ArrowRight") next = (index + tabs.length - 1) % tabs.length;
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = tabs.length - 1;
        if (next === undefined) return;
        event.preventDefault();
        activate(tabs[next].dataset.bookingTab, {focus: true, hash: true});
      });
    });
    root.querySelectorAll("[data-booking-open]").forEach((link) => {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        activate(link.dataset.bookingOpen, {hash: true});
      });
    });
    // No preventDefault: genuine POSTs and server validation remain authoritative.
    root.addEventListener("submit", (event) => reveal(event.target));
    root.addEventListener("invalid", (event) => reveal(event.target), true);
    browser.addEventListener("hashchange", () => activate(hashPanel()));
    const initial = root.dataset.defaultPanel;
    activate(initial && initial !== "invoice" && names.includes(initial)
      ? initial : hashPanel() || "invoice");
    const error = root.querySelector(".errorlist li");
    if (error) reveal(error);
    return {activate, reveal};
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = {initializeBookingDetail};
  }
  if (typeof document !== "undefined") {
    document.querySelectorAll("[data-booking-detail]").forEach((root) => {
      initializeBookingDetail(root, window);
    });
  }
})();
