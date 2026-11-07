"""Live, SQLite-native projections of the current approved revision only."""

from django.db import migrations, models

INVOICES = """
CREATE VIEW reporting_invoices AS
SELECT
    i.id AS invoice_id,
    i.document_id,
    i.vendor_id,
    v.name AS vendor_name,
    v.slug AS vendor_slug,
    i.reference,
    i.kind AS invoice_kind,
    i.invoice_date,
    unixepoch(i.invoice_date) AS invoice_time,
    i.currency,
    i.net_minor,
    i.vat_minor,
    i.rounding_minor,
    i.invoice_total_minor,
    i.amount_due_minor,
    i.review_revision,
    i.approved_at,
    unixepoch(i.approved_at) AS approved_time
FROM bills_invoice AS i
JOIN bills_vendor AS v ON v.id = i.vendor_id
WHERE i.status = 'approved'
"""

COSTS = """
CREATE VIEW reporting_costs AS
SELECT
    s.id AS allocation_id,
    s.section_id AS section_key,
    i.invoice_id,
    i.document_id,
    i.vendor_id,
    i.vendor_name,
    i.vendor_slug,
    i.reference,
    i.invoice_kind,
    i.invoice_date,
    i.invoice_time,
    i.currency,
    i.review_revision,
    i.approved_at,
    i.approved_time,
    s.property_id,
    p.name AS property_name,
    p.slug AS property_slug,
    s.service,
    s.net_minor,
    s.vat_minor,
    s.gross_minor,
    s.start AS period_start,
    s.end AS period_end,
    unixepoch(s.start) AS period_start_time,
    unixepoch(s.end) AS period_end_time
FROM bills_billingsection AS s
JOIN reporting_invoices AS i ON i.invoice_id = s.invoice_id
JOIN bills_property AS p ON p.id = s.property_id
"""

USAGE = """
CREATE VIEW reporting_usage AS
SELECT
    u.id AS usage_id,
    c.allocation_id,
    c.section_key,
    c.invoice_id,
    c.document_id,
    c.vendor_id,
    c.vendor_name,
    c.vendor_slug,
    c.reference,
    c.invoice_kind,
    c.invoice_date,
    c.invoice_time,
    c.review_revision,
    c.approved_at,
    c.approved_time,
    c.property_id,
    c.property_name,
    c.property_slug,
    c.service AS source_service,
    u.utility,
    u.unit,
    u.quantity_milli,
    u.start AS period_start,
    u.end AS period_end,
    unixepoch(u.start) AS period_start_time,
    unixepoch(u.end) AS period_end_time,
    u.basis,
    CAST(json_extract(u.evidence, '$.page') AS INTEGER) AS source_page
FROM bills_usage AS u
JOIN reporting_costs AS c ON c.allocation_id = u.section_id
"""

