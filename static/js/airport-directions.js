// Display-only, one request per page visit when the arrival panel becomes visible.
// No geolocation, polling, shared response cache or browser-visible provider key.
export async function loadAirportEstimate(panel, request = fetch) {
    if (panel.dataset.requested) return;
    panel.dataset.requested = "true";
    const status = panel.querySelector("[data-route-status]");
    const updated = panel.querySelector("[data-route-updated]");
    const attribution = panel.querySelector("[data-route-attribution]");
    status.textContent = panel.dataset.loading;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
        const response = await request(panel.dataset.routeUrl, {
            method: "POST",
            credentials: "same-origin",
            cache: "no-store",
            headers: {"Content-Type": "application/json", "X-CSRFToken": panel.dataset.csrfToken},
            body: JSON.stringify({ticket: panel.dataset.routeTicket}),
            signal: controller.signal,
        });
        if (!response.ok) throw new Error("Estimate unavailable");
        const result = await response.json();
        const calculated = new Date(result.calculated_at);
        if (!Number.isFinite(result.duration_minutes) || result.duration_minutes <= 0 ||
            !Number.isFinite(result.distance_km) || result.distance_km <= 0 ||
            !Number.isFinite(calculated.getTime()) || result.attribution !== "Google Maps") {
            throw new Error("Invalid estimate");
        }
        const language = document.documentElement.lang || "en";
        const number = new Intl.NumberFormat(language, {maximumFractionDigits: 1});
        const time = new Intl.DateTimeFormat(language, {
            timeZone: panel.dataset.timeZone || "Asia/Riyadh", hour: "2-digit", minute: "2-digit",
        }).format(calculated);
        status.textContent = panel.dataset.estimate
            .replace("{minutes}", number.format(result.duration_minutes))
            .replace("{distance}", number.format(result.distance_km));
        updated.textContent = panel.dataset.updated.replace("{time}", time);
        updated.hidden = false;
        attribution.hidden = false;
    } catch {
        status.textContent = panel.dataset.unavailable;
        updated.hidden = true;
        attribution.hidden = true;
    } finally {
        clearTimeout(timeout);
    }
}

export function observeAirportPanel(panel, Observer = IntersectionObserver) {
    let intersecting = false;
    const start = () => {
        if (intersecting && !document.hidden) {
            observer.disconnect();
            document.removeEventListener("visibilitychange", start);
            loadAirportEstimate(panel);
        }
    };
    const observer = new Observer((entries) => {
        intersecting = entries.some((entry) => entry.isIntersecting);
        start();
    }, {threshold: 0.1});
    document.addEventListener("visibilitychange", start);
    observer.observe(panel);
    return observer;
}

if (typeof document !== "undefined" && typeof IntersectionObserver !== "undefined") {
    document.querySelectorAll("[data-airport-live]").forEach((panel) => observeAirportPanel(panel));
}
