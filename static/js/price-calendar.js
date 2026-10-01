// Lazy-loaded, display-only calendar. It never creates a quote or reservation.
const DAY = 86400000;
const utcDate = (value) => new Date(`${value}T12:00:00Z`);
const dateKey = (date) => date.toISOString().slice(0, 10);

export function calendarLocale(language) {
    return language === "ar" ? "ar-SA-u-ca-gregory" : language === "fr" ? "fr-FR" : "en-GB";
}

export function stayIsAvailable(days, arrival, departure) {
    if (!arrival || !departure || departure <= arrival) return false;
    const first = days.get(arrival);
    const last = days.get(departure);
    const nights = (utcDate(departure) - utcDate(arrival)) / DAY;
    if (!first?.arrival_available || !last?.departure_available ||
        nights < (first.minimum_stay || 1)) return false;
    for (let day = utcDate(arrival); dateKey(day) < departure; day = new Date(+day + DAY)) {
        if (!days.get(dateKey(day))?.available) return false;
    }
    return true;
}

export function lowestInMonth(days, year, month, today) {
    const prefix = `${year}-${String(month + 1).padStart(2, "0")}-`;
    const prices = [...days.values()].filter((day) => day.date.startsWith(prefix) &&
        day.date >= today && day.available && day.price !== null && Number(day.price) > 0
    ).map((day) => Number(day.price));
    // No artificial promotion when every night has the same price.
    return new Set(prices).size > 1 ? Math.min(...prices) : null;
}

let stylesheetPromise;
let requestController;
let activeTrigger;
let generation = 0;
const monthCache = new Map();

function loadStyles(dialog) {
    stylesheetPromise ||= new Promise((resolve, reject) => {
        const link = document.createElement("link");
        link.rel = "stylesheet";
        link.href = dialog.dataset.styleUrl;
        link.onload = resolve;
        link.onerror = () => { link.remove(); stylesheetPromise = null; reject(); };
        document.head.append(link);
    });
    return stylesheetPromise;
}

