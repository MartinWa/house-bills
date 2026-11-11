"use strict";

(() => {
  const update = () => {
    for (const select of document.querySelectorAll('select[name$="subcategory"]')) {
      const categoryName = select.name.replace(/subcategory$/, "category");
      const utilityName = select.name.replace(/subcategory$/, "utility");
      const category = document.getElementsByName(categoryName)[0] || document.getElementsByName(utilityName)[0];
      if (!category) continue;
      for (const option of select.options) {
        const unavailable = Boolean(option.value && option.dataset.category !== category.value);
        option.hidden = unavailable;
        option.disabled = unavailable;
      }
      if (select.selectedOptions[0]?.disabled) select.value = "";
    }
  };
  const initialize = () => {
    document.addEventListener("change", update);
    document.addEventListener("formset:added", update);
    new MutationObserver(update).observe(document.body, {childList: true, subtree: true});
    update();
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, {once: true});
  } else {
    initialize();
  }
})();
