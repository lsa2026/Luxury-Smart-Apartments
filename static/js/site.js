"use strict";

document.documentElement.classList.add("js");

const brandSplash = document.querySelector("[data-brand-splash]");
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

if (brandSplash) {
    let splashSeen = false;

    try {
        splashSeen = window.sessionStorage.getItem("lsa-brand-splash") === "seen";
    } catch {
        splashSeen = false;
    }

    if (splashSeen || reducedMotion) {
        brandSplash.classList.add("is-skipped");
        brandSplash.hidden = true;
    } else {
        document.body.classList.add("splash-active");

        try {
            window.sessionStorage.setItem("lsa-brand-splash", "seen");
        } catch {
            // The splash remains a one-time animation for this page when storage is unavailable.
        }

        window.setTimeout(() => brandSplash.classList.add("is-leaving"), 2450);
        window.setTimeout(() => {
            brandSplash.hidden = true;
            document.body.classList.remove("splash-active");
        }, 3300);
    }
}

if (!reducedMotion && "IntersectionObserver" in window) {
    const revealTargets = document.querySelectorAll(
        ".section-heading, .property-card, .review-card, .value-grid article, " +
        ".city-card, .cta-panel, .management-note, .faq-item, .gallery-page figure, " +
        ".form-card, .review-panel, .prose-card, .contact-note, .booking-sidebar__card"
    );

    revealTargets.forEach((element, index) => {
        element.dataset.reveal = "";
        element.style.setProperty("--reveal-delay", `${(index % 3) * 70}ms`);
    });

    document.documentElement.classList.add("reveal-ready");

    const revealObserver = new IntersectionObserver((entries, observer) => {
        entries.forEach((entry) => {
            if (entry.isIntersecting) {
                entry.target.classList.add("is-revealed");
                observer.unobserve(entry.target);
            }
        });
    }, { rootMargin: "0px 0px -6% 0px", threshold: 0.08 });

    revealTargets.forEach((element) => revealObserver.observe(element));
}

const menuToggle = document.querySelector("[data-menu-toggle]");
const mobileNavigation = document.querySelector("[data-mobile-nav]");
const menuClose = mobileNavigation?.querySelector("[data-menu-close]");
const siteHeader = document.querySelector("[data-site-header]");
let menuReturnFocus = null;

function menuFocusableElements() {
    if (!mobileNavigation) {
        return [];
    }
    return Array.from(mobileNavigation.querySelectorAll(
        "a[href], button:not([disabled]), select:not([disabled]), input:not([disabled])"
    )).filter((element) => !element.hidden && element.getClientRects().length > 0);
}

function closeMenu() {
    if (!menuToggle || !mobileNavigation || mobileNavigation.hidden) {
        return;
    }
    mobileNavigation.hidden = true;
    menuToggle.setAttribute("aria-expanded", "false");
    menuToggle.setAttribute("aria-label", menuToggle.dataset.openLabel || "Open menu");
    document.body.classList.remove("nav-open");
    siteHeader?.classList.remove("menu-open");
    if (menuReturnFocus) {
        menuReturnFocus.focus();
    }
}

if (menuToggle && mobileNavigation) {
    menuToggle.addEventListener("click", () => {
        const willOpen = mobileNavigation.hidden;
        mobileNavigation.hidden = !willOpen;
        menuToggle.setAttribute("aria-expanded", String(willOpen));
        menuToggle.setAttribute(
            "aria-label",
            willOpen
                ? (menuToggle.dataset.closeLabel || "Close menu")
                : (menuToggle.dataset.openLabel || "Open menu"),
        );
        document.body.classList.toggle("nav-open", willOpen);
        siteHeader?.classList.toggle("menu-open", willOpen);
        if (willOpen) {
            menuReturnFocus = menuToggle;
            (menuClose || mobileNavigation.querySelector("a"))?.focus();
        }
    });
    menuClose?.addEventListener("click", closeMenu);
    mobileNavigation.addEventListener("click", (event) => {
        if (event.target === mobileNavigation) {
            closeMenu();
        }
    });
}

document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
        closeMenu();
    } else if (
        event.key === "Tab"
        && mobileNavigation
        && !mobileNavigation.hidden
    ) {
        const focusable = menuFocusableElements();
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first?.focus();
        }
    }
});

window.addEventListener("resize", () => {
    if (window.matchMedia("(min-width: 68.01rem)").matches) {
        closeMenu();
    }
});

document.querySelectorAll(".form-field--error, .field-group:has(.errorlist)").forEach((wrapper) => {
    const field = wrapper.querySelector("input, select, textarea");
    const error = wrapper.querySelector(".form-error, .errorlist");
    if (!(field instanceof HTMLElement) || !(error instanceof HTMLElement)) {
        return;
    }
    field.setAttribute("aria-invalid", "true");
    if (!error.id && field.id) {
        error.id = `${field.id}-error`;
    }
    if (error.id) {
        const describedBy = new Set((field.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean));
        describedBy.add(error.id);
        field.setAttribute("aria-describedby", Array.from(describedBy).join(" "));
    }
});

document.querySelectorAll("[data-language-select]").forEach((select) => {
    select.addEventListener("change", () => select.form?.requestSubmit());
});

document.querySelectorAll("[data-review-toggle]").forEach((button) => {
    const copy = document.getElementById(button.getAttribute("aria-controls"));
    const visibleLabel = button.querySelector("[data-review-toggle-label]");
    const lessLabel = button.querySelector("[data-review-less-label]");

    if (!copy || !visibleLabel || !lessLabel) {
        return;
    }

    button.hidden = false;
    button.addEventListener("click", () => {
        const isExpanded = button.getAttribute("aria-expanded") === "true";
        button.setAttribute("aria-expanded", String(!isExpanded));
        copy.classList.toggle("is-expanded", !isExpanded);
        visibleLabel.textContent = isExpanded ? visibleLabel.dataset.moreLabel : lessLabel.textContent;
    });
    visibleLabel.dataset.moreLabel = visibleLabel.textContent;
});

document.querySelectorAll("[data-review-collection]").forEach((collection) => {
    const cards = Array.from(collection.querySelectorAll("[data-review-card]"));
    const moreWrap = collection.querySelector("[data-review-more-wrap]");
    const moreButton = collection.querySelector("[data-review-more]");
    const remainingLabel = collection.querySelector("[data-review-remaining]");
    const desktopInitialCount = Math.max(1, Number(collection.dataset.initialCount) || 6);
    const mobileInitialCount = Math.max(
        1,
        Number(collection.dataset.mobileInitialCount) || 3
    );
    const initialCount = window.matchMedia("(max-width: 48rem)").matches
        ? mobileInitialCount
        : desktopInitialCount;
    const batchSize = Math.max(1, Number(collection.dataset.batchSize) || 3);

    function updateRemainingCount() {
        const hiddenCards = cards.filter((card) => card.hidden);
        if (remainingLabel) {
            remainingLabel.textContent = hiddenCards.length ? `(${hiddenCards.length})` : "";
        }
        if (moreWrap) {
            moreWrap.hidden = hiddenCards.length === 0;
        }
    }

    cards.slice(initialCount).forEach((card) => {
        card.hidden = true;
    });
    collection.classList.add("is-ready");

    if (cards.length <= initialCount || !moreButton || !moreWrap) {
        return;
    }

    moreWrap.hidden = false;
    updateRemainingCount();
    moreButton.addEventListener("click", () => {
        cards.filter((card) => card.hidden).slice(0, batchSize).forEach((card) => {
            card.hidden = false;
        });
        updateRemainingCount();
    });
});

