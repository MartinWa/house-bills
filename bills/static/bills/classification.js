"use strict";

(() => {
  const updateSelect = (select, scope) => {
    const categoryName = select.name.replace(/subcategory$/, "category");
    const utilityName = select.name.replace(/subcategory$/, "utility");
    const category = scope.querySelector(`select[name="${CSS.escape(categoryName)}"], select[name="${CSS.escape(utilityName)}"]`);
    if (!category) return;
    for (const option of select.options) {
      const unavailable = Boolean(option.value && option.dataset.category !== category.value);
      option.hidden = unavailable;
      option.disabled = unavailable;
    }
    if (select.selectedOptions[0]?.disabled) select.value = "";
  };
  const update = scope => {
    for (const select of scope.querySelectorAll('select[name$="subcategory"]')) updateSelect(select, scope);
  };
  const initialize = () => {
    document.addEventListener("change", event => {
      const category = event.target;
      if (!category.matches('select[name$="category"], select[name$="utility"]') || category.name.endsWith("subcategory")) return;
      const scope = category.form;
      if (!scope) return;
      const name = category.name.replace(/(?:category|utility)$/, "subcategory");
      const select = scope.querySelector(`select[name="${CSS.escape(name)}"]`);
      if (select) updateSelect(select, scope);
    });
    document.addEventListener("formset:added", event => update(event.target));
    document.addEventListener("review:row-added", event => update(event.target));
    update(document);
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initialize, {once: true});
  } else {
    initialize();
  }
})();
