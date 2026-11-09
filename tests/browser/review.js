async (page, artifacts = ".playwright-cli", readFixture = null) => {
  const base = new URL(page.url()).origin;
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const text = async (value, target = page) => assert((await target.locator("body").innerText()).includes(value), `Missing text: ${value}`);
  const fill = async (name, value, target = page) => target.locator(`[name="${name}"]`).fill(value);
  const select = async (name, value, target = page) => {
    await target.waitForLoadState("load");
    const control = target.locator(`[name="${name}"]`);
    const automatic = await control.evaluate(element => !!element.closest("[data-auto-filter]"));
    const selected = await control.selectOption(value);
    if (automatic) await target.waitForURL(url => url.searchParams.get(name) === selected[0]);
  };
  const filterDates = async (start, end) => {
    await page.waitForLoadState("load");
    await fill("date_from", start);
    await fill("date_to", end);
    await page.locator('[name="date_to"]').blur();
    await page.waitForURL(url => url.searchParams.get("date_from") === start && url.searchParams.get("date_to") === end);
  };
  const submit = async (name, status = 302, target = page) => {
    const response = target.waitForResponse(response => response.request().method() === "POST" && new URL(response.url()).origin === base);
    await target.getByRole("button", { name, exact: true }).click();
    assert((await response).status() === status, `Unexpected save status for ${name}`);
    await target.waitForLoadState("domcontentloaded");
  };
  const openReview = async () => {
    await page.getByRole("link", { name: "Open document", exact: true }).click();
    await page.getByRole("link", { name: "Review invoice", exact: true }).click();
  };

  await page.goto(`${base}/login/`);
  await page.getByLabel("Username").fill("synthetic-browser-reviewer");
  await page.getByLabel("Password").fill("synthetic-browser-password");
  await submit("Sign in");
  assert(new URL(page.url()).pathname === "/invoices/", "Sign-in did not open Invoices");
  assert(await page.getByRole("navigation", { name: "Main", exact: true }).getByRole("link").count() === 3, "Main navigation still contains Home or Upload");
  assert((await page.getByRole("navigation", { name: "Main", exact: true }).getByRole("link").allTextContents()).join(",") === "Invoices,Documents,Reports", "Main navigation order is incorrect");
  await page.locator(".user-menu summary").click();
  assert(await page.getByRole("link", { name: "Administration", exact: true }).isVisible(), "Administration is missing from the User menu");
  await page.keyboard.press("Escape");
  assert(!await page.locator(".user-menu").evaluate(element => element.open), "Escape did not close the User menu");
  for (const [name, slug] of [["Browser House One", "browser-house-one"], ["Browser House Two", "browser-house-two"]]) {
    await page.goto(`${base}/admin/bills/property/add/`);
    await page.getByLabel("Name:", { exact: true }).fill(name);
    await page.getByLabel("Name:", { exact: true }).press("Tab");
    await page.getByLabel("Slug:", { exact: true }).fill(slug);
    await submit("Save");
    await text(name);
  }
  await page.goto(`${base}/admin/bills/vendor/add/`);
  await page.getByLabel("Name:", { exact: true }).fill("Browser Utilities");
  await page.getByLabel("Name:", { exact: true }).press("Tab");
  await page.getByLabel("Slug:", { exact: true }).fill("browser-utilities");
  await select("default_property", { label: "Browser House One" });
  const houseOne = await page.locator('[name="default_property"]').inputValue();
  await select("issuer_identifiers-0-kind", "vat");
  await fill("issuer_identifiers-0-value", "SE000000999901");
  await select("layout_families-0-family", "layout_01");
  await page.locator("#layout_families-group .add-row a").click();
  await select("layout_families-1-family", "layout_02");
  for (const [index, family] of ["layout_03", "layout_04", "layout_05", "layout_06"].entries()) {
    await page.locator("#layout_families-group .add-row a").click();
    await select(`layout_families-${index + 2}-family`, family);
  }
  await select("property_identifiers-0-property", { label: "Browser House One" });
  await select("property_identifiers-0-kind", "installation");
  await fill("property_identifiers-0-value", "000 000 000 000 000 001");
  await page.locator("#property_identifiers-group .add-row a").click();
  await select("property_identifiers-1-property", { label: "Browser House Two" });
  const houseTwo = await page.locator('[name="property_identifiers-1-property"]').inputValue();
  await select("property_identifiers-1-kind", "installation");
  await fill("property_identifiers-1-value", "000 000 000 000 000 002");
  await submit("Save");
  await page.locator("#result_list").getByRole("link", { name: "Browser Utilities", exact: true }).click();
  assert(await page.locator('[name="default_property"]').inputValue() === houseOne, "Default property did not persist");
  assert(await page.locator('[name="property_identifiers-1-value"]').inputValue() === "000000000000000002", "Identifier normalization did not persist");
  assert(await page.getByRole("heading", { name: "Invoice formats", exact: true }).count() === 1, "Invoice formats are not visible");

  const upload = async filename => {
    const bytes = readFixture
      ? await readFixture(filename)
      : Array.from(await (await import("node:fs/promises")).readFile(`${artifacts}/fixtures/${filename}`));
    await page.goto(`${base}/documents/`);
    await page.waitForLoadState("load");
    assert(!await page.locator('input[type="file"]').isVisible(), "Direct upload still shows a file subsection");
    const chooserPromise = page.waitForEvent("filechooser");
    await page.getByRole("button", { name: "Upload invoice", exact: true }).click();
    const chooser = await chooserPromise;
    assert(!chooser.isMultiple(), "Upload allows more than one PDF");
    const responsePromise = page.waitForResponse(response => response.request().method() === "POST" && new URL(response.url()).pathname === "/upload/");
    await chooser.setFiles(`${artifacts}/fixtures/${filename}`);
    assert((await responsePromise).status() === 302, "Direct upload failed");
    await page.waitForURL(url => /^\/documents\/\d+\/$/.test(url.pathname));
    await page.waitForLoadState("load");
    return { url: page.url(), bytes };
  };
  const supported = await upload("shared.pdf");
  await text("Browser House One");
  await text("Browser House Two");
  await text("140,250 kWh");
  await page.getByRole("link", { name: "Review invoice", exact: true }).click();
  assert(await page.locator('[name^="charges-"]').count() === 0, "Review still requires itemized fee fields");
  const allocationGross = page.locator('[name="sections-0-gross_minor"]');
  const originalAllocationGross = await allocationGross.inputValue();
  await allocationGross.fill("1,00");
  await submit("Approve invoice", 400);
  await text("Net plus VAT must equal cost including VAT.");
  await allocationGross.fill(originalAllocationGross);
  await fill("sections-0-end", "2024-01-31");
  await submit("Approve invoice", 400);
  await text("Last billed day must be on or after the first day.");
  await fill("sections-0-end", "2024-02-29");
  await fill("usages-0-quantity_milli", "140,2501");
  await submit("Approve invoice", 400);
  await text("Enter a number with at most 3 decimal places.");
  assert(await page.locator('[name="usages-0-quantity_milli"]').inputValue() === "140,2501", "Invalid input was lost");
  await fill("usages-0-quantity_milli", "140,250");
  await submit("Approve invoice");
  const supportedInvoice = page.url();
  await text("Approved revision 1");
  await text("308,75 SEK");
  const duplicate = await upload("shared.pdf");
  assert(duplicate.url === supported.url, "Duplicate upload created a new document");
  await text("This PDF is already stored.");
  await page.getByRole("link", { name: "Open approved invoice" }).click();
  await text("Approved revision 1");

  await page.getByRole("link", { name: "Reports", exact: true }).click();
  assert((await page.locator("#report-costs").innerText()).includes("138 SEK"), "Report multiplied or changed rounded supply costs");
  assert((await page.locator("#report-consumption").innerText()).includes("140 kWh"), "Duplicate upload multiplied consumption");
  assert((await page.locator("#report-basis").innerText()).includes("Electricity supply (fallback)"), "Supply fallback was not explicit");
  assert((await page.locator("#report-costs").innerText()).includes("Elhandel"), "Supply cost is missing its electricity subcategory");
  await page.goto(supportedInvoice);

  await openReview();
  const stale = await page.context().newPage();
  await stale.goto(page.url());
  await fill("usages-0-quantity_milli", "141,125");
  await fill("note", "Synthetic browser consumption correction.");
  await submit("Save correction");
  await text("Approved revision 2");
  await text("141,125 kWh");
  const reportPage = await page.context().newPage();
  await reportPage.goto(`${base}/reports/`);
  assert((await reportPage.locator("#report-consumption").innerText()).includes("141 kWh"), "Report did not refresh the approved correction");
  assert(!(await reportPage.locator("#report-consumption").innerText()).includes("140 kWh"), "Report retained the previous approved quantity");
  await reportPage.close();
  await fill("usages-0-quantity_milli", "143,000", stale);
  await submit("Save correction", 409, stale);
  await text("Your entries are retained", stale);
  assert(await stale.locator('[name="usages-0-quantity_milli"]').inputValue() === "143,000", "Stale entries were lost");
  await stale.close();
  await page.reload();
  await text("141,125 kWh");
  await page.getByRole("link", { name: "Open document", exact: true }).click();
  assert(await page.getByRole("link", { name: "Review invoice", exact: true }).count() === 1, "Document has duplicate review entry points");
  await page.getByRole("link", { name: "Review invoice", exact: true }).click();
  assert(await page.getByRole("link", { name: "Load latest extraction", exact: true }).count() === 0, "Already reviewed extraction is offered again");
  await page.getByRole("link", { name: "Cancel", exact: true }).click();
  await submit("Re-parse document");
  await text("Proposed differences");
  await text("141,125 kWh");
  await text("140,250 kWh");
  assert(await page.getByRole("link", { name: "Load latest extraction", exact: true }).count() === 0, "Extraction loading belongs inside review");
  await page.getByRole("link", { name: "Review invoice", exact: true }).click();
  assert(await page.locator('[name="usages-0-quantity_milli"]').inputValue() === "141,125", "Re-parsing replaced the correction form");
  await page.getByRole("link", { name: "Load latest extraction", exact: true }).click();
  assert(await page.locator('[name="usages-0-quantity_milli"]').inputValue() === "140,250", "Extraction proposal was not loaded separately");
  await submit("Save correction");
  await text("Approved revision 3");
  await text("140,250 kWh");
  assert(page.url() === supportedInvoice, "Accepting the proposal created another invoice");
  await openReview();
  assert(await page.getByRole("link", { name: "Load latest extraction", exact: true }).count() === 0, "Accepted extraction is still pending");
  await page.getByRole("link", { name: "Cancel", exact: true }).click();

  const manual = await upload("unsupported.pdf");
  await page.getByRole("link", { name: "Review invoice", exact: true }).click();
  assert(await page.locator('[name^="charges-"]').count() === 0, "Manual entry still requires itemized fees");
  await text("Extraction warnings");
  await select("vendor", { label: "Browser Utilities" });
  await fill("reference", "BROWSER-MANUAL-CREDIT-001");
  await select("kind", "credit");
  await fill("invoice_date", "2024-03-05");
  for (const [name, value] of [["net_minor", "-100,00"], ["vat_minor", "-25,00"], ["rounding_minor", "0,00"], ["invoice_total_minor", "-125,00"], ["amount_due_minor", "-115,00"]]) await fill(name, value);
  await page.getByLabel("I confirm any difference").check();
  await select("sections-0-property", houseTwo);
  await select("sections-0-service", "water");
  for (const [suffix, value] of [["net_minor", "-100,00"], ["vat_minor", "-25,00"], ["gross_minor", "-125,00"]]) {
    await fill(`sections-0-${suffix}`, value);
  }
  await fill("sections-0-start", "2024-02-29");
  await fill("sections-0-end", "2024-02-29");
  await page.getByRole("button", { name: "Add consumption", exact: true }).click();
  await select("usages-0-utility", "water");
  await select("usages-0-unit", "m3");
  await fill("usages-0-quantity_milli", "-1,250");
  await fill("usages-0-start", "2024-02-29");
  await fill("usages-0-end", "2024-02-29");
  await select("usages-0-basis", "reversal");
  await page.getByRole("button", { name: "Add identifier", exact: true }).click();
  await select("identifiers-0-scope", "manual-1");
  await select("identifiers-0-kind", "contract");
  await fill("identifiers-0-value", "BROWSER-MANUAL-KEY");

  // Added allocations update the related selectors. Removing a row is explicit.
  await page.getByRole("button", { name: "Add allocation", exact: true }).click();
  await select("sections-1-property", houseOne);
  await select("sections-1-service", "waste");
  for (const suffix of ["net_minor", "vat_minor", "gross_minor"]) await fill(`sections-1-${suffix}`, "0,00");
  await fill("sections-1-start", "2024-02-29");
  await fill("sections-1-end", "2024-02-29");
  await page.locator('[name="sections-1-DELETE"]').check();
  await submit("Approve invoice", 400);
  await text("This field is required.");
  await page.getByLabel("I checked the source and resolved").check();
  await submit("Approve invoice");
  const manualInvoice = page.url();
  await text("Credit invoice");
  await text("Browser House Two");
  await text("-1,250 m³");
  await text("2024-02-29 to 2024-02-29");
  await text("The amount due differs from the invoice total.");
  assert(!(await page.locator("body").innerText()).includes("Browser House One"), "Removed allocation was approved");

  const source = await page.context().request.get(`${manual.url}view/`);
  assert(source.status() === 200 && source.headers()["content-disposition"].startsWith("inline"), "Private viewer failed");
  assert((await source.body()).equals(Buffer.from(manual.bytes)), "Manual source bytes changed after publication");
  await page.screenshot({ path: `${artifacts}/invoice.png`, fullPage: true });
  await page.getByRole("link", { name: "Open document", exact: true }).click();
  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("link", { name: "Download", exact: true }).click();
  const download = await downloadPromise;
  assert(download.suggestedFilename() === "unsupported.pdf", "Download filename changed");
  await download.saveAs(`${artifacts}/original.pdf`);
  await page.getByRole("link", { name: "Review invoice", exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Review form overflows on mobile");
  await page.screenshot({ path: `${artifacts}/review-mobile.png` });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(`${base}/invoices/`);
  await select("property", houseTwo);
  await select("service", "water");
  await text("BROWSER-MANUAL-CREDIT-001");
  assert(!(await page.locator("tbody").innerText()).includes("880003"), "Invoice filters joined unrelated allocations");
  await filterDates("2024-03-06", "2024-03-06");
  await text("No approved invoices match");
  await filterDates("2024-03-05", "2024-03-05");
  await text("BROWSER-MANUAL-CREDIT-001");
  const warningSource = await upload("section-warning.pdf");
  await page.goto(`${base}/documents/`);
  assert(await page.locator('[name="processing"]').inputValue() === "attention", "Documents does not default to Needs attention");
  assert(await page.locator('[name="storage"]').count() === 0, "Documents still has a storage filter");
  assert(!(await page.locator("tbody").innerText()).includes("shared.pdf"), "Completed documents remain in the default queue");
  await select("processing", "review");
  const documents = await page.locator("tbody").innerText();
  assert(documents.includes("section-warning.pdf"), "Document filters lost a document needing review");
  assert(!documents.includes("shared.pdf") && !documents.includes("unsupported.pdf"), "Document status filter included completed invoices");
  await page.screenshot({ path: `${artifacts}/documents-desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Documents overflow on mobile");
  await page.setViewportSize({ width: 1280, height: 900 });

  await page.goto(warningSource.url);
  const allocationLabel = "Allocation 4 · Browser House One · Electricity network";
  const documentWarning = page.locator(".extraction-warnings");
  assert((await documentWarning.innerText()).includes("Missing billed electricity consumption"), "Consumption failure was described as an allocation totals error");
  assert(!(await documentWarning.innerText()).includes("section_04"), "Internal section ID is visible in the warning");
  await documentWarning.getByRole("link", { name: allocationLabel, exact: true }).click();
  assert(page.url().endsWith("#sections-3"), "Warning did not link to the review allocation");
  const allocation = page.locator("#sections-3");
  assert(await allocation.locator('[name="sections-3-property"]').inputValue() === houseOne, "Warning linked to another property");
  assert(await allocation.locator('[name="sections-3-service"]').inputValue() === "electricity_network", "Warning linked to another service");
  const reviewWarning = page.locator(".extraction-warnings");
  await reviewWarning.getByRole("link", { name: allocationLabel, exact: true }).click();
  assert(await allocation.evaluate(element => element.matches(":target")), "Allocation was not highlighted after following its warning");
  assert(await reviewWarning.getByRole("link", { name: "PDF page 4", exact: true }).getAttribute("href") === `${new URL(warningSource.url).pathname}view/#page=4`, "Warning linked to another PDF page");

  await upload("multiple-readings.pdf");
  assert(await page.locator(".extraction-warnings").count() === 0, "Valid multiple network readings produced an extraction warning");
  await page.getByRole("link", { name: "Review invoice", exact: true }).click();
  assert(await page.locator('[name="usages-1-subcategory"]').inputValue() === "matavfall", "Food waste subcategory was lost during review");
  assert(await page.locator('[name="usages-2-subcategory"]').inputValue() === "restavfall", "Residual waste subcategory was lost during review");
  const networkReadings = page.locator('[data-formset="usages"] [data-review-row]').filter({ has: page.locator('select[name$="-section"] option[value="section_04"]:checked') });
  assert(await networkReadings.count() === 2, "Multiple network readings were lost or duplicated");
  assert(await networkReadings.nth(0).locator('[name$="-quantity_milli"]').inputValue() === "121,125", "First network consumption was changed");
  assert(await networkReadings.nth(1).locator('[name$="-quantity_milli"]').inputValue() === "200,000", "Second network consumption was changed");
  assert(await networkReadings.nth(0).locator('[name$="-end"]').inputValue() === "2024-02-14", "First reading interval included the next period");
  assert(await networkReadings.nth(1).locator('[name$="-start"]').inputValue() === "2024-02-15", "Second reading interval overlapped the first");
  await submit("Approve invoice");
  await text("121,125 kWh");
  await text("200,000 kWh");
  await text("Matavfall");
  await text("Restavfall");
  await text("Electricity › Elnät");

  await page.getByRole("link", { name: "Reports", exact: true }).click();
  const consumption = await page.locator("#report-consumption").innerText();
  const foodWaste = page.locator("#report-consumption tbody tr").filter({ hasText: "Matavfall" });
  const residualWaste = page.locator("#report-consumption tbody tr").filter({ hasText: "Restavfall" });
  assert(await foodWaste.count() === 1 && (await foodWaste.innerText()).includes("12 kg"), "Food waste report total was mixed with residual waste");
  assert(await residualWaste.count() === 1 && (await residualWaste.innerText()).includes("8 kg"), "Residual waste report total was mixed with food waste");
  assert((await page.locator("#report-costs").innerText()).includes("Elnät"), "Network cost is missing its electricity subcategory");
  assert(consumption.includes("321 kWh") && consumption.includes("510 kWh") && consumption.includes("-1 m³"), "Report lost network intervals, a property or signed corrections");
  assert(!consumption.includes("140 kWh"), "Report summed supply with network consumption");
  assert((await page.locator("#report-costs").innerText()).includes("473 SEK"), "Network cost was multiplied by its readings");
  await select("property", houseTwo);
  assert(!(await page.locator("#report-consumption").innerText()).includes("Browser House One"), "Report property filter failed");
  await page.getByRole("link", { name: "Clear filters", exact: true }).click();
  await filterDates("2024-02-15", "2024-02-15");
  assert((await page.locator("#report-costs").innerText()).includes("No approved costs"), "Report filtered costs by billing period instead of invoice date");
  assert((await page.locator("#report-consumption").innerText()).includes("200 kWh"), "Report prorated a complete billed interval");
  await fill("date_from", "2024-03-05");
  await fill("date_to", "2024-02-01");
  await page.locator('[name="date_to"]').blur();
  assert(await page.locator('[name="date_to"]').evaluate(element => element.validationMessage) === "End date must be on or after start date.", "Invalid date range was submitted automatically");
  await page.goto(`${base}/reports/?date_from=2024-03-05&date_to=2024-02-01`);
  await text("End date must be on or after start date.");
  assert(await page.locator("#report-costs").count() === 0, "Invalid report filters produced totals");
  await page.getByRole("link", { name: "Clear filters", exact: true }).click();
  await page.screenshot({ path: `${artifacts}/reports-desktop.png`, fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Reports overflow on mobile");
  await page.screenshot({ path: `${artifacts}/reports-mobile.png`, fullPage: true });
  await page.setViewportSize({ width: 1280, height: 900 });

  await page.goto(supportedInvoice);
  await openReview();
  await fill("usages-0-start", "2024-01-01");
  await fill("note", "Synthetic partial network coverage check.");
  await submit("Save correction");
  await page.getByRole("link", { name: "Reports", exact: true }).click();
  const reportIssue = page.getByRole("table", { name: "Electricity consumption needing review" });
  assert((await reportIssue.innerText()).includes("Network readings cover only part of this supply period."), "Partial network coverage was not visible");
  assert((await reportIssue.innerText()).includes("2024-01-01 – 2024-02-29"), "Report changed the inclusive displayed final day");
  assert((await reportIssue.getByRole("link", { name: "View source", exact: true }).getAttribute("href")).includes(`${new URL(supported.url).pathname}view/#page=`), "Report warning linked to another source");
  await reportIssue.getByRole("link", { name: "Review invoice", exact: true }).click();
  assert(await page.locator('[name="usages-0-start"]').inputValue() === "2024-01-01", "Report warning opened another invoice review");
  await fill("usages-0-start", "2024-02-01");
  await submit("Save correction");
  await page.getByRole("link", { name: "Reports", exact: true }).click();
  assert(await page.getByRole("table", { name: "Electricity consumption needing review" }).count() === 0, "Resolved report exclusion remained stale");

  for (const [filename, reference, total] of [
    ["quarterly-waste.pdf", "910003", "130,00 SEK"],
    ["cover-supply.pdf", "910004", "100,00 SEK"],
    ["chart-supply.pdf", "910005", "100,00 SEK"],
    ["monthly-network.pdf", "910006", "100,15 SEK"],
  ]) {
    await upload(filename);
    await text("Browser House One");
    await page.getByRole("link", { name: "Review invoice", exact: true }).click();
    assert(await page.locator('[name="reference"]').inputValue() === reference, `Current format header failed: ${filename}`);
    await submit("Approve invoice");
    await text(total);
  }

  await page.goto(`${base}/invoices/`);
  await submit("Add manual invoice");
  await select("vendor", { label: "Browser Utilities" });
  await fill("reference", "BROWSER-LEDGER-001");
  await fill("invoice_date", "2024-03-05");
  for (const [name, value] of [["net_minor", "10,00"], ["vat_minor", "2,50"], ["rounding_minor", "0,00"], ["invoice_total_minor", "12,50"], ["amount_due_minor", "12,50"]]) await fill(name, value);
  await select("sections-0-property", houseOne);
  await select("sections-0-service", "other");
  for (const [suffix, value] of [["net_minor", "10,00"], ["vat_minor", "2,50"], ["gross_minor", "12,50"], ["start", "2024-02-01"], ["end", "2024-02-29"]]) await fill(`sections-0-${suffix}`, value);
  await submit("Approve invoice");
  await text("Manual entry");
  await page.getByRole("link", { name: "Open document", exact: true }).click();
  const ledgerDocument = new URL(page.url()).pathname;
  const ledgerName = await page.getByRole("heading", { level: 2 }).innerText();
  assert((await page.locator("body").innerText()).match(/no original PDF/gi).length === 1, "Manual document repeats its source explanation");
  assert(await page.getByRole("link", { name: "Review invoice", exact: true }).getAttribute("class") !== "button", "Manual review uses inconsistent action styling");
  await page.goto(`${base}/documents/`);
  assert(await page.getByRole("link", { name: ledgerName, exact: true }).count() === 0, "Completed manual entry remains in the attention queue");
  await select("processing", "");
  assert(await page.getByRole("link", { name: ledgerName, exact: true }).getAttribute("href") === ledgerDocument, "All documents excludes completed manual entries");
  await select("processing", "ready");
  await text("Done");
  await page.goto(`${base}/reports/`);
  const summary = await page.locator(".report-summary").innerText();
  assert(summary.includes("8 approved invoices") && summary.includes("7 from PDFs") && summary.includes("1 manual"), "Report source counts repeat allocations or omit manual entries");

  await page.goto(`${base}/documents/`);
  await page.waitForLoadState("load");
  const invalidChooserPromise = page.waitForEvent("filechooser");
  await page.getByRole("button", { name: "Upload invoice", exact: true }).click();
  const invalidResponse = page.waitForResponse(response => response.request().method() === "POST" && new URL(response.url()).pathname === "/upload/");
  await (await invalidChooserPromise).setFiles({ name: "invalid.pdf", mimeType: "application/pdf", buffer: Buffer.from("Wholly synthetic invalid PDF") });
  assert((await invalidResponse).status() === 400, "Invalid direct upload was accepted");
  await page.waitForLoadState("load");
  await text("PDF header");
  assert(await page.getByRole("button", { name: "Upload invoice", exact: true }).isEnabled(), "Upload errors do not allow retrying");

  const fallbackContext = await page.context().browser().newContext({ javaScriptEnabled: false, storageState: await page.context().storageState() });
  try {
    const fallback = await fallbackContext.newPage();
    await fallback.goto(`${base}/documents/`);
    assert(await fallback.locator('input[type="file"]').isVisible(), "Upload has no fallback without JavaScript");
    await fallback.locator('input[type="file"]').setInputFiles(`${artifacts}/fixtures/shared.pdf`);
    await submit("Upload invoice", 302, fallback);
    await text("This PDF is already stored.", fallback);
  } finally {
    await fallbackContext.close();
  }

  const anonymous = await page.context().browser().newContext();
  try {
    for (const url of [`${base}/reports/`, `${base}/invoices/`, manualInvoice, manual.url, `${manual.url}review/`, `${manual.url}source/`, `${manual.url}view/`]) {
      const response = await anonymous.request.get(url, { maxRedirects: 0 });
      assert(response.status() === 302 && response.headers().location.startsWith("/login/"), `Anonymous access was allowed: ${url}`);
    }
    const visitor = await anonymous.newPage();
    await visitor.goto(`${manual.url}view/`);
    assert(visitor.url().includes("/login/"), "Private source did not redirect an anonymous browser");
  } finally {
    await anonymous.close();
  }
  await page.locator(".user-menu summary").click();
  await submit("Sign out");
  assert(new URL(page.url()).pathname === "/login/", "User-menu sign out did not return to sign in");
  assert(errors.length === 0, `Browser script errors: ${errors.join(", ")}`);
  return "Configuration, upload, duplicates, validation, manual entry, approval, correction, stale saves, re-parsing, reporting sources/totals/filters, mobile layout and private source checks passed.";
}