document.querySelectorAll("[data-availability-form]").forEach((form) => {
    const citySelect = form.querySelector("select[data-city-select]");
    const propertySelect = form.querySelector("select[data-property-select]");
    const guestInput = form.querySelector("input[name='guests']");

    if (!citySelect || !propertySelect) {
        return;
    }

    function updateGuestCapacity() {
        const selectedOption = propertySelect.selectedOptions[0];
        const capacity = Number(selectedOption?.dataset.capacity || 0);
        if (guestInput) {
            if (capacity > 0) {
                guestInput.max = String(capacity);
            } else {
                guestInput.removeAttribute("max");
            }
        }
    }

    function filterProperties() {
        const selectedCity = citySelect.value;
        Array.from(propertySelect.options).forEach((option, index) => {
            if (index === 0) {
                return;
            }
            // City is optional; with none chosen the whole portfolio stays selectable.
            const matches = !selectedCity || option.dataset.city === selectedCity;
            option.hidden = !matches;
            option.disabled = !matches;
        });
        if (propertySelect.selectedOptions[0]?.disabled) {
            propertySelect.value = "";
        }
        propertySelect.disabled = false;
        updateGuestCapacity();
    }

    const errorSummary = form.querySelector("[data-availability-errors]");
    const validatedFields = Array.from(form.elements).filter(
        (field) => field instanceof HTMLInputElement
            || field instanceof HTMLSelectElement
            || field instanceof HTMLTextAreaElement,
    );

    function fieldWrapper(field) {
        return field.closest("[data-form-field]");
    }

    function clearClientError(field) {
        const wrapper = fieldWrapper(field);
        const error = wrapper?.querySelector("[data-client-error]");
        wrapper?.classList.remove("form-field--client-error");
        field.removeAttribute("aria-invalid");
        if (error) {
            error.hidden = true;
            error.textContent = "";
        }
        if (validatedFields.every((candidate) => !candidate.willValidate || candidate.validity.valid)) {
            if (errorSummary) {
                errorSummary.hidden = true;
            }
        }
    }

    function showClientError(field) {
        const wrapper = fieldWrapper(field);
        const error = wrapper?.querySelector("[data-client-error]");
        wrapper?.classList.add("form-field--client-error");
        field.setAttribute("aria-invalid", "true");
        if (error) {
            error.textContent = field.validity.valueMissing
                ? (form.dataset.requiredMessage || "This field is required.")
                : (form.dataset.invalidMessage || "Please review this field.");
            error.hidden = false;
            if (field.id) {
                error.id = `${field.id}-client-error`;
                field.setAttribute("aria-describedby", error.id);
            }
        }
    }

    validatedFields.forEach((field) => {
        field.addEventListener("input", () => clearClientError(field));
        field.addEventListener("change", () => clearClientError(field));
    });

    form.addEventListener("submit", (event) => {
        const invalidFields = validatedFields.filter(
            (field) => field.willValidate && !field.validity.valid,
        );
        if (invalidFields.length === 0) {
            if (errorSummary) {
                errorSummary.hidden = true;
            }
            return;
        }

        event.preventDefault();
        invalidFields.forEach(showClientError);
        if (errorSummary) {
            errorSummary.hidden = false;
        }

        const firstInvalid = invalidFields[0];
        const dateTrigger = firstInvalid.name === "check_in" || firstInvalid.name === "check_out"
            ? form.querySelector(`[data-date-trigger='${firstInvalid.name}']`)
            : null;
        (dateTrigger || firstInvalid).focus();
    });

    citySelect.addEventListener("change", filterProperties);
    propertySelect.addEventListener("change", updateGuestCapacity);
    filterProperties();
});

