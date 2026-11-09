/* The application server caches GitHub metadata; browsers only contact this origin. */
(() => {
  "use strict";
  const link = document.querySelector("[data-release-status-url]");
  if (!link) return;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  fetch(link.dataset.releaseStatusUrl, {
    credentials: "same-origin",
    headers: { "Accept": "application/json" },
    signal: controller.signal,
  }).then(response => response.ok ? response.json() : null).then(status => {
    if (status?.update_available === true) {
      link.querySelector("[data-update-indicator]").hidden = false;
    }
  }).catch(() => {
    // Version and release links remain available when the check cannot complete.
  }).finally(() => clearTimeout(timeout));
})();