ELECTRICITY_SELECTION = """
CREATE VIEW reporting_electricity_selection AS
WITH electricity AS (
    SELECT * FROM reporting_usage WHERE utility = 'electricity'
), positives AS (
    SELECT
        e.*,
        row_number() OVER (
            PARTITION BY property_id, vendor_id, source_service,
                         period_start, period_end, quantity_milli
            ORDER BY invoice_date, invoice_id, usage_id
        ) AS positive_position
    FROM electricity AS e
    WHERE quantity_milli > 0
), remaining_positives AS (
    -- An exact signed cancellation consumes one positive at most. Both original
    -- rows remain in reporting; only overlap detection uses this pairing.
    SELECT p.*
    FROM positives AS p
    WHERE p.positive_position > (
        SELECT count(*) FROM electricity AS reversal
        WHERE reversal.property_id = p.property_id
          AND reversal.vendor_id = p.vendor_id
          AND reversal.source_service = p.source_service
          AND reversal.period_start = p.period_start
          AND reversal.period_end = p.period_end
          AND reversal.quantity_milli = -p.quantity_milli
    )
), conflicts AS (
    SELECT
        a.property_id,
        a.source_service,
        a.period_start AS a_start,
        a.period_end AS a_end,
        b.period_start AS b_start,
        b.period_end AS b_end
    FROM remaining_positives AS a
    JOIN remaining_positives AS b
      ON b.property_id = a.property_id
     AND b.source_service = a.source_service
     AND b.usage_id > a.usage_id
     AND b.period_start < a.period_end
     AND a.period_start < b.period_end
), assessed AS (
    SELECT e.*,
        CASE
            WHEN EXISTS (
                SELECT 1 FROM conflicts AS c
                WHERE c.property_id = e.property_id
                  AND c.source_service = e.source_service
                  AND (
                      (c.a_start < e.period_end AND e.period_start < c.a_end)
                      OR (c.b_start < e.period_end AND e.period_start < c.b_end)
                  )
            ) THEN 'same_source_overlap'
            WHEN e.source_service = 'electricity_network' THEN 'network'
            WHEN EXISTS (
                SELECT 1 FROM conflicts AS c
                WHERE c.property_id = e.property_id
                  AND c.source_service = 'electricity_network'
                  AND (
                      (c.a_start < e.period_end AND e.period_start < c.a_end)
                      OR (c.b_start < e.period_end AND e.period_start < c.b_end)
                  )
            ) THEN 'network_source_overlap'
            WHEN NOT EXISTS (
                SELECT 1 FROM electricity AS n
                WHERE n.property_id = e.property_id
                  AND n.source_service = 'electricity_network'
                  AND n.period_start < e.period_end
                  AND e.period_start < n.period_end
            ) THEN 'supply_only'
            WHEN EXISTS (
                SELECT 1 FROM electricity AS n
                WHERE n.property_id = e.property_id
                  AND n.source_service = 'electricity_network'
                  AND n.period_start <= e.period_start
                  AND n.period_end > e.period_start
            ) AND NOT EXISTS (
                -- Coverage may consist of several consecutive readings. Every
                -- network end inside the supply period must have a successor.
                SELECT 1 FROM electricity AS edge
                WHERE edge.property_id = e.property_id
                  AND edge.source_service = 'electricity_network'
                  AND edge.period_end > e.period_start
                  AND edge.period_end < e.period_end
                  AND NOT EXISTS (
                      SELECT 1 FROM electricity AS successor
                      WHERE successor.property_id = e.property_id
                        AND successor.source_service = 'electricity_network'
                        AND successor.period_start <= edge.period_end
                        AND successor.period_end > edge.period_end
                  )
            ) THEN 'network_covers_period'
            ELSE 'partial_network_coverage'
        END AS selection_reason
    FROM electricity AS e
)
SELECT assessed.*,
    CASE selection_reason
        WHEN 'network' THEN 'network'
        WHEN 'supply_only' THEN 'supply_fallback'
        WHEN 'network_covers_period' THEN 'network_preferred'
        ELSE 'needs_review'
    END AS source_selection
FROM assessed
"""

ELECTRICITY_USAGE = """
CREATE VIEW reporting_electricity_usage AS
SELECT * FROM reporting_electricity_selection
WHERE source_selection IN ('network', 'supply_fallback')
"""


class Migration(migrations.Migration):
    dependencies = [("bills", "0007_cost_totals")]

    operations = [
        migrations.AddIndex(
            model_name="billingsection",
            index=models.Index(fields=["property", "service"], name="section_property_service"),
        ),
        migrations.AddIndex(
            model_name="usage",
            index=models.Index(fields=["utility", "start", "end"], name="usage_utility_period"),
        ),
        *[
            migrations.RunSQL(sql, reverse_sql=f"DROP VIEW {name}")
            for name, sql in [
                ("reporting_invoices", INVOICES),
                ("reporting_costs", COSTS),
                ("reporting_usage", USAGE),
                ("reporting_electricity_selection", ELECTRICITY_SELECTION),
                ("reporting_electricity_usage", ELECTRICITY_USAGE),
            ]
        ],
    ]