document.querySelectorAll("[data-luxury-calendar]").forEach((calendar) => {
    const form = calendar.closest("form");
    const arrivalInput = form?.querySelector("input[name='check_in']");
    const departureInput = form?.querySelector("input[name='check_out']");
    const arrivalTrigger = form?.querySelector("[data-date-trigger='check_in']");
    const departureTrigger = form?.querySelector("[data-date-trigger='check_out']");
    const monthsContainer = calendar.querySelector("[data-calendar-months]");
    const heading = calendar.querySelector("[data-calendar-heading]");
    const help = calendar.querySelector("[data-calendar-help]");
    const arrivalSummary = calendar.querySelector("[data-calendar-arrival]");
    const departureSummary = calendar.querySelector("[data-calendar-departure]");
    const nightsSummary = calendar.querySelector("[data-calendar-nights]");
    const previousButton = calendar.querySelector("[data-calendar-previous]");
    const nextButton = calendar.querySelector("[data-calendar-next]");
    const confirmButton = calendar.querySelector("[data-calendar-confirm]");
    const clearButton = calendar.querySelector("[data-calendar-clear]");
    const closeButton = calendar.querySelector("[data-calendar-close]");
    const mobileCalendar = window.matchMedia("(max-width: 42rem)");

    if (
        !(calendar instanceof HTMLDialogElement)
        || !(arrivalInput instanceof HTMLInputElement)
        || !(departureInput instanceof HTMLInputElement)
        || !arrivalTrigger
        || !departureTrigger
        || !monthsContainer
    ) {
        return;
    }

    const documentLanguage = document.documentElement.lang || "ar";
    // CLDR defaults "ar" to Latin digits, but the rest of the site renders Arabic
    // money and dates with Arabic-Indic digits, so ask for that numbering system.
    const locale = documentLanguage.startsWith("ar")
        ? `${documentLanguage}-u-nu-arab`
        : documentLanguage;
    const chooseLabel = calendar.dataset.chooseLabel || "Choose date";
    const arrivalLabel = calendar.dataset.arrivalLabel || "Arrival";
    const departureLabel = calendar.dataset.departureLabel || "Departure";
    const shortDateFormatter = new Intl.DateTimeFormat(locale, { day: "numeric", month: "short" });
    const fullDateFormatter = new Intl.DateTimeFormat(locale, {
        weekday: "long",
        day: "numeric",
        month: "long",
        year: "numeric",
    });
    const monthFormatter = new Intl.DateTimeFormat(locale, { month: "long", year: "numeric" });
    const weekdayFormatter = new Intl.DateTimeFormat(locale, { weekday: "short" });
    const initialDepartureMinimum = departureInput.min;
    let activeField = "check_in";
    let displayMonth = null;
    let returnFocus = null;

    function parseDate(value) {
        const parts = String(value || "").split("-").map(Number);
        if (parts.length !== 3 || parts.some((part) => !Number.isFinite(part))) {
            return null;
        }
        const parsed = new Date(parts[0], parts[1] - 1, parts[2]);
        return Number.isNaN(parsed.getTime()) ? null : parsed;
    }

    function formatDate(date) {
        const year = date.getFullYear();
        const month = String(date.getMonth() + 1).padStart(2, "0");
        const day = String(date.getDate()).padStart(2, "0");
        return `${year}-${month}-${day}`;
    }

    function startOfMonth(date) {
        return new Date(date.getFullYear(), date.getMonth(), 1);
    }

    function addMonths(date, amount) {
        return new Date(date.getFullYear(), date.getMonth() + amount, 1);
    }

    function addDays(date, amount) {
        return new Date(date.getFullYear(), date.getMonth(), date.getDate() + amount);
    }

    function sameDay(left, right) {
        return Boolean(left && right && formatDate(left) === formatDate(right));
    }

    function dispatchDateChange(input) {
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new Event("change", { bubbles: true }));
    }

    const today = parseDate(arrivalInput.min) || new Date();
    today.setHours(0, 0, 0, 0);

    function updateTrigger(trigger, input, label) {
        const value = trigger.querySelector("[data-date-trigger-value]");
        const selectedDate = parseDate(input.value);
        if (value) {
            value.textContent = selectedDate ? shortDateFormatter.format(selectedDate) : chooseLabel;
        }
        trigger.setAttribute(
            "aria-label",
            selectedDate ? `${label}: ${fullDateFormatter.format(selectedDate)}` : `${label}: ${chooseLabel}`,
        );
        trigger.classList.toggle("has-value", Boolean(selectedDate));
    }

    function updateSelectionSummary() {
        const arrival = parseDate(arrivalInput.value);
        const departure = parseDate(departureInput.value);
        updateTrigger(arrivalTrigger, arrivalInput, arrivalLabel);
        updateTrigger(departureTrigger, departureInput, departureLabel);

        if (arrivalSummary) {
            arrivalSummary.textContent = arrival ? shortDateFormatter.format(arrival) : chooseLabel;
        }
        if (departureSummary) {
            departureSummary.textContent = departure ? shortDateFormatter.format(departure) : chooseLabel;
        }

        calendar.querySelectorAll("[data-calendar-mode]").forEach((button) => {
            button.classList.toggle("is-active", button.dataset.calendarMode === activeField);
        });

        if (arrival && departure && nightsSummary) {
            const nights = Math.round((departure - arrival) / 86400000);
            const label = nights === 1 ? calendar.dataset.nightLabel : calendar.dataset.nightsLabel;
            nightsSummary.textContent = `${new Intl.NumberFormat(locale).format(nights)} ${label}`;
            nightsSummary.hidden = false;
        } else if (nightsSummary) {
            nightsSummary.hidden = true;
        }

        if (help) {
            if (arrival && departure) {
                help.textContent = calendar.dataset.selectedHelp || "";
            } else if (activeField === "check_out") {
                help.textContent = calendar.dataset.departureHelp || "";
            } else {
                help.textContent = calendar.dataset.arrivalHelp || "";
            }
        }

        if (confirmButton) {
            confirmButton.disabled = !(arrival && departure);
        }
        if (clearButton) {
            // Offered as soon as anything is chosen, so a wrong arrival can be
            // undone without first having to pick a departure.
            clearButton.disabled = !(arrival || departure);
        }
    }

    function createMonth(monthDate, arrival, departure) {
        const article = document.createElement("section");
        article.className = "luxury-calendar__month";

        const title = document.createElement("h3");
        title.textContent = monthFormatter.format(monthDate);
        article.append(title);

        const weekdayRow = document.createElement("div");
        weekdayRow.className = "luxury-calendar__weekdays";
        const sunday = new Date(2023, 0, 1);
        for (let index = 0; index < 7; index += 1) {
            const weekday = document.createElement("span");
            weekday.textContent = weekdayFormatter.format(addDays(sunday, index));
            weekdayRow.append(weekday);
        }
        article.append(weekdayRow);

        const days = document.createElement("div");
        days.className = "luxury-calendar__days";
        const firstWeekday = monthDate.getDay();
        for (let index = 0; index < firstWeekday; index += 1) {
            const spacer = document.createElement("span");
            spacer.className = "luxury-calendar__day-spacer";
            spacer.setAttribute("aria-hidden", "true");
            days.append(spacer);
        }

        const lastDay = new Date(monthDate.getFullYear(), monthDate.getMonth() + 1, 0).getDate();
        for (let day = 1; day <= lastDay; day += 1) {
            const date = new Date(monthDate.getFullYear(), monthDate.getMonth(), day);
            const button = document.createElement("button");
            button.type = "button";
            button.dataset.calendarDate = formatDate(date);
            button.textContent = new Intl.NumberFormat(locale, { useGrouping: false }).format(day);
            button.setAttribute("aria-label", fullDateFormatter.format(date));

            const beforeToday = date < today;
            const beforeArrival = activeField === "check_out" && arrival && date <= arrival;
            button.disabled = Boolean(beforeToday || beforeArrival);
            button.classList.toggle("is-today", sameDay(date, today));
            button.classList.toggle("is-arrival", sameDay(date, arrival));
            button.classList.toggle("is-departure", sameDay(date, departure));
            button.classList.toggle("is-in-range", Boolean(arrival && departure && date > arrival && date < departure));
            if (sameDay(date, arrival) || sameDay(date, departure)) {
                button.setAttribute("aria-selected", "true");
            }
            days.append(button);
        }
        article.append(days);
        return article;
    }

    function renderCalendar() {
        const arrival = parseDate(arrivalInput.value);
        const departure = parseDate(departureInput.value);
        if (!displayMonth) {
            displayMonth = startOfMonth(arrival || today);
        }

        const monthCount = mobileCalendar.matches ? 1 : 2;
        monthsContainer.replaceChildren();
        for (let index = 0; index < monthCount; index += 1) {
            monthsContainer.append(createMonth(addMonths(displayMonth, index), arrival, departure));
        }

        if (heading) {
            heading.textContent = monthCount === 1
                ? monthFormatter.format(displayMonth)
                : `${monthFormatter.format(displayMonth)} — ${monthFormatter.format(addMonths(displayMonth, 1))}`;
        }
        if (previousButton) {
            previousButton.disabled = displayMonth <= startOfMonth(today);
        }
        updateSelectionSummary();
    }

    function openCalendar(field, trigger) {
        const arrival = parseDate(arrivalInput.value);
        const departure = parseDate(departureInput.value);
        activeField = field === "check_out" && !arrival ? "check_in" : field;
        const startingDate = activeField === "check_out" ? (arrival || departure || today) : (arrival || today);
        displayMonth = startOfMonth(startingDate);
        returnFocus = trigger;
        renderCalendar();
        calendar.showModal();
        document.body.classList.add("calendar-open");
        closeButton?.focus();
    }

    arrivalTrigger.hidden = false;
    departureTrigger.hidden = false;
    form.classList.add("has-luxury-calendar");
    updateSelectionSummary();

    arrivalTrigger.addEventListener("click", () => openCalendar("check_in", arrivalTrigger));
    departureTrigger.addEventListener("click", () => openCalendar("check_out", departureTrigger));

    calendar.querySelectorAll("[data-calendar-mode]").forEach((button) => {
        button.addEventListener("click", () => {
            const arrival = parseDate(arrivalInput.value);
            activeField = button.dataset.calendarMode === "check_out" && !arrival ? "check_in" : button.dataset.calendarMode;
            const selected = activeField === "check_out" ? arrival : parseDate(arrivalInput.value);
            displayMonth = startOfMonth(selected || today);
            renderCalendar();
        });
    });

    monthsContainer.addEventListener("click", (event) => {
        const button = event.target.closest("[data-calendar-date]");
        if (!button || button.disabled) {
            return;
        }
        const selectedDate = parseDate(button.dataset.calendarDate);
        if (!selectedDate) {
            return;
        }

        if (activeField === "check_in") {
            arrivalInput.value = formatDate(selectedDate);
            departureInput.min = formatDate(addDays(selectedDate, 1));
            const departure = parseDate(departureInput.value);
            if (departure && departure <= selectedDate) {
                departureInput.value = "";
                dispatchDateChange(departureInput);
            }
            dispatchDateChange(arrivalInput);
            activeField = "check_out";
            displayMonth = startOfMonth(selectedDate);
        } else {
            departureInput.value = formatDate(selectedDate);
            dispatchDateChange(departureInput);
        }
        renderCalendar();
    });

    previousButton?.addEventListener("click", () => {
        displayMonth = addMonths(displayMonth || today, -1);
        renderCalendar();
    });
    nextButton?.addEventListener("click", () => {
        displayMonth = addMonths(displayMonth || today, 1);
        renderCalendar();
    });
    closeButton?.addEventListener("click", () => calendar.close());
    clearButton?.addEventListener("click", () => {
        arrivalInput.value = "";
        departureInput.value = "";
        // Restore the floor the form started with, otherwise the previous
        // arrival keeps blocking earlier departure days after the reset.
        departureInput.min = initialDepartureMinimum;
        dispatchDateChange(arrivalInput);
        dispatchDateChange(departureInput);
        activeField = "check_in";
        displayMonth = startOfMonth(today);
        renderCalendar();
        // The calendar stays open on the arrival step, which is the point of
        // the button: start again here rather than close and reopen.
        clearButton.blur();
    });
    confirmButton?.addEventListener("click", () => {
        if (arrivalInput.value && departureInput.value) {
            calendar.close();
        }
    });
    calendar.addEventListener("click", (event) => {
        if (event.target === calendar) {
            calendar.close();
        }
    });
    calendar.addEventListener("close", () => {
        document.body.classList.remove("calendar-open");
        returnFocus?.focus();
    });
    mobileCalendar.addEventListener("change", () => {
        if (calendar.open) {
            renderCalendar();
        }
    });
    arrivalInput.addEventListener("change", () => {
        const arrival = parseDate(arrivalInput.value);
        departureInput.min = arrival ? formatDate(addDays(arrival, 1)) : initialDepartureMinimum;
        updateSelectionSummary();
    });
    departureInput.addEventListener("change", updateSelectionSummary);
});

