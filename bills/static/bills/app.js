"use strict";

(() => {
  for (const form of document.querySelectorAll("[data-direct-upload]")) {
    const input = form.querySelector('input[type="file"]');
    const button = form.querySelector("[data-upload-button]");
    const status = form.querySelector("[data-upload-status]");
    const buttonContents = button.innerHTML;
    form.querySelector("[data-upload-field]").hidden = true;
    input.required = false;
    window.addEventListener("pageshow", () => {
      input.value = "";
      button.disabled = false;
      button.innerHTML = buttonContents;
      status.textContent = "";
      form.removeAttribute("aria-busy");
    });
    button.addEventListener("click", event => {
      event.preventDefault();
      input.click();
    });
    input.addEventListener("change", () => {
      if (input.files.length) form.requestSubmit();
    });
    form.addEventListener("submit", event => {
      if (!input.files.length) {
        event.preventDefault();
        input.click();
        return;
      }
      button.disabled = true;
      button.textContent = "Uploading…";
      status.textContent = `Uploading ${input.files[0].name}…`;
      form.setAttribute("aria-busy", "true");
    });
  }

  for (const form of document.querySelectorAll("[data-auto-filter]")) {
    let timer;
    const start = form.elements.namedItem("date_from");
    const end = form.elements.namedItem("date_to");
    const validateDates = () => {
      if (start && end) {
        end.setCustomValidity(start.value && end.value && end.value < start.value
          ? "End date must be on or after start date." : "");
      }
    };
    form.addEventListener("input", () => {
      clearTimeout(timer);
      validateDates();
    });
    form.addEventListener("change", () => {
      clearTimeout(timer);
      validateDates();
      if (!form.reportValidity()) return;
      timer = setTimeout(() => form.requestSubmit(), 250);
    });
    form.addEventListener("submit", event => {
      clearTimeout(timer);
      validateDates();
      if (!form.reportValidity()) {
        event.preventDefault();
        return;
      }
      form.querySelector("[data-filter-status]").textContent = "Updating filters…";
    });
  }

  const userMenu = document.querySelector(".user-menu");
  if (userMenu) {
    document.addEventListener("click", event => {
      if (!userMenu.contains(event.target)) userMenu.open = false;
    });
    document.addEventListener("keydown", event => {
      if (event.key === "Escape" && userMenu.open) {
        userMenu.open = false;
        userMenu.querySelector("summary").focus();
      }
    });
  }
})();
