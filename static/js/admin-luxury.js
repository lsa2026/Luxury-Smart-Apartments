(function () {
  "use strict";

  const command = document.querySelector("[data-lsa-command]");
  if (command) {
    const search = command.querySelector("[data-lsa-command-search]");
    const items = Array.from(command.querySelectorAll("[data-lsa-command-item]"));
    const empty = command.querySelector("[data-lsa-command-empty]");
    const closeButtons = command.querySelectorAll("[data-lsa-command-close]");

    const filterItems = () => {
      const query = (search.value || "").trim().toLocaleLowerCase("ar");
      let visible = 0;
      items.forEach((item) => {
        const matches = !query || item.textContent.toLocaleLowerCase("ar").includes(query);
        item.hidden = !matches;
        visible += matches ? 1 : 0;
      });
      empty.hidden = visible !== 0;
    };

    command.addEventListener("toggle", () => {
      document.documentElement.classList.toggle("lsa-command-open", command.open);
      if (command.open) {
        window.setTimeout(() => search.focus(), 40);
      } else {
        search.value = "";
        filterItems();
      }
    });
    search.addEventListener("input", filterItems);
    closeButtons.forEach((button) => button.addEventListener("click", () => {
      command.open = false;
    }));
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && command.open) {
        command.open = false;
      }
      if ((event.ctrlKey || event.metaKey) && event.key.toLocaleLowerCase() === "k") {
        event.preventDefault();
        command.open = !command.open;
      }
    });
  }

  const clock = document.querySelector("[data-lsa-clock]");
  if (clock) {
    const updateClock = () => {
      clock.textContent = new Intl.DateTimeFormat("ar-SA", {
        hour: "numeric",
        minute: "2-digit",
        weekday: "long",
        day: "numeric",
        month: "long",
      }).format(new Date());
    };
    updateClock();
    window.setInterval(updateClock, 60000);
  }

  document.querySelectorAll(".lsa-user-menu").forEach((menu) => {
    menu.addEventListener("toggle", () => {
      if (!menu.open) return;
      document.querySelectorAll(".lsa-user-menu[open]").forEach((other) => {
        if (other !== menu) other.open = false;
      });
    });
  });
})();

/* Image alternative text editor: fill empty fields from the property name. */
(function () {
  "use strict";
  const form = document.querySelector("form[data-name-ar]");
  if (!form) return;

  form.querySelectorAll("[data-alt-fill]").forEach((button) => {
    button.addEventListener("click", () => {
      const lang = button.getAttribute("data-alt-fill");
      const base = (form.getAttribute("data-name-" + lang) || "").trim();
      if (!base) return;
      const inputs = form.querySelectorAll('input[data-alt-lang="' + lang + '"]');
      let index = 0;
      inputs.forEach((input) => {
        index += 1;
        if (input.value.trim()) return;
        input.value = index === 1 ? base : base + " — " + index;
      });
    });
  });
})();
