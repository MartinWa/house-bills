# Review browser checks

The pinned Chromium check creates a temporary installation with invented invoices and
uses a separate browser image/state volume. It covers configuration and mapping persistence,
all six current format families, upload, duplicates,
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
Documents with a direct file picker and a fallback without JavaScript, automatic
property/service/date filters, the default attention queue and All/Done status filters,
and desktop/mobile layout. Review starts from approved values and offers a newer
extraction only inside review. Reports show rounded totals and distinct PDF/manual
invoice counts. Manual entries have one source explanation and leave the queue after
approval. Every document offers a delete page. Checks cover deletion of approved PDFs,
parsed entries and approved manual invoices, updated reporting counts, and fresh uploads
of the same PDF. Cancelling an unfinished manual form removes its placeholder, while
cancelling a correction keeps the saved invoice. Cancellation and deletion also work
without JavaScript. Invalid date ranges are checked in the browser and through direct URLs.
Desktop and mobile previews are saved with the synthetic browser artifacts.
Invoice folder checks cover shared-original counts, manual entries, stored filenames,
PDF downloads, retained filters, keyboard controls without JavaScript and an expanded
320-pixel mobile layout. Folder previews are saved alongside the other screenshots.

Footer checks cover release/GitHub links, no anonymous status requests, and a visible
update indicator only for a newer release. The container runner supplies a synthetic
stable version and intercepts release status to cover current and unavailable responses
without contacting GitHub.

From the repository root with Docker running, including on PowerShell:

```sh
docker compose -f compose.development.yaml build browser
docker compose -f compose.development.yaml run --rm browser python -m tools.run_browser
```

`tools/run_browser.py` creates a new database, secret and originals directory under the
browser state's temporary storage, migrates it, populates synthetic setup and starts
the application on container loopback. It removes that instance after the check. The
normal development database is never used. Setup additionally refuses any instance
already containing users or billing records.

The image installs Playwright from `package-lock.json` and Chromium during its build.
No host Node, browser or Playwright CLI installation is needed. `tests/browser/run.cjs`
runs the shared `review.js` scenario, blocks requests outside the local application and
collects traces, screenshots, synthetic downloads and server logs under
`/state/browser-artifacts/run-*` in the browser volume. CI uploads those artifacts for
seven days and executes the built image without a source mount or external network.

To reproduce CI using the built image directly:

```sh
docker run --rm --network none --init --shm-size=1g --cap-drop ALL --security-opt no-new-privileges:true house-bills:browser python -m tools.run_browser
```

The `--rm` form discards artifacts with the container. Use a named disposable container
without `--rm` and `docker cp` if artifacts are needed afterward. Treat artifacts as
synthetic test evidence; never run this scenario against real billing data.

## Optional interactive CLI run

The same scenario remains compatible with an independently installed Playwright CLI.
Use a dedicated disposable Compose project and an unused host port:

```powershell
$env:HOUSE_BILLS_DEV_PORT = '8006'
docker compose -p house-bills-cli-browser -f compose.development.yaml up -d --no-build app
# Wait until http://localhost:8006/login/ responds before running setup.
docker compose -p house-bills-cli-browser -f compose.development.yaml exec -T -e HOUSE_BILLS_BROWSER_CHECK=true app python -m tests.browser.setup
playwright-cli -s=house-bills-cli open http://localhost:8006/
playwright-cli -s=house-bills-cli run-code --filename=tests/browser/review.js
playwright-cli -s=house-bills-cli close
docker compose -p house-bills-cli-browser -f compose.development.yaml down -v
Remove-Item Env:HOUSE_BILLS_DEV_PORT
```

The named disposable volume contains only check data; the final command removes that
volume. The normal development project and volume are preserved. The setup refuses
to operate on an installation containing users or billing records. Use a different
unused port/project name if necessary; open its URL before running the script.

The interactive CLI writes screenshots and a downloaded synthetic original under the
ignored `.playwright-cli` directory. Install that optional CLI following its own setup
documentation. The container scenario is the canonical release check.