export async function openPriceCalendar(trigger) {
    const token = ++generation;
    const dialog = document.querySelector("[data-price-calendar]");
    await loadStyles(dialog);
    if (token !== generation) return;
    requestController?.abort();
    activeTrigger = trigger;
    const lang = document.documentElement.lang || "en";
    const locale = calendarLocale(lang);
    const labels = dialog.dataset;
    const q = (selector) => dialog.querySelector(selector);
    const months = q("[data-price-months]");
    const status = q("[data-price-status]");
    const next = q("[data-price-next]");
    const previous = q("[data-price-prev]");
    const continueLink = q("[data-price-continue]");
    const shortDate = (key) => new Intl.DateTimeFormat(locale, {
        day: "numeric", month: "short", timeZone: "UTC"
    }).format(utcDate(key));
    const baseUrl = new URL(trigger.href, window.location.href);
    baseUrl.hash = "availability";
    let today = new Intl.DateTimeFormat("en-CA", {
        timeZone: "Asia/Riyadh", year: "numeric", month: "2-digit", day: "2-digit"
    }).format(new Date());
    let currentMonth = new Date(Date.UTC(utcDate(today).getUTCFullYear(),
        utcDate(today).getUTCMonth(), 1, 12));
    let coverageEnd = "";
    let arrival = "";
    let departure = "";
    let currency = "";
    let updatedAt = "";
    let failed = false;
    const days = new Map();
    const number = new Intl.NumberFormat(locale, {maximumFractionDigits: 2});

    q("[data-price-property]").textContent = trigger.dataset.priceCalendarName;
    months.replaceChildren();
    q("[data-price-arrival]").textContent = "—";
    q("[data-price-departure]").textContent = "—";
    continueLink.href = baseUrl.href;
    continueLink.removeAttribute("aria-disabled");
    continueLink.onclick = null;
    q("[data-price-close]").onclick = () => dialog.close();
    dialog.onclose = () => {
        requestController?.abort();
        ++generation;
        document.documentElement.classList.remove("has-price-calendar");
        activeTrigger?.focus();
    };
    dialog.onclick = (event) => { if (event.target === dialog) dialog.close(); };
    q("[data-price-clear]").onclick = () => { arrival = departure = ""; render(); };
    previous.onclick = () => {
        currentMonth.setUTCMonth(currentMonth.getUTCMonth() - 1);
        loadMonth();
    };
    next.onclick = () => {
        currentMonth.setUTCMonth(currentMonth.getUTCMonth() + 1);
        loadMonth();
    };

    function renderSelection() {
        q("[data-price-arrival]").textContent = arrival ? shortDate(arrival) : "—";
        q("[data-price-departure]").textContent = departure ? shortDate(departure) : "—";
        const valid = stayIsAvailable(days, arrival, departure);
        continueLink.href = baseUrl.href;
        continueLink.removeAttribute("aria-disabled");
        if (arrival || departure) {
            continueLink.setAttribute("aria-disabled", String(!valid));
        }
        if (valid) {
            const url = new URL(baseUrl);
            url.searchParams.set("source", "browse");
            url.searchParams.set("check_in", arrival);
            url.searchParams.set("check_out", departure);
            if (!url.searchParams.has("guests")) url.searchParams.set("guests", "2");
            continueLink.href = url.href;
        }
        continueLink.onclick = (event) => {
            if (continueLink.getAttribute("aria-disabled") === "true") event.preventDefault();
        };
    }

    function render() {
        if (token !== generation) return;
        const count = window.matchMedia("(min-width: 760px)").matches ? 2 : 1;
        const monthTitle = (date) => new Intl.DateTimeFormat(locale, {
            month: "long", year: "numeric", timeZone: "UTC"
        }).format(date);
        previous.disabled = dateKey(currentMonth).slice(0, 7) <= today.slice(0, 7);
        const afterCurrent = new Date(currentMonth);
        afterCurrent.setUTCMonth(afterCurrent.getUTCMonth() + 1);
        next.disabled = !coverageEnd || dateKey(afterCurrent) >= coverageEnd;
        q("[data-price-month-label]").textContent = monthTitle(currentMonth);
        months.replaceChildren();
        for (let offset = 0; offset < count; offset++) {
            const date = new Date(currentMonth);
            date.setUTCMonth(date.getUTCMonth() + offset);
            const year = date.getUTCFullYear(), month = date.getUTCMonth();
            const section = document.createElement("section");
            const heading = document.createElement("h3");
            heading.textContent = monthTitle(date);
            section.append(heading);
            const grid = document.createElement("div");
            grid.className = "price-calendar__grid";
            for (let index = 0; index < 7; index++) {
                const weekday = document.createElement("span");
                weekday.className = "price-calendar__weekday";
                weekday.textContent = new Intl.DateTimeFormat(locale, {
                    weekday: "short", timeZone: "UTC"
                }).format(new Date(Date.UTC(2026, 0, 4 + index, 12)));
                grid.append(weekday);
            }
            for (let blank = 0; blank < date.getUTCDay(); blank++) grid.append(document.createElement("span"));
            const lowest = lowestInMonth(days, year, month, today);
            const lastDay = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
            for (let dayNumber = 1; dayNumber <= lastDay; dayNumber++) {
                const key = dateKey(new Date(Date.UTC(year, month, dayNumber, 12)));
                const info = days.get(key);
                const button = document.createElement("button");
                button.type = "button";
                button.className = "price-calendar__day";
                const choosingDeparture = arrival && !departure && key > arrival;
                button.disabled = key < today || !info ||
                    !(choosingDeparture ? info.departure_available : info.arrival_available);
                const dayLabel = document.createElement("strong");
                dayLabel.textContent = number.format(dayNumber);
                const price = document.createElement("small");
                // Prices belong to available nights, not occupied nights.
                price.textContent = info?.available && info.price !== null ? number.format(Number(info.price)) : "—";
                button.append(dayLabel, price);
                if (!info?.available) button.classList.add("is-unavailable");
                if (info?.available && lowest !== null && Number(info.price) === lowest) button.classList.add("is-lowest");
                if (key === today) button.classList.add("is-today");
                if (key === arrival || key === departure) button.classList.add("is-selected");
                if (arrival && departure && arrival < key && key < departure) button.classList.add("is-in-range");
                const fullDate = new Intl.DateTimeFormat(locale, {dateStyle: "full", timeZone: "UTC"}).format(utcDate(key));
                const minimum = info?.minimum_stay > 1 ? `, ${labels.minimumStay}: ${number.format(info.minimum_stay)}` : "";
                button.setAttribute("aria-label", `${fullDate}, ${info?.available ? labels.available : labels.unavailable}, ${price.textContent} ${currency}${minimum}`);
                button.setAttribute("aria-pressed", String(key === arrival || key === departure));
                button.onclick = () => {
                    if (choosingDeparture) departure = key;
                    else { arrival = key; departure = ""; }
                    render();
                };
                grid.append(button);
            }
            section.append(grid);
            months.append(section);
        }
        renderSelection();
        const time = updatedAt ? new Intl.DateTimeFormat(locale, {
            dateStyle: "short", timeStyle: "short", timeZone: "Asia/Riyadh"
        }).format(new Date(updatedAt)) : "";
        status.textContent = failed ? labels.error :
            `${arrival && departure && !stayIsAvailable(days, arrival, departure) ? labels.invalidStay :
                arrival && !departure ? labels.chooseDeparture : labels.chooseArrival} · ${labels.nightly}: ${currency} · ${labels.updated}: ${time}`;
    }

    async function loadMonth() {
        requestController?.abort();
        requestController = new AbortController();
        const controller = requestController;
        const start = dateKey(currentMonth) < today ? today : dateKey(currentMonth);
        const endDate = new Date(currentMonth);
        endDate.setUTCMonth(endDate.getUTCMonth() + 2);
        const url = new URL(trigger.dataset.priceCalendarUrl, window.location.href);
        url.searchParams.set("start", start);
        url.searchParams.set("end", dateKey(endDate));
        status.textContent = labels.loading;
        previous.disabled = next.disabled = true;
        months.setAttribute("aria-busy", "true");
        failed = false;
        try {
            let document = monthCache.get(url.href);
            if (!document || Date.now() - document.loadedAt > 300000) {
                const response = await fetch(url, {signal: controller.signal, credentials: "same-origin"});
                if (!response.ok) throw new Error("calendar_unavailable");
                document = {data: await response.json(), loadedAt: Date.now()};
                if (!Array.isArray(document.data.days)) throw new Error("calendar_invalid");
                monthCache.set(url.href, document);
            }
            if (token !== generation || controller.signal.aborted) return;
            today = document.data.today;
            currency = document.data.currency;
            updatedAt = document.data.updated_at;
            coverageEnd = document.data.coverage_end;
            document.data.days.forEach((day) => days.set(day.date, day));
        } catch (error) {
            if (error.name === "AbortError" || token !== generation) return;
            failed = true;
            days.clear();
            coverageEnd = "";
        } finally {
            if (token === generation && !controller.signal.aborted) {
                months.removeAttribute("aria-busy");
                render();
            }
        }
    }
    document.documentElement.classList.add("has-price-calendar");
    if (!dialog.open) dialog.showModal();
    loadMonth();
}
