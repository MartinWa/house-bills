"""Live expected invoice months and received originals, including pending review."""

from django.db import migrations

COVERAGE = """
CREATE VIEW reporting_invoice_coverage AS
WITH RECURSIVE months(schedule_id, expected_month) AS (
    SELECT id, start_month
    FROM bills_vendorbillingschedule
    WHERE is_active = 1 AND frequency_months > 0
      AND start_month <= date('now', 'start of month')
    UNION ALL
    SELECT m.schedule_id,
           date(m.expected_month, printf('+%d months', s.frequency_months))
    FROM months AS m
    JOIN bills_vendorbillingschedule AS s ON s.id = m.schedule_id
    WHERE date(m.expected_month, printf('+%d months', s.frequency_months))
          <= date('now', 'start of month')
      AND (s.end_month IS NULL OR
           date(m.expected_month, printf('+%d months', s.frequency_months)) <= s.end_month)
), receipts AS (
    SELECT i.id AS invoice_id, i.document_id, i.vendor_id, b.property_id,
           b.service, b.subcategory, date(i.invoice_date, 'start of month') AS invoice_month,
           i.status
    FROM bills_invoice AS i
    JOIN bills_billingsection AS b ON b.invoice_id = i.id
    WHERE i.kind = 'invoice' AND i.invoice_date IS NOT NULL
    UNION ALL
    SELECT NULL, e.document_id, json_extract(e.candidate, '$.routing.vendor_id'),
           p.value, json_extract(b.value, '$.service'),
           coalesce(json_extract(b.value, '$.subcategory'), ''),
           date(json_extract(e.candidate, '$.invoice_date'), 'start of month'), 'review'
    FROM bills_extractionattempt AS e
    JOIN bills_document AS d ON d.id = e.document_id AND d.extraction_revision = e.revision
    JOIN json_each(e.candidate, '$.sections') AS b
    JOIN json_each(e.candidate, '$.routing.section_properties') AS p
         ON p.key = json_extract(b.value, '$.section_id')
    WHERE json_type(e.candidate, '$.routing.vendor_id') = 'integer' AND p.type = 'integer'
      AND coalesce(json_extract(e.candidate, '$.routing.kind'), 'invoice') = 'invoice'
      AND NOT EXISTS (
          SELECT 1 FROM bills_invoice AS approved
          WHERE approved.document_id = e.document_id AND approved.status = 'approved'
      )
), matched AS (
    SELECT s.id AS schedule_id, s.vendor_id, v.name AS vendor_name, v.slug AS vendor_slug,
           s.property_id, p.name AS property_name, p.slug AS property_slug, s.service,
           CASE WHEN s.service IN ('electricity_network', 'electricity_supply')
                THEN 'electricity' ELSE s.service END AS category,
           CASE WHEN s.service IN ('electricity_network', 'electricity_supply')
                THEN s.service ELSE s.subcategory END AS subcategory,
           s.frequency_months, s.grace_days, m.expected_month,
           unixepoch(m.expected_month) AS expected_time,
           date(m.expected_month, '+1 month', printf('+%d days', s.grace_days)) AS missing_after,
           count(DISTINCT CASE WHEN r.status = 'approved' THEN r.invoice_id END)
               AS approved_invoice_count,
           count(DISTINCT CASE WHEN r.status = 'review' THEN r.document_id END)
               AS review_document_count,
           min(CASE WHEN r.status = 'approved' THEN r.invoice_id END) AS invoice_id,
           min(CASE WHEN r.status = 'review' THEN r.document_id END) AS review_document_id
    FROM months AS m
    JOIN bills_vendorbillingschedule AS s ON s.id = m.schedule_id
    JOIN bills_vendor AS v ON v.id = s.vendor_id
    JOIN bills_property AS p ON p.id = s.property_id
    LEFT JOIN receipts AS r ON r.vendor_id = s.vendor_id AND r.property_id = s.property_id
         AND r.service = s.service AND r.invoice_month = m.expected_month
         AND (s.subcategory = '' OR r.subcategory = s.subcategory)
    GROUP BY s.id, m.expected_month
)
SELECT matched.*,
       CASE WHEN approved_invoice_count > 0 THEN 'received'
            WHEN review_document_count > 0 THEN 'needs_review'
            WHEN date('now') >= missing_after THEN 'missing'
            ELSE 'expected' END AS status
FROM matched
"""


class Migration(migrations.Migration):
    dependencies = [("bills", "0013_vendor_billing_schedules")]

    operations = [
        migrations.RunSQL(COVERAGE, reverse_sql="DROP VIEW reporting_invoice_coverage"),
    ]
