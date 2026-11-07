# Review browser checks

The Playwright CLI check uses a fresh installation, invented invoices and a separate
Compose project. It covers configuration and mapping persistence, upload, duplicates,
visible validation, supported approval, manual credits, row addition/removal, corrections,
stale forms, re-parsing and explicit acceptance, invoice filters, exact original bytes,
anonymous source blocking and mobile review layout.
Allocation warnings are also checked for readable property/service labels, direct
review links and the correct PDF page.
Multiple network readings must produce separate consecutive consumption periods,
reconcile against repeated billed price quantities and pass review/approval.
Review contains allocation totals and billing periods, with no itemized fee fields.
Invalid totals/dates must retain the entered values and show visible errors.
Reports must reflect corrections and duplicates, keep costs independent of multiple
readings, select network electricity with explicit supply fallbacks, preserve signed
credits, filter invoice dates separately from whole usage intervals, reject invalid
filters and fit the mobile viewport. Anonymous report access is blocked.
Waste review and reports retain Matavfall and Restavfall separately. Electricity costs
show Elhandel and Elnät under Electricity while consumption keeps the existing source
selection.

Design checks cover the Invoices landing page, User menu and sign out, upload within
Documents, automatic property/service/date filters, combined document processing and
storage filters, and desktop/mobile layout. Invalid date ranges are checked both in
the browser and through direct URLs. Desktop previews are saved as
`.playwright-cli/design-reports-desktop.png` and `design-documents-desktop.png`.

On PowerShell, from the repository root:

```powershell
$env:HOUSE_BILLS_DEV_PORT = '8006'
docker compose -p house-bills-step6-browser -f compose.development.yaml up -d --no-build app
# Wait until http://localhost:8006/login/ responds before running setup.
docker compose -p house-bills-step6-browser -f compose.development.yaml exec -T -e HOUSE_BILLS_BROWSER_CHECK=true app python -m tests.browser.setup
playwright-cli -s=house-bills-step6 open http://localhost:8006/
playwright-cli -s=house-bills-step6 run-code --filename=tests/browser/review.js
playwright-cli -s=house-bills-step6 close
docker compose -p house-bills-step6-browser -f compose.development.yaml down -v
Remove-Item Env:HOUSE_BILLS_DEV_PORT
```

The named disposable volume contains only check data; the final command removes that
volume. The normal development project and volume are preserved. The setup refuses
to operate on an installation containing users or billing records. Use a different
unused port/project name if necessary; open its URL before running the script.

Playwright CLI writes screenshots and a downloaded synthetic original under the ignored
`.playwright-cli` directory. Install the CLI following its own setup documentation.
Browser CI integration remains part of the first-release work.
