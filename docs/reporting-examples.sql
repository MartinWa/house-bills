-- Costs belong to invoice dates. This groups whole allocations by invoice month;
-- service periods are retained in reporting_costs and are not prorated.
SELECT unixepoch(date(invoice_date, 'start of month')) AS time,
       property_slug, service, currency,
       sum(gross_minor) AS gross_minor,
       sum(gross_minor) / 100.0 AS cost_including_vat
FROM reporting_costs
GROUP BY date(invoice_date, 'start of month'), property_id, property_slug, service, currency
ORDER BY time, property_slug, service;

-- Combined electricity cost includes both sources. It never joins usage rows.
SELECT property_slug, currency, sum(gross_minor) AS gross_minor,
       sum(gross_minor) / 100.0 AS cost_including_vat
FROM reporting_costs
WHERE service IN ('electricity_network', 'electricity_supply')
GROUP BY property_id, property_slug, currency
ORDER BY property_slug;

-- Water and waste retain signed, whole billed intervals and measurement basis.
-- period_end is exclusive; time represents period_start, not a measured instant.
SELECT period_start_time AS time, property_slug, utility, subcategory, unit, source_service,
       period_start, period_end, basis, quantity_milli,
       quantity_milli / 1000.0 AS quantity
FROM reporting_usage
WHERE utility <> 'electricity'
ORDER BY time, property_slug, utility, usage_id;

-- Canonical electricity keeps network readings and disjoint supply fallbacks.
-- Separate rows preserve the source, basis, corrections and actual intervals.
SELECT period_start_time AS time, property_slug, period_start, period_end,
       source_service, source_selection, basis, quantity_milli,
       quantity_milli / 1000.0 AS quantity
FROM reporting_electricity_usage
ORDER BY time, property_slug, usage_id;

-- Check exclusions alongside consumption totals; they require invoice review.
SELECT property_slug, invoice_id, reference, source_service,
       period_start, period_end, quantity_milli, selection_reason
FROM reporting_electricity_selection
WHERE source_selection = 'needs_review'
ORDER BY property_slug, period_start, invoice_id, usage_id;

-- Aggregate costs before joining headers, so rounding and invoice totals occur
-- once per invoice, including invoices shared by several properties.
WITH allocated AS (
    SELECT invoice_id, sum(net_minor) AS net_minor,
           sum(vat_minor) AS vat_minor, sum(gross_minor) AS gross_minor
    FROM reporting_costs GROUP BY invoice_id
)
SELECT i.invoice_id, i.reference, i.currency,
       a.gross_minor AS allocated_gross_minor, i.rounding_minor,
       i.invoice_total_minor, i.amount_due_minor,
       (a.net_minor = i.net_minor AND a.vat_minor = i.vat_minor
        AND a.gross_minor + i.rounding_minor = i.invoice_total_minor) AS reconciles
FROM reporting_invoices AS i
JOIN allocated AS a ON a.invoice_id = i.invoice_id
ORDER BY i.invoice_date, i.invoice_id;
-- Missing invoice expectations for a Grafana table. Each row is a property/service
-- expectation; one PDF can satisfy several services or properties. Filter the chosen
-- invoice year or expected_time after selecting the live view.
SELECT expected_month, property_name, vendor_name, category, subcategory,
       missing_after, status
FROM reporting_invoice_coverage
WHERE status = 'missing'
ORDER BY expected_month, property_name, vendor_name, service, subcategory;

-- Missing property/service expectations and received documents needing review.
-- Unassigned months identify services needing a vendor.
SELECT status, count(*) AS expectation_count
FROM reporting_invoice_coverage
WHERE status IN ('missing', 'needs_review', 'unassigned')
GROUP BY status;

-- Confirmed months with no invoice. These do not enter the missing-invoice count.
SELECT expected_month, property_name, vendor_name, category, subcategory,
       exception_reason
FROM reporting_invoice_coverage
WHERE status = 'not_expected'
ORDER BY expected_month, property_name, vendor_name, service, subcategory;