document.querySelectorAll("[data-management-calendar]").forEach((calendar) => {
    const form = calendar.closest("form");
    const kind = calendar.dataset.calendarKind === "single" ? "single" : "range";
    const startInput = kind === "range"
        ? form?.querySelector(`input[name='${calendar.dataset.startInput}']`)
        : null;
    const endInput = form?.querySelector(`input[name='${calendar.dataset.endInput}']`);
    const startTrigger = kind === "range"
        ? form?.querySelector(`[data-management-date-trigger='${calendar.dataset.startInput}']`)
        : null;
    const endTrigger = form?.querySelector(`[data-management-date-trigger='${calendar.dataset.endInput}']`);
    const monthsContainer = calendar.querySelector("[data-management-calendar-months]");
    const heading = calendar.querySelector("[data-management-calendar-heading]");
    const help = calendar.querySelector("[data-management-calendar-help]");
    const startSummary = calendar.querySelector("[data-management-calendar-start]");
    const endSummary = calendar.querySelector("[data-management-calendar-end]");
    const nightsSummary = calendar.querySelector("[data-management-calendar-nights]");
    const previousButton = calendar.querySelector("[data-management-calendar-previous]");
    const nextButton = calendar.querySelector("[data-management-calendar-next]");
    const confirmButton = calendar.querySelector("[data-management-calendar-confirm]");
    const closeButton = calendar.querySelector("[data-management-calendar-close]");
    const mobileCalendar = window.matchMedia("(max-width: 42rem)");

    if (
        !(calendar instanceof HTMLDialogElement)
        || !(endInput instanceof HTMLInputElement)
        || (kind === "range" && !(startInput instanceof HTMLInputElement))
        || !endTrigger
        || (kind === "range" && !startTrigger)
        || !monthsContainer
    ) {
        return;
    }

    const documentLanguage = document.documentElement.lang || "ar";
    // CLDR defaults "ar" to Latin digits, but the rest of the site renders Arabic
    // money and dates with Arabic-Indic digits, so ask for that numbering system.
    const locale = documentLanguage.startsWith("ar")
        ? `${documentLanguage}-u-nu-arab`
        : documentLanguage;
    const chooseLabel = calendar.dataset.chooseLabel || "Choose date";
    const shortDateFormatter = new Intl.DateTimeFormat(locale, { day: "numeric", month: "short" });
    const fullDateFormatter = new Intl.DateTimeFormat(locale, {
        weekday: "long",
        day: "numeric",
        month: "long",
        year: "numeric",
    });
    const monthFormatter = new Intl.DateTimeFormat(locale, { month: "long", year: "numeric" });
    const weekdayFormatter = new Intl.DateTimeFormat(locale, { weekday: "short" });
    const initialEndMinimum = endInput.min;
    let activeField = kind === "single" ? "end" : "start";
    let displayMonth = null;
    let returnFocus = null;

    function parseDate(value) {
        const parts = String(value || "").split("-").map(Number);
        if (parts.length !== 3 || parts.some((part) => !Number.isFinite(part))) {
            return null;
        }
        const parsed = new Date(parts[0], parts[1] - 1, parts[2]);
        return Number.isNaN(parsed.getTime()) ? null : parsed;
    }

    function formatDate(date) {
        const year = date.getFullYear();
        const month = String(date.getMonth() + 1).padStart(2, "0");
        const day = String(date.getDate()).padStart(2, "0");
        return `${year}-${month}-${day}`;
    }

    function startOfMonth(date) {
        return new Date(date.getFullYear(), date.getMonth(), 1);
    }

    function addMonths(date, amount) {
        return new Date(date.getFullYear(), date.getMonth() + amount, 1);
    }

    function addDays(date, amount) {
        return new Date(date.getFullYear(), date.getMonth(), date.getDate() + amount);
    }

    function sameDay(left, right) {
        return Boolean(left && right && formatDate(left) === formatDate(right));
    }

    function dispatchDateChange(input) {
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new Event("change", { bubbles: true }));
    }

    const fixedStart = parseDate(calendar.dataset.fixedStart);
    const earliestDate = parseDate(kind === "range" ? startInput.min : endInput.min) || new Date();
    earliestDate.setHours(0, 0, 0, 0);

    function selectedStart() {
        return kind === "range" ? parseDate(startInput.value) : fixedStart;
    }

    function updateTrigger(trigger, input, label) {
        if (!trigger) {
            return;
        }
        const value = trigger.querySelector("[data-management-date-trigger-value]");
        const selectedDate = parseDate(input.value);
        if (value) {
            value.textContent = selectedDate ? fullDateFormatter.format(selectedDate) : chooseLabel;
        }
        trigger.setAttribute(
            "aria-label",
            selectedDate ? `${label}: ${fullDateFormatter.format(selectedDate)}` : `${label}: ${chooseLabel}`,
        );
        trigger.classList.toggle("has-value", Boolean(selectedDate));
    }

    function updateSelectionSummary() {
        const start = selectedStart();
        const end = parseDate(endInput.value);
        if (kind === "range") {
            updateTrigger(startTrigger, startInput, calendar.dataset.arrivalLabel || "Arrival");
        }
        updateTrigger(endTrigger, endInput, calendar.dataset.departureLabel || "Departure");

        if (startSummary) {
            startSummary.textContent = start ? shortDateFormatter.format(start) : chooseLabel;
        }
        if (endSummary) {
            endSummary.textContent = end ? shortDateFormatter.format(end) : chooseLabel;
        }
        calendar.querySelectorAll("[data-management-calendar-mode]").forEach((button) => {
            button.classList.toggle("is-active", button.dataset.managementCalendarMode === activeField);
        });

        if (start && end && end > start && nightsSummary) {
            const nights = Math.round((end - start) / 86400000);
            const label = nights === 1 ? calendar.dataset.nightLabel : calendar.dataset.nightsLabel;
            nightsSummary.textContent = `${new Intl.NumberFormat(locale).format(nights)} ${label}`;
            nightsSummary.hidden = false;
        } else if (nightsSummary) {
            nightsSummary.hidden = true;
        }

        if (help) {
            if ((kind === "single" && end) || (kind === "range" && start && end)) {
                help.textContent = calendar.dataset.selectedHelp || "";
            } else if (kind === "single") {
                help.textContent = calendar.dataset.extensionHelp || "";
            } else if (activeField === "end") {
                help.textContent = calendar.dataset.departureHelp || "";
            } else {
                help.textContent = calendar.dataset.arrivalHelp || "";
            }
        }
        if (confirmButton) {
            confirmButton.disabled = kind === "single" ? !end : !(start && end && end > start);
        }
    }

    function createMonth(monthDate, start, end) {
        const article = document.createElement("section");
        article.className = "luxury-calendar__month";
        const title = document.createElement("h3");
        title.textContent = monthFormatter.format(monthDate);
        article.append(title);

        const weekdayRow = document.createElement("div");
        weekdayRow.className = "luxury-calendar__weekdays";
        const sunday = new Date(2023, 0, 1);
        for (let index = 0; index < 7; index += 1) {
            const weekday = document.createElement("span");
            weekday.textContent = weekdayFormatter.format(addDays(sunday, index));
            weekdayRow.append(weekday);
        }
        article.append(weekdayRow);

        const days = document.createElement("div");
        days.className = "luxury-calendar__days";
        for (let index = 0; index < monthDate.getDay(); index += 1) {
            const spacer = document.createElement("span");
            spacer.className = "luxury-calendar__day-spacer";
            spacer.setAttribute("aria-hidden", "true");
            days.append(spacer);
        }

        const inputMinimum = parseDate(activeField === "start" ? startInput?.min : endInput.min);
        const inputMaximum = parseDate(activeField === "start" ? startInput?.max : endInput.max);
        const lastDay = new Date(monthDate.getFullYear(), monthDate.getMonth() + 1, 0).getDate();
        for (let day = 1; day <= lastDay; day += 1) {
            const date = new Date(monthDate.getFullYear(), monthDate.getMonth(), day);
            const button = document.createElement("button");
            button.type = "button";
            button.dataset.managementCalendarDate = formatDate(date);
            button.textContent = new Intl.NumberFormat(locale, { useGrouping: false }).format(day);
            button.setAttribute("aria-label", fullDateFormatter.format(date));

            const beforeMinimum = inputMinimum && date < inputMinimum;
            const afterMaximum = inputMaximum && date > inputMaximum;
            const beforeStart = activeField === "end" && start && date <= start;
            button.disabled = Boolean(beforeMinimum || afterMaximum || beforeStart);
            button.classList.toggle("is-today", sameDay(date, new Date()));
            button.classList.toggle("is-arrival", sameDay(date, start));
            button.classList.toggle("is-departure", sameDay(date, end));
            button.classList.toggle("is-in-range", Boolean(start && end && date > start && date < end));
            if (sameDay(date, start) || sameDay(date, end)) {
                button.setAttribute("aria-selected", "true");
            }
            days.append(button);
        }
        article.append(days);
        return article;
    }

    function renderCalendar() {
        const start = selectedStart();
        const end = parseDate(endInput.value);
        if (!displayMonth) {
            displayMonth = startOfMonth((activeField === "end" && end) || start || earliestDate);
        }
        const monthCount = mobileCalendar.matches ? 1 : 2;
        monthsContainer.replaceChildren();
        for (let index = 0; index < monthCount; index += 1) {
            monthsContainer.append(createMonth(addMonths(displayMonth, index), start, end));
        }
        if (heading) {
            heading.textContent = monthCount === 1
                ? monthFormatter.format(displayMonth)
                : `${monthFormatter.format(displayMonth)} — ${monthFormatter.format(addMonths(displayMonth, 1))}`;
        }
        if (previousButton) {
            previousButton.disabled = displayMonth <= startOfMonth(earliestDate);
        }
        updateSelectionSummary();
    }

    function openCalendar(field, trigger) {
        const start = selectedStart();
        const end = parseDate(endInput.value);
        activeField = kind === "single" ? "end" : field;
        displayMonth = startOfMonth((activeField === "end" && (end || start)) || start || earliestDate);
        returnFocus = trigger;
        renderCalendar();
        calendar.showModal();
        document.body.classList.add("calendar-open");
        closeButton?.focus();
    }

    startTrigger?.removeAttribute("hidden");
    endTrigger.removeAttribute("hidden");
    form.classList.add("has-management-calendar");
    if (startInput?.value) {
        const initialStart = parseDate(startInput.value);
        if (initialStart) {
            endInput.min = formatDate(addDays(initialStart, 1));
        }
    }
    updateSelectionSummary();

    startTrigger?.addEventListener("click", () => openCalendar("start", startTrigger));
    endTrigger.addEventListener("click", () => openCalendar("end", endTrigger));
    calendar.querySelectorAll("[data-management-calendar-mode]").forEach((button) => {
        button.addEventListener("click", () => {
            if (button.disabled) {
                return;
            }
            activeField = button.dataset.managementCalendarMode;
            const selected = activeField === "end" ? (parseDate(endInput.value) || selectedStart()) : selectedStart();
            displayMonth = startOfMonth(selected || earliestDate);
            renderCalendar();
        });
    });
    monthsContainer.addEventListener("click", (event) => {
        const button = event.target.closest("[data-management-calendar-date]");
        if (!button || button.disabled) {
            return;
        }
        const selectedDate = parseDate(button.dataset.managementCalendarDate);
        if (!selectedDate) {
            return;
        }
        if (activeField === "start" && startInput) {
            startInput.value = formatDate(selectedDate);
            endInput.min = formatDate(addDays(selectedDate, 1));
            const end = parseDate(endInput.value);
            if (end && end <= selectedDate) {
                endInput.value = "";
                dispatchDateChange(endInput);
            }
            dispatchDateChange(startInput);
            activeField = "end";
        } else {
            endInput.value = formatDate(selectedDate);
            dispatchDateChange(endInput);
        }
        displayMonth = startOfMonth(selectedDate);
        renderCalendar();
    });
    previousButton?.addEventListener("click", () => {
        displayMonth = addMonths(displayMonth || earliestDate, -1);
        renderCalendar();
    });
    nextButton?.addEventListener("click", () => {
        displayMonth = addMonths(displayMonth || earliestDate, 1);
        renderCalendar();
    });
    closeButton?.addEventListener("click", () => calendar.close());
    confirmButton?.addEventListener("click", () => {
        if (endInput.value && (kind === "single" || startInput.value)) {
            calendar.close();
        }
    });
    calendar.addEventListener("click", (event) => {
        if (event.target === calendar) {
            calendar.close();
        }
    });
    calendar.addEventListener("close", () => {
        document.body.classList.remove("calendar-open");
        returnFocus?.focus();
    });
    mobileCalendar.addEventListener("change", () => {
        if (calendar.open) {
            renderCalendar();
        }
    });
    startInput?.addEventListener("change", () => {
        const start = parseDate(startInput.value);
        endInput.min = start ? formatDate(addDays(start, 1)) : initialEndMinimum;
        updateSelectionSummary();
    });
    endInput.addEventListener("change", updateSelectionSummary);
});

