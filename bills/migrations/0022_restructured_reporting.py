"""Shared schedules and catalogue-aware reporting; enforce cross-row invariants."""

from importlib import import_module

from django.db import migrations

old = import_module(".0013_vendor_billing_schedules", __package__)
legacy = import_module(".0017_legacy_invoice_coverage", __package__)

COSTS = old.NEW_VIEWS[0][1].replace(
    """    s.service,
    CASE WHEN s.service IN ('electricity_network', 'electricity_supply')
         THEN 'electricity' ELSE s.service END AS category,
    CASE WHEN s.service IN ('electricity_network', 'electricity_supply')
         THEN s.service ELSE s.subcategory END AS subcategory,""",
    """    CASE WHEN s.category = 'electricity' AND s.subcategory IN
                   ('electricity_network', 'electricity_supply')
         THEN s.subcategory ELSE s.category END AS service,
    s.category,
    coalesce(s.subcategory, '') AS subcategory,""",
)
USAGE = (
    old.NEW_VIEWS[1][1]
    .replace(
        "c.service AS source_service,",
        """CASE WHEN u.utility = 'electricity'
              AND u.subcategory IN ('electricity_network', 'electricity_supply')
         THEN u.subcategory ELSE c.service END AS source_service,""",
    )
    .replace(
        """CASE WHEN u.utility = 'electricity' THEN c.subcategory
         ELSE u.subcategory END AS subcategory,""",
        "coalesce(u.subcategory, '') AS subcategory,",
    )
)
SELECTION = old.NEW_VIEWS[2][1].replace(
    "CASE\n            WHEN EXISTS",
    """CASE
            WHEN e.source_service NOT IN ('electricity_network', 'electricity_supply')
                 THEN 'unspecified_source'
            WHEN EXISTS""",
)
REPORTS = [
    ("reporting_costs", COSTS),
    ("reporting_usage", USAGE),
    ("reporting_electricity_selection", SELECTION),
    old.NEW_VIEWS[3],
]

