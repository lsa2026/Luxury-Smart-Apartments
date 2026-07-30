"use strict";

document.documentElement.classList.add("js");

const menuToggle = document.querySelector("[data-menu-toggle]");
const mobileNavigation = document.querySelector("[data-mobile-nav]");
let menuReturnFocus = null;

function closeMenu() {
    if (!menuToggle || !mobileNavigation || mobileNavigation.hidden) {
        return;
    }
    mobileNavigation.hidden = true;
    menuToggle.setAttribute("aria-expanded", "false");
    document.body.classList.remove("nav-open");
    if (menuReturnFocus) {
        menuReturnFocus.focus();
    }
}

if (menuToggle && mobileNavigation) {
    menuToggle.addEventListener("click", () => {
        const willOpen = mobileNavigation.hidden;
        mobileNavigation.hidden = !willOpen;
        menuToggle.setAttribute("aria-expanded", String(willOpen));
        document.body.classList.toggle("nav-open", willOpen);
        if (willOpen) {
            menuReturnFocus = menuToggle;
            mobileNavigation.querySelector("a")?.focus();
        }
    });
    mobileNavigation.addEventListener("click", (event) => {
        if (event.target === mobileNavigation) {
            closeMenu();
        }
    });
}

document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
        closeMenu();
    }
});

document.querySelectorAll("[data-language-select]").forEach((select) => {
    select.addEventListener("change", () => select.form?.requestSubmit());
});

document.querySelectorAll("[data-submit-once]").forEach((form) => {
    form.addEventListener("submit", () => {
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

const lightbox = document.querySelector("dialog[data-lightbox]");
if (lightbox instanceof HTMLDialogElement) {
    const dataElement = lightbox.querySelector("[data-lightbox-data]");
    const imageElement = lightbox.querySelector("[data-lightbox-image]");
    const captionElement = lightbox.querySelector("[data-lightbox-caption]");
    const closeButton = lightbox.querySelector("[data-lightbox-close]");
    const previousButton = lightbox.querySelector("[data-lightbox-prev]");
    const nextButton = lightbox.querySelector("[data-lightbox-next]");
    let images = [];
    let currentIndex = 0;
    let lightboxReturnFocus = null;

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