document.querySelectorAll("[data-guest-stepper]").forEach((stepper) => {
    const input = stepper.querySelector("[data-guest-stepper-input]");
    const decreaseButton = stepper.querySelector("[data-guest-stepper-decrease]");
    const increaseButton = stepper.querySelector("[data-guest-stepper-increase]");
    if (!(input instanceof HTMLInputElement) || !decreaseButton || !increaseButton) {
        return;
    }

    const minimum = Number(input.min || 1);
    const maximum = Number(input.max || Number.MAX_SAFE_INTEGER);
    function currentValue() {
        const value = Number(input.value);
        return Number.isFinite(value) ? value : minimum;
    }
    function updateButtons() {
        const value = currentValue();
        decreaseButton.disabled = value <= minimum;
        increaseButton.disabled = value >= maximum;
    }
    function changeBy(amount) {
        input.value = String(Math.min(maximum, Math.max(minimum, currentValue() + amount)));
        input.dispatchEvent(new Event("input", { bubbles: true }));
        input.dispatchEvent(new Event("change", { bubbles: true }));
        input.focus();
    }

    decreaseButton.addEventListener("click", () => changeBy(-1));
    increaseButton.addEventListener("click", () => changeBy(1));
    input.addEventListener("input", updateButtons);
    input.addEventListener("change", updateButtons);
    updateButtons();
});