SERVICE_COVERAGE = f"""
CREATE VIEW reporting_service_coverage AS
WITH RECURSIVE months(property_service_id, expected_month) AS (
    SELECT id, start_month FROM bills_propertyservice
    WHERE is_active=1 AND mode='regular' AND start_month <= date('now', 'start of month')
    UNION ALL
    SELECT m.property_service_id, date(m.expected_month, '+1 month')
    FROM months m JOIN bills_propertyservice s ON s.id=m.property_service_id
    WHERE date(m.expected_month, '+1 month') <= date('now', 'start of month')
      AND (s.end_month IS NULL OR date(m.expected_month, '+1 month') <= s.end_month)
), {legacy.LEGACY_CATEGORIES}, receipts AS (
    SELECT i.id AS invoice_id, i.document_id, i.vendor_id, i.review_revision,
           date(i.invoice_date, 'start of month') AS invoice_month, i.status
    FROM bills_invoice i WHERE i.kind='invoice' AND i.invoice_date IS NOT NULL
      AND (i.status='approved' OR NOT EXISTS (
          SELECT 1 FROM bills_extractionattempt e JOIN bills_document d
          ON d.id=e.document_id AND d.extraction_revision=e.revision WHERE
    e.document_id=i.document_id))
    UNION ALL
    SELECT NULL, e.document_id, json_extract(e.candidate, '$.routing.vendor_id'), NULL,
           date(json_extract(e.candidate, '$.invoice_date'), 'start of month'), 'review'
    FROM bills_extractionattempt e
    JOIN bills_document d ON d.id=e.document_id AND d.extraction_revision=e.revision
    WHERE json_type(e.candidate, '$.routing.vendor_id')='integer'
      AND coalesce(json_extract(e.candidate, '$.routing.kind'), 'invoice')='invoice'
      AND NOT EXISTS (SELECT 1 FROM bills_invoice i
                      WHERE i.document_id=e.document_id AND i.status='approved')
), allocations AS (
    SELECT i.id AS invoice_id, i.document_id, i.vendor_id, b.property_id, b.category,
           coalesce(b.subcategory, '') AS subcategory,
           date(i.invoice_date, 'start of month') AS invoice_month, i.status
    FROM bills_invoice i JOIN bills_billingsection b ON b.invoice_id=i.id
    WHERE i.kind='invoice' AND i.invoice_date IS NOT NULL
      AND (i.status='approved' OR NOT EXISTS (
          SELECT 1 FROM bills_extractionattempt e JOIN bills_document d
          ON d.id=e.document_id AND d.extraction_revision=e.revision WHERE
    e.document_id=i.document_id))
    UNION ALL
    SELECT NULL, e.document_id, json_extract(e.candidate, '$.routing.vendor_id'), p.value,
           CASE WHEN json_extract(b.value, '$.service') IN
                     ('electricity_network', 'electricity_supply') THEN 'electricity'
                ELSE coalesce(json_extract(b.value, '$.category'), json_extract(b.value,
    '$.service')) END,
           CASE WHEN json_extract(b.value, '$.service') IN
                     ('electricity_network', 'electricity_supply')
                THEN coalesce(nullif(json_extract(b.value, '$.subcategory'), ''),
    json_extract(b.value, '$.service'))
                ELSE coalesce(json_extract(b.value, '$.subcategory'),
                     (SELECT l.subcategory FROM legacy_categories l
                      WHERE l.extraction_id=e.id AND l.section_index=b.key), '') END,
           date(json_extract(e.candidate, '$.invoice_date'), 'start of month'), 'review'
    FROM bills_extractionattempt e
    JOIN bills_document d ON d.id=e.document_id AND d.extraction_revision=e.revision
    JOIN json_each(e.candidate, '$.sections') b
    JOIN json_each(e.candidate, '$.routing.section_properties') p
         ON p.key=json_extract(b.value, '$.section_id')
    WHERE json_type(e.candidate, '$.routing.vendor_id')='integer' AND p.type='integer'
      AND coalesce(json_extract(e.candidate, '$.routing.kind'), 'invoice')='invoice'
      AND NOT EXISTS (SELECT 1 FROM bills_invoice i
                      WHERE i.document_id=e.document_id AND i.status='approved')
), matched AS (
    SELECT s.id AS property_service_id, s.property_id, p.name AS property_name,
           s.category, coalesce(s.subcategory, '') AS subcategory,
           a.id AS assignment_id, a.vendor_id, v.name AS vendor_name,
           q.id AS schedule_id, q.frequency_months, q.grace_days, m.expected_month,
           unixepoch(m.expected_month) AS expected_time,
           date(m.expected_month, '+1 month', printf('+%d days', q.grace_days)) AS missing_after,
           (SELECT count(DISTINCT r.invoice_id) FROM receipts r WHERE r.vendor_id=a.vendor_id
            AND r.invoice_month=m.expected_month AND r.status='approved') AS
    approved_invoice_count,
           (SELECT count(DISTINCT r.document_id) FROM receipts r WHERE r.vendor_id=a.vendor_id
            AND r.invoice_month=m.expected_month AND r.status='review') AS review_document_count,
           (SELECT min(r.invoice_id) FROM receipts r WHERE r.vendor_id=a.vendor_id
            AND r.invoice_month=m.expected_month AND r.status='approved') AS invoice_id,
           (SELECT min(r.document_id) FROM receipts r WHERE r.vendor_id=a.vendor_id
            AND r.invoice_month=m.expected_month AND r.status='review') AS review_document_id,
           count(DISTINCT CASE WHEN b.status='approved' THEN b.invoice_id END) AS
    allocation_invoice_count,
           count(DISTINCT CASE WHEN b.status='review' THEN b.document_id END) AS
    allocation_review_count,
           max(x.id) AS exception_id,
           max(x.reason) AS exception_reason,
           max(CASE WHEN x.reviewed_invoice_id IS NOT NULL AND EXISTS (
               SELECT 1 FROM receipts r WHERE r.invoice_id=x.reviewed_invoice_id
               AND r.review_revision=x.reviewed_revision AND r.vendor_id=a.vendor_id
               AND r.invoice_month=m.expected_month AND r.status='approved'
           ) THEN 1 ELSE 0 END) AS reviewed_exception
    FROM months m
    JOIN bills_propertyservice s ON s.id=m.property_service_id
    JOIN bills_property p ON p.id=s.property_id
    LEFT JOIN bills_servicevendorassignment a ON a.property_service_id=s.id AND a.is_active=1
         AND a.start_month <= m.expected_month AND (a.end_month IS NULL OR a.end_month >=
    m.expected_month)
    LEFT JOIN bills_vendor v ON v.id=a.vendor_id
    LEFT JOIN bills_vendorbillingschedule q ON q.vendor_id=a.vendor_id AND q.is_active=1
         AND q.start_month <= m.expected_month AND (q.end_month IS NULL OR q.end_month >=
    m.expected_month)
    LEFT JOIN allocations b ON b.vendor_id=a.vendor_id AND b.property_id=s.property_id
         AND b.category=s.category AND b.invoice_month=m.expected_month
         AND (s.subcategory IS NULL OR b.subcategory=s.subcategory)
    LEFT JOIN bills_serviceinvoiceexception x ON x.vendor_id=a.vendor_id AND
    x.month=m.expected_month
         AND (x.property_service_id IS NULL OR x.property_service_id=s.id)
    WHERE a.id IS NULL OR q.id IS NULL OR (q.frequency_months > 0
         AND m.expected_month >= q.first_expected_month
         AND ((CAST(strftime('%Y', m.expected_month) AS INTEGER)-CAST(strftime('%Y',
    q.first_expected_month) AS INTEGER))*12
              +CAST(strftime('%m', m.expected_month) AS INTEGER)-CAST(strftime('%m',
    q.first_expected_month) AS INTEGER)) % q.frequency_months=0)
    GROUP BY s.id, m.expected_month, a.id
)
SELECT matched.*,
       CASE WHEN vendor_id IS NULL THEN 'unassigned'
            WHEN schedule_id IS NULL THEN 'unscheduled'
            WHEN allocation_invoice_count > 0 THEN 'received'
            WHEN allocation_review_count > 0 THEN 'needs_review'
            WHEN reviewed_exception=1 AND approved_invoice_count=1 AND review_document_count=0
    THEN 'not_expected'
            WHEN approved_invoice_count > 0 THEN 'incomplete'
            WHEN review_document_count > 0 THEN 'needs_review'
            WHEN exception_id IS NOT NULL THEN 'not_expected'
            WHEN date('now') >= missing_after THEN 'missing' ELSE 'expected' END AS status
FROM matched
"""

