"use strict";

document.documentElement.classList.add("js");

document.querySelectorAll("[data-availability-form]").forEach((form) => {
    form.addEventListener("submit", () => {
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