document.querySelectorAll("[data-submit-once]").forEach((form) => {
    form.addEventListener("submit", (event) => {
        if (event.defaultPrevented || !form.checkValidity()) {
            return;
        }
        if (form.dataset.submitting === "true") {
            return;
        }
        form.dataset.submitting = "true";
        const loading = form.querySelector(".availability-form__loading");
        const submit = form.querySelector("button[type='submit']");
        if (loading) {
            loading.hidden = false;
        }
        if (submit) {
            submit.disabled = true;
            submit.setAttribute("aria-busy", "true");
        }
    });
});

document.querySelectorAll("[data-country-select]").forEach((select) => {
    if (!(select instanceof HTMLSelectElement) || typeof Intl.DisplayNames !== "function") {
        return;
    }
    try {
        const language = document.documentElement.lang || "ar";
        const regionNames = new Intl.DisplayNames([language], { type: "region" });
        Array.from(select.options).forEach((option) => {
            if (/^[A-Z]{2}$/.test(option.value)) {
                const localizedName = regionNames.of(option.value);
                if (localizedName) {
                    option.textContent = `${localizedName} (${option.value})`;
                }
            }
        });
    } catch (_error) {
        // ISO codes remain a valid, accessible fallback in older browsers.
    }
});

document.querySelectorAll("[data-guest-journey]").forEach((form) => {
    const panels = Array.from(form.querySelectorAll("[data-journey-panel]"));
    const indicators = Array.from(form.querySelectorAll("[data-journey-indicator]"));
    const nextButton = form.querySelector("[data-journey-next]");
    const backButton = form.querySelector("[data-journey-back]");
    let currentStep = Number(form.dataset.initialStep) === 2 ? 2 : 1;

    function controlsFor(panel) {
        return Array.from(panel?.querySelectorAll("input, select, textarea") || []).filter(
            (field) => !field.disabled && field.type !== "hidden",
        );
    }

    function showStep(step, { focus = false } = {}) {
        currentStep = step === 2 ? 2 : 1;
        form.dataset.currentStep = String(currentStep);
        panels.forEach((panel) => {
            const active = panel.dataset.journeyPanel === String(currentStep);
            panel.classList.toggle("is-active", active);
            panel.hidden = !active;
        });
        indicators.forEach((indicator) => {
            const active = indicator.dataset.journeyIndicator === String(currentStep);
            indicator.classList.toggle("is-active", active);
            if (active) {
                indicator.setAttribute("aria-current", "step");
            } else {
                indicator.removeAttribute("aria-current");
            }
        });
        if (focus) {
            const heading = form.querySelector(
                `[data-journey-panel='${currentStep}'] h3`,
            );
            heading?.setAttribute("tabindex", "-1");
            heading?.focus({ preventScroll: true });
            form.querySelector(".guest-journey__progress")?.scrollIntoView({
                behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
                    ? "auto"
                    : "smooth",
                block: "start",
            });
        }
    }

    function firstInvalid(panel) {
        return controlsFor(panel).find((field) => field.willValidate && !field.checkValidity());
    }

    function revealInvalid(field) {
        if (!field) {
            return;
        }
        field.closest("details")?.setAttribute("open", "");
        field.closest("[data-journey-field]")?.classList.add("form-field--client-error");
        field.setAttribute("aria-invalid", "true");
        field.reportValidity();
        field.focus({ preventScroll: true });
        field.scrollIntoView({ behavior: "smooth", block: "center" });
    }

    controlsFor(form).forEach((field) => {
        field.addEventListener("input", () => {
            if (field.checkValidity()) {
                field.closest("[data-journey-field]")?.classList.remove(
                    "form-field--client-error",
                );
                field.removeAttribute("aria-invalid");
            }
        });
    });

    form.classList.add("is-enhanced");
    if (nextButton) {
        nextButton.hidden = false;
        nextButton.addEventListener("click", () => {
            const guestPanel = form.querySelector("[data-journey-panel='1']");
            const invalid = firstInvalid(guestPanel);
            if (invalid) {
                revealInvalid(invalid);
                return;
            }
            showStep(2, { focus: true });
        });
    }
    if (backButton) {
        backButton.hidden = false;
        backButton.addEventListener("click", () => showStep(1, { focus: true }));
    }

    form.addEventListener("submit", (event) => {
        const guestPanel = form.querySelector("[data-journey-panel='1']");
        const billingPanel = form.querySelector("[data-journey-panel='2']");
        const invalidGuest = firstInvalid(guestPanel);
        const invalidBilling = firstInvalid(billingPanel);
        const invalid = invalidGuest || invalidBilling;
        if (!invalid) {
            return;
        }
        event.preventDefault();
        showStep(invalidGuest ? 1 : 2);
        window.requestAnimationFrame(() => revealInvalid(invalid));
    });

    showStep(currentStep);
});