COVERAGE = """
CREATE VIEW reporting_invoice_coverage AS
SELECT vendor_id, vendor_name, expected_month, expected_time, schedule_id,
       group_concat(DISTINCT property_name) AS property_names,
       frequency_months, grace_days, missing_after,
       CASE WHEN vendor_id IS NULL THEN property_service_id END AS property_service_id,
       count(*) AS required_service_count,
       sum(status='received') AS received_service_count,
       max(approved_invoice_count) AS approved_invoice_count,
       max(review_document_count) AS review_document_count,
       max(invoice_id) AS invoice_id, max(review_document_id) AS review_document_id,
       max(exception_id) AS exception_id,
       group_concat(DISTINCT CASE WHEN status='not_expected' THEN exception_reason END) AS
    exception_reason,
       CASE WHEN vendor_id IS NULL THEN 'unassigned'
            WHEN schedule_id IS NULL THEN 'unscheduled'
            WHEN max(approved_invoice_count)>0 AND sum(status NOT IN ('received',
    'not_expected'))>0 THEN 'incomplete'
            WHEN max(approved_invoice_count)>0 THEN 'received'
            WHEN max(review_document_count)>0 THEN 'needs_review'
            WHEN sum(status='not_expected')=count(*) THEN 'not_expected'
            WHEN sum(status='missing')>0 THEN 'missing' ELSE 'expected' END AS status
FROM reporting_service_coverage
GROUP BY vendor_id, expected_month, CASE WHEN vendor_id IS NULL THEN property_service_id END
"""

TRIGGERS = []
for table, category in (
    ("bills_propertyservice", "category"),
    ("bills_billingsection", "category"),
    ("bills_usage", "utility"),
):
    for operation in ("INSERT", "UPDATE"):
        name = f"{table}_classification_{operation.lower()}"
        TRIGGERS.append(
            (
                name,
                f"""CREATE TRIGGER {name} BEFORE {operation} ON {table}
        WHEN NEW.subcategory IS NOT NULL AND NOT EXISTS
             (SELECT 1 FROM bills_subcategory c WHERE c.key=NEW.subcategory AND
    c.category=NEW.{category})
        BEGIN SELECT RAISE(ABORT, 'Subcategory must belong to the category'); END""",
            )
        )
for table, key in (
    ("bills_vendorbillingschedule", "r.vendor_id=NEW.vendor_id"),
    (
        "bills_servicevendorassignment",
        "r.vendor_id=NEW.vendor_id AND r.property_service_id=NEW.property_service_id",
    ),
    (
        "bills_propertyservice",
        "r.property_id=NEW.property_id AND r.category=NEW.category "
        "AND coalesce(r.subcategory,'')=coalesce(NEW.subcategory,'')",
    ),
):
    for operation in ("INSERT", "UPDATE"):
        name = f"{table}_overlap_{operation.lower()}"
        TRIGGERS.append(
            (
                name,
                f"""CREATE TRIGGER {name} BEFORE {operation} ON {table}
        WHEN NEW.is_active=1 AND EXISTS (SELECT 1 FROM {table} r WHERE r.id<>NEW.id
             AND r.is_active=1 AND {key} AND (NEW.end_month IS NULL OR
    r.start_month<=NEW.end_month)
             AND (r.end_month IS NULL OR NEW.start_month<=r.end_month))
        BEGIN SELECT RAISE(ABORT, 'Enabled configuration periods must not overlap'); END""",
            )
        )
