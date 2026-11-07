"use strict";

(() => {
  const form = document.getElementById("review-form");
  if (!form) return;
  const allocations = () => Array.from(form.querySelectorAll('[data-formset="sections"] [data-review-row]')).flatMap((row, index) => {
    if (row.querySelector('[name$="-DELETE"]').checked) return [];
    const key = row.querySelector('[name$="-section_id"]').value;
    const property = row.querySelector('[name$="-property"]');
    const service = row.querySelector('[name$="-service"]');
    const house = property.selectedOptions[0]?.textContent || "Choose property";
    const title = service.selectedOptions[0]?.textContent || "Choose service";
    return [[key, `Allocation ${index + 1} · ${house} · ${title}`]];
  });
  const updateChoices = () => {
    const choices = allocations();
    for (const select of form.querySelectorAll('[name$="-section"], [name$="-scope"]')) {
      const selected = select.value;
      const header = select.name.endsWith("-scope");
      select.replaceChildren(new Option(header ? "Invoice" : "Choose an allocation", header ? "invoice" : ""));
      for (const [key, label] of choices) select.add(new Option(label, key));
      // Retain a removed assignment visibly until its dependent row is corrected/deleted.
      if (selected && selected !== "invoice" && !choices.some(([key]) => key === selected)) {
        select.add(new Option("Removed allocation", selected));
      }
      select.value = selected;
    }
  };
  for (const group of form.querySelectorAll("[data-formset]")) {
    group.querySelector("[data-add-row]").addEventListener("click", event => {
      event.preventDefault();
      const name = group.dataset.formset;
      const total = group.querySelector(`[name="${name}-TOTAL_FORMS"]`);
      const index = Number(total.value);
      if (index >= Number(group.dataset.limit)) {
        group.querySelector("[data-formset-status]").textContent = "Row limit reached. Check the existing entries.";
        return;
      }
      const template = group.querySelector("[data-empty-form]");
      const holder = document.createElement("template");
      holder.innerHTML = template.innerHTML.replaceAll("__prefix__", String(index));
      const row = holder.content.querySelector("[data-review-row]");
      row.querySelector("[data-row-number]").textContent = index + 1;
      if (name === "sections") row.querySelector('[name$="-section_id"]').value = `manual-added-${index}`;
      group.querySelector("[data-formset-rows]").append(holder.content);
      total.value = index + 1;
      updateChoices();
      const assignment = row.querySelector('[name$="-section"]');
      if (assignment && allocations().length === 1) assignment.value = allocations()[0][0];
      row.querySelector("input:not([type=hidden]), select, textarea")?.focus();
    });
  }
  form.addEventListener("change", event => {
    if (event.target.name?.endsWith("-DELETE")) {
      event.target.closest("[data-review-row]").classList.toggle("is-removed", event.target.checked);
    }
    updateChoices();
  });
  updateChoices();
})();