document.querySelectorAll("[data-card-image]").forEach((image) => {
    const fallback = image.parentElement?.querySelector("[data-image-error]");
    const showFallback = () => {
        image.hidden = true;
        if (fallback) {
            fallback.hidden = false;
        }
    };

    image.addEventListener("error", showFallback, { once: true });
    if (image.complete && image.naturalWidth === 0) {
        showFallback();
    }
});

document.querySelectorAll("[data-card-gallery]").forEach((gallery) => {
    const slides = Array.from(gallery.querySelectorAll("[data-card-slide]"));
    const dots = Array.from(gallery.querySelectorAll("[data-card-dot]"));
    const current = gallery.querySelector("[data-card-current]");
    const previous = gallery.querySelector("[data-card-previous]");
    const next = gallery.querySelector("[data-card-next]");
    let activeIndex = 0;
    let pointerStart = null;
    let suppressSlideClick = false;

    if (slides.length < 2) {
        return;
    }

    function showSlide(index) {
        activeIndex = (index + slides.length) % slides.length;
        slides.forEach((slide, slideIndex) => {
            const isActive = slideIndex === activeIndex;
            slide.classList.toggle("is-active", isActive);
            slide.setAttribute("aria-hidden", String(!isActive));
            slide.tabIndex = isActive ? 0 : -1;
        });
        dots.forEach((dot, dotIndex) => {
            dot.classList.toggle("is-active", dotIndex === activeIndex);
        });
        if (current) {
            current.textContent = String(activeIndex + 1);
        }
    }

    previous?.addEventListener("click", () => showSlide(activeIndex - 1));
    next?.addEventListener("click", () => showSlide(activeIndex + 1));

    gallery.addEventListener("pointerdown", (event) => {
        if (event.pointerType !== "mouse") {
            pointerStart = event.clientX;
        }
    });
    gallery.addEventListener("pointerup", (event) => {
        if (pointerStart === null) {
            return;
        }
        const distance = event.clientX - pointerStart;
        pointerStart = null;
        if (Math.abs(distance) < 45) {
            return;
        }
        suppressSlideClick = true;
        const direction = document.documentElement.dir === "rtl" ? -1 : 1;
        showSlide(activeIndex + (distance < 0 ? direction : -direction));
    });
    gallery.addEventListener("pointercancel", () => {
        pointerStart = null;
    });
    gallery.addEventListener("click", (event) => {
        if (suppressSlideClick && event.target.closest("[data-card-slide]")) {
            event.preventDefault();
            suppressSlideClick = false;
        }
    }, true);
});

const lightbox = document.querySelector("dialog[data-lightbox]");
if (lightbox instanceof HTMLDialogElement) {
    const dataElement = lightbox.querySelector("[data-lightbox-data]");
    const imageElement = lightbox.querySelector("[data-lightbox-image]");
    const captionElement = lightbox.querySelector("[data-lightbox-caption]");
    const closeButton = lightbox.querySelector("[data-lightbox-close]");
    const previousButton = lightbox.querySelector("[data-lightbox-prev]");
    const nextButton = lightbox.querySelector("[data-lightbox-next]");
    const currentElement = lightbox.querySelector("[data-lightbox-current]");
    const totalElement = lightbox.querySelector("[data-lightbox-total]");
    const progressElement = lightbox.querySelector("[data-lightbox-progress]");
    let images = [];
    let currentIndex = 0;
    let lightboxReturnFocus = null;
    let lightboxPointerStart = null;

    try {
        images = JSON.parse(dataElement?.textContent || "[]");
    } catch {
        images = [];
    }

    function showImage(index) {
        if (!images.length || !imageElement || !captionElement) {
            return;
        }
        currentIndex = (index + images.length) % images.length;
        const selected = images[currentIndex];
        imageElement.src = selected.src;
        imageElement.alt = selected.alt || "";
        captionElement.textContent = selected.caption || "";
        if (currentElement) {
            currentElement.textContent = String(currentIndex + 1);
        }
        if (totalElement) {
            totalElement.textContent = String(images.length);
        }
        if (progressElement) {
            progressElement.style.setProperty("--lightbox-progress", `${((currentIndex + 1) / images.length) * 100}%`);
        }
    }

    document.querySelectorAll("[data-lightbox-open]").forEach((button) => {
        button.addEventListener("click", () => {
            lightboxReturnFocus = button;
            showImage(Number(button.dataset.lightboxOpen || 0));
            lightbox.showModal();
            document.body.classList.add("nav-open");
            closeButton?.focus();
        });
    });

    previousButton?.addEventListener("click", () => showImage(currentIndex - 1));
    nextButton?.addEventListener("click", () => showImage(currentIndex + 1));
    lightbox.addEventListener("pointerdown", (event) => {
        if (event.pointerType !== "mouse") {
            lightboxPointerStart = event.clientX;
        }
    });
    lightbox.addEventListener("pointerup", (event) => {
        if (lightboxPointerStart === null) {
            return;
        }
        const distance = event.clientX - lightboxPointerStart;
        lightboxPointerStart = null;
        if (Math.abs(distance) < 45) {
            return;
        }
        const direction = document.documentElement.dir === "rtl" ? -1 : 1;
        showImage(currentIndex + (distance < 0 ? direction : -direction));
    });
    lightbox.addEventListener("pointercancel", () => {
        lightboxPointerStart = null;
    });
    closeButton?.addEventListener("click", () => lightbox.close());
    lightbox.addEventListener("close", () => {
        document.body.classList.remove("nav-open");
        lightboxReturnFocus?.focus();
    });
    lightbox.addEventListener("click", (event) => {
        if (event.target === lightbox) {
            lightbox.close();
        }
    });
    lightbox.addEventListener("keydown", (event) => {
        if (event.key === "ArrowLeft") {
            showImage(document.documentElement.dir === "rtl" ? currentIndex + 1 : currentIndex - 1);
        }
        if (event.key === "ArrowRight") {
            showImage(document.documentElement.dir === "rtl" ? currentIndex - 1 : currentIndex + 1);
        }
        if (event.key === "Tab") {
            const controls = [closeButton, previousButton, nextButton].filter(Boolean);
            const first = controls[0];
            const last = controls[controls.length - 1];
            if (event.shiftKey && document.activeElement === first) {
                event.preventDefault();
                last.focus();
            } else if (!event.shiftKey && document.activeElement === last) {
                event.preventDefault();
                first.focus();
            }
        }
    });
}