TRIGGERS.append(
    (
        "subcategory_referenced_category",
        """CREATE TRIGGER subcategory_referenced_category
BEFORE UPDATE OF category ON bills_subcategory WHEN NEW.category<>OLD.category AND (
EXISTS (SELECT 1 FROM bills_propertyservice WHERE subcategory=OLD.key) OR
EXISTS (SELECT 1 FROM bills_billingsection WHERE subcategory=OLD.key) OR
EXISTS (SELECT 1 FROM bills_usage WHERE subcategory=OLD.key))
BEGIN SELECT RAISE(ABORT, 'Referenced subcategory category is immutable'); END""",
    )
)

for operation in ("INSERT", "UPDATE"):
    name = f"invoice_exception_scope_{operation.lower()}"
    TRIGGERS.append(
        (
            name,
            f"""CREATE TRIGGER {name} BEFORE {operation} ON bills_serviceinvoiceexception
    WHEN NOT EXISTS (
        SELECT 1 FROM bills_vendorbillingschedule q
        JOIN bills_servicevendorassignment a ON a.vendor_id=q.vendor_id
        JOIN bills_propertyservice s ON s.id=a.property_service_id
        WHERE q.vendor_id=NEW.vendor_id AND q.is_active=1 AND q.frequency_months>0
        AND q.start_month<=NEW.month AND (q.end_month IS NULL OR q.end_month>=NEW.month)
        AND q.first_expected_month<=NEW.month
        AND ((CAST(strftime('%Y', NEW.month) AS INTEGER)-CAST(strftime('%Y',
    q.first_expected_month) AS INTEGER))*12
             +CAST(strftime('%m', NEW.month) AS INTEGER)-CAST(strftime('%m',
    q.first_expected_month) AS INTEGER)) % q.frequency_months=0
        AND a.is_active=1 AND a.start_month<=NEW.month AND (a.end_month IS NULL OR
    a.end_month>=NEW.month)
        AND s.is_active=1 AND s.mode='regular' AND s.start_month<=NEW.month
        AND (s.end_month IS NULL OR s.end_month>=NEW.month)
        AND (NEW.property_service_id IS NULL OR NEW.property_service_id=s.id)
    ) OR (NEW.reviewed_invoice_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM bills_invoice i WHERE i.id=NEW.reviewed_invoice_id
        AND i.vendor_id=NEW.vendor_id AND i.status='approved' AND i.kind='invoice'
        AND date(i.invoice_date, 'start of month')=NEW.month AND
    NEW.reviewed_revision<=i.review_revision
    ))
    BEGIN SELECT RAISE(ABORT, 'Invalid invoice exception schedule or scope'); END""",
        )
    )
for operation in ("INSERT", "UPDATE"):
    name = f"usage_allocation_category_{operation.lower()}"
    TRIGGERS.append(
        (
            name,
            f"""CREATE TRIGGER {name} BEFORE {operation} ON bills_usage
    WHEN NOT EXISTS (SELECT 1 FROM bills_billingsection s WHERE s.id=NEW.section_id AND
    s.category=NEW.utility)
    BEGIN SELECT RAISE(ABORT, 'Consumption must match its allocation category'); END""",
        )
    )
TRIGGERS.append(
    (
        "allocation_usage_category",
        """CREATE TRIGGER allocation_usage_category
BEFORE UPDATE OF category ON bills_billingsection WHEN EXISTS (
SELECT 1 FROM bills_usage u WHERE u.section_id=OLD.id AND u.utility<>NEW.category)
BEGIN SELECT RAISE(ABORT, 'Allocation category must match its consumption'); END""",
    )
)


class Migration(migrations.Migration):
    dependencies = [("bills", "0021_reviewed_allocation_exceptions")]
    operations = [
        *[migrations.RunSQL(sql, reverse_sql=f"DROP VIEW {name}") for name, sql in REPORTS],
        migrations.RunSQL(SERVICE_COVERAGE, reverse_sql="DROP VIEW reporting_service_coverage"),
        migrations.RunSQL(COVERAGE, reverse_sql="DROP VIEW reporting_invoice_coverage"),
        *[migrations.RunSQL(sql, reverse_sql=f"DROP TRIGGER {name}") for name, sql in TRIGGERS],
    ]