// Checkout payment-method tabs. Both HyperPay widgets stay mounted so the hosted
// iframes are never torn down; only their panels are shown or hidden.
const checkoutMethods = document.querySelectorAll("[data-checkout-method]");
if (checkoutMethods.length) {
    const panels = document.querySelectorAll("[data-checkout-panel]");

    function selectMethod(name, { focusTab = false } = {}) {
        checkoutMethods.forEach((tab) => {
            const isActive = tab.dataset.checkoutMethod === name;
            tab.classList.toggle("is-active", isActive);
            tab.setAttribute("aria-selected", isActive ? "true" : "false");
            tab.tabIndex = isActive ? 0 : -1;
            if (isActive && focusTab) {
                tab.focus();
            }
        });
        panels.forEach((panel) => {
            panel.hidden = panel.dataset.checkoutPanel !== name;
        });
    }

    checkoutMethods.forEach((tab) => {
        tab.addEventListener("click", () => selectMethod(tab.dataset.checkoutMethod));
        tab.addEventListener("keydown", (event) => {
            const keys = ["ArrowRight", "ArrowLeft", "Home", "End"];
            if (!keys.includes(event.key)) {
                return;
            }
            event.preventDefault();
            const tabs = [...checkoutMethods];
            const current = tabs.indexOf(tab);
            // Arrow keys follow reading order, which is mirrored in RTL.
            const forward = document.documentElement.dir === "rtl" ? "ArrowLeft" : "ArrowRight";
            let next = current;
            if (event.key === "Home") {
                next = 0;
            } else if (event.key === "End") {
                next = tabs.length - 1;
            } else {
                next = event.key === forward ? current + 1 : current - 1;
                next = (next + tabs.length) % tabs.length;
            }
            selectMethod(tabs[next].dataset.checkoutMethod, { focusTab: true });
        });
    });
}

// Floating WhatsApp button. It keeps clear of the consent banner, which shares
// the bottom of the viewport and can wrap to several lines on narrow screens.
const whatsappFab = document.querySelector("[data-whatsapp-fab]");
if (whatsappFab) {
    const consentBanner = document.querySelector("[data-consent-banner]");

    if (consentBanner) {
        const syncOffset = () => {
            const clear = consentBanner.hidden ? 0 : consentBanner.offsetHeight + 12;
            whatsappFab.style.setProperty("--whatsapp-fab-offset", `${clear}px`);
        };

        syncOffset();
        new MutationObserver(syncOffset).observe(consentBanner, {
            attributes: true,
            attributeFilter: ["hidden"],
        });
        if ("ResizeObserver" in window) {
            new ResizeObserver(syncOffset).observe(consentBanner);
        } else {
            window.addEventListener("resize", syncOffset);
        }
    }
}

// Password visibility toggles on the account forms. The button ships pressed=false
// so a scripted reveal never leaves the field readable without the control saying so.
document.querySelectorAll("[data-password-toggle]").forEach((toggle) => {
    const field = toggle.closest(".auth-password")?.querySelector("input");
    if (!field) {
        return;
    }

    toggle.addEventListener("click", () => {
        const revealed = field.type === "text";
        field.type = revealed ? "password" : "text";
        toggle.setAttribute("aria-pressed", revealed ? "false" : "true");
        const label = revealed ? toggle.dataset.showLabel : toggle.dataset.hideLabel;
        if (label) {
            toggle.setAttribute("aria-label", label);
        }
    });
});

// Third-party hero imagery is outside our control. When one fails the parent
// keeps a branded gradient instead of a broken-image glyph, and the alt text
// still reaches assistive technology through the parent's label.
document.querySelectorAll("[data-image-fallback] img").forEach((image) => {
    const markUnavailable = () => image.classList.add("is-unavailable");
    if (image.complete && image.naturalWidth === 0) {
        markUnavailable();
        return;
    }
    image.addEventListener("error", markUnavailable, { once: true });
});

// Billing region: Saudi Arabia has a closed list of thirteen, every other
// country a free-text field. Both are in the DOM so the form still works
// without JavaScript; this only hides the one that does not apply.
document.querySelectorAll("form").forEach((form) => {
    const country = form.querySelector("[data-country-select]");
    const saudiGroup = form.querySelector('[data-region-group="SA"]');
    const otherGroup = form.querySelector('[data-region-group="other"]');
    if (!country || !saudiGroup || !otherGroup) {
        return;
    }

    function applyCountry() {
        const isSaudi = country.value === "SA";
        saudiGroup.hidden = !isSaudi;
        otherGroup.hidden = isSaudi;
    }

    country.addEventListener("change", applyCountry);
    applyCountry();
});

// Draft autosave for the guest details step. Kept in sessionStorage so a guest
// who steps back, or reloads after a validation error, does not retype the
// address. Nothing is written to the server, and the draft is dropped as soon
// as the form is submitted.
document.querySelectorAll("[data-draft-form]").forEach((form) => {
    const key = `lsa-draft:${form.dataset.draftForm}`;
    const fields = Array.from(
        form.querySelectorAll("input[name], select[name], textarea[name]")
    ).filter((field) => !["hidden", "password", "checkbox", "radio"].includes(field.type));

    function read() {
        try {
            return JSON.parse(window.sessionStorage.getItem(key) || "{}");
        } catch {
            return {};
        }
    }

    const saved = read();
    fields.forEach((field) => {
        if (!field.value && typeof saved[field.name] === "string") {
            field.value = saved[field.name];
            field.dispatchEvent(new Event("change", { bubbles: true }));
        }
    });

    form.addEventListener("input", () => {
        const draft = {};
        fields.forEach((field) => {
            if (field.value) {
                draft[field.name] = field.value;
            }
        });
        try {
            window.sessionStorage.setItem(key, JSON.stringify(draft));
        } catch {
            // A full or disabled store simply means no draft is kept.
        }
    });

    form.addEventListener("submit", () => {
        try {
            window.sessionStorage.removeItem(key);
        } catch {
            // Nothing to clean up when storage is unavailable.
        }
    });
});
