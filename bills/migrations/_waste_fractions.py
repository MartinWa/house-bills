"""Frozen SQL for waste projections and cost presence on shared vendor bills."""

# Exact replacement anchors and expanded SQL predicates retain their literal text.
# ruff: noqa: E501

from importlib import import_module

previous = import_module(".0022_restructured_reporting", __package__)
old_views = [
    *previous.REPORTS,
    ("reporting_service_coverage", previous.SERVICE_COVERAGE),
    ("reporting_invoice_coverage", previous.COVERAGE),
]
WASTE_FRACTIONS = [
    ("matavfall", "Matavfall", True),
    ("restavfall", "Restavfall", True),
    ("tradgardsavfall", "Trädgårdsavfall", True),
    ("plastic", "Plast", False),
    ("cardboard", "Kartong", False),
    ("glass", "Glas", False),
]


def pattern_due(assignment, service, month):
    return f"""coalesce((SELECT CASE WHEN cp.frequency_months>0
        AND {month}>=cp.first_expected_month
        AND ((CAST(strftime('%Y', {month}) AS INTEGER)-CAST(strftime('%Y', cp.first_expected_month) AS INTEGER))*12
             +CAST(strftime('%m', {month}) AS INTEGER)-CAST(strftime('%m', cp.first_expected_month) AS INTEGER)) % cp.frequency_months=0
        AND (cp.season_start_month IS NULL
             OR (cp.season_start_month<=cp.season_end_month AND CAST(strftime('%m', {month}) AS INTEGER) BETWEEN cp.season_start_month AND cp.season_end_month)
             OR (cp.season_start_month>cp.season_end_month AND (CAST(strftime('%m', {month}) AS INTEGER)>=cp.season_start_month OR CAST(strftime('%m', {month}) AS INTEGER)<=cp.season_end_month)))
        THEN 1 ELSE 0 END FROM bills_servicecostschedule cp
        WHERE cp.service_vendor_assignment_id={assignment}.id AND cp.is_active=1
        AND cp.start_month<={month} AND (cp.end_month IS NULL OR cp.end_month>={month})
        LIMIT 1), {service}.mode='regular')"""


def tracks_receipt(assignment, service, month):
    return f"""({service}.mode='regular' OR EXISTS (SELECT 1 FROM bills_servicecostschedule cp
        WHERE cp.service_vendor_assignment_id={assignment}.id AND cp.is_active=1 AND cp.frequency_months>0
        AND cp.start_month<={month} AND (cp.end_month IS NULL OR cp.end_month>={month})))"""


SERVICE_COVERAGE = (
    previous.SERVICE_COVERAGE.replace("is_active=1 AND mode='regular'", "is_active=1")
    .replace(
        "s.category, coalesce(s.subcategory, '') AS subcategory,",
        f"""s.category, s.service_group, coalesce(s.subcategory, '') AS subcategory,
             {pattern_due("a", "s", "m.expected_month")} AS cost_required,
             {tracks_receipt("a", "s", "m.expected_month")} AS tracks_vendor_receipt,
             (SELECT cp.id FROM bills_servicecostschedule cp WHERE cp.service_vendor_assignment_id=a.id
              AND cp.is_active=1 AND cp.start_month<=m.expected_month
              AND (cp.end_month IS NULL OR cp.end_month>=m.expected_month) LIMIT 1) AS cost_schedule_id,""",
    )
    .replace(
        "max(x.id) AS exception_id,",
        "max(x.id) AS exception_id, max(CASE WHEN x.id IS NOT NULL AND x.property_service_id IS NULL AND x.reviewed_invoice_id IS NULL THEN 1 ELSE 0 END) AS vendor_not_sent_exception,",
    )
    .replace(
        "WHEN reviewed_exception=1", "WHEN cost_required=0 THEN 'not_due' WHEN reviewed_exception=1"
    )
    .replace(
        "FROM matched\n",
        """FROM matched
    WHERE tracks_vendor_receipt=1 OR (vendor_id IS NOT NULL AND EXISTS (
        SELECT 1 FROM matched other WHERE other.vendor_id=matched.vendor_id
        AND other.expected_month=matched.expected_month AND other.tracks_vendor_receipt=1))
    """,
    )
)
COVERAGE = (
    previous.COVERAGE.replace(
        "count(*) AS required_service_count",
        "sum(cost_required) AS required_service_count, sum(cost_required=0) AS optional_service_count",
    )
    .replace(
        "sum(status='received') AS received_service_count",
        "sum(cost_required AND status='received') AS received_service_count",
    )
    .replace("sum(status NOT IN ('received',", "sum(cost_required AND status NOT IN ('received',")
    .replace(
        "WHEN sum(status='not_expected')=count(*) THEN 'not_expected'",
        "WHEN max(vendor_not_sent_exception)=1 OR (sum(cost_required)>0 AND sum(cost_required AND status='not_expected')=sum(cost_required)) THEN 'not_expected'",
    )
    .replace(
        "WHEN sum(status='missing')>0 THEN 'missing'",
        "WHEN date('now')>=missing_after THEN 'missing'",
    )
)
WASTE_COSTS = """CREATE VIEW reporting_waste_costs AS
SELECT c.*, coalesce(sc.is_waste_fraction, 0) AS is_waste_fraction, sc.name AS fraction_name
FROM reporting_costs c LEFT JOIN bills_subcategory sc ON sc.key=c.subcategory
WHERE c.category='waste'"""
WASTE_USAGE = """CREATE VIEW reporting_waste_usage AS
SELECT u.*, coalesce(sc.is_waste_fraction, 0) AS is_waste_fraction, sc.name AS fraction_name
FROM reporting_usage u LEFT JOIN bills_subcategory sc ON sc.key=u.subcategory
WHERE u.utility='waste'"""
VIEWS = [
    *previous.REPORTS,
    ("reporting_service_coverage", SERVICE_COVERAGE),
    ("reporting_invoice_coverage", COVERAGE),
    ("reporting_waste_costs", WASTE_COSTS),
    ("reporting_waste_usage", WASTE_USAGE),
]
TRIGGERS = []
for name, sql in previous.TRIGGERS:
    if name.startswith("invoice_exception_scope_"):
        sql = sql.replace(
            "s.is_active=1 AND s.mode='regular'",
            f"s.is_active=1 AND {tracks_receipt('a', 's', 'NEW.month')}",
        )
        sql = sql.replace(
            "NEW.property_service_id=s.id)",
            f"NEW.property_service_id=s.id AND {pattern_due('a', 's', 'NEW.month')})",
        )
    TRIGGERS.append((name, sql))

TRIGGERS.append(
    (
        "subcategory_referenced_fraction",
        """CREATE TRIGGER subcategory_referenced_fraction
BEFORE UPDATE OF is_waste_fraction ON bills_subcategory WHEN NEW.is_waste_fraction<>OLD.is_waste_fraction AND (
EXISTS (SELECT 1 FROM bills_propertyservice WHERE subcategory=OLD.key) OR
EXISTS (SELECT 1 FROM bills_billingsection WHERE subcategory=OLD.key) OR
EXISTS (SELECT 1 FROM bills_usage WHERE subcategory=OLD.key))
BEGIN SELECT RAISE(ABORT, 'Referenced fraction classification is immutable'); END""",
    )
)
for operation in ("INSERT", "UPDATE"):
    name = f"cost_schedule_overlap_{operation.lower()}"
    TRIGGERS.append(
        (
            name,
            f"""CREATE TRIGGER {name} BEFORE {operation} ON bills_servicecostschedule
WHEN NEW.is_active=1 AND EXISTS (SELECT 1 FROM bills_servicecostschedule r
WHERE r.id<>NEW.id AND r.is_active=1 AND r.service_vendor_assignment_id=NEW.service_vendor_assignment_id
AND (NEW.end_month IS NULL OR r.start_month<=NEW.end_month) AND (r.end_month IS NULL OR NEW.start_month<=r.end_month))
BEGIN SELECT RAISE(ABORT, 'Enabled cost patterns must not overlap'); END""",
        )
    )
    name = f"cost_schedule_parent_{operation.lower()}"
    TRIGGERS.append(
        (
            name,
            f"""CREATE TRIGGER {name} BEFORE {operation} ON bills_servicecostschedule
WHEN NOT EXISTS (SELECT 1 FROM bills_servicevendorassignment a JOIN bills_propertyservice s ON s.id=a.property_service_id
WHERE a.id=NEW.service_vendor_assignment_id AND s.category='waste' AND NEW.start_month>=a.start_month
AND (a.end_month IS NULL OR (NEW.end_month IS NOT NULL AND NEW.end_month<=a.end_month)))
OR (NEW.predecessor_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM bills_servicecostschedule p
WHERE p.id=NEW.predecessor_id AND p.service_vendor_assignment_id=NEW.service_vendor_assignment_id AND p.start_month<NEW.start_month))
BEGIN SELECT RAISE(ABORT, 'Invalid cost pattern assignment, dates or predecessor'); END""",
        )
    )
    name = f"cost_schedule_alignment_{operation.lower()}"
    TRIGGERS.append(
        (
            name,
            f"""CREATE TRIGGER {name} BEFORE {operation} ON bills_servicecostschedule
WHEN NEW.is_active=1 AND NEW.frequency_months>0 AND EXISTS (
SELECT 1 FROM bills_servicevendorassignment a JOIN bills_propertyservice s ON s.id=a.property_service_id
JOIN bills_vendorbillingschedule q ON q.vendor_id=a.vendor_id
WHERE a.id=NEW.service_vendor_assignment_id AND a.is_active=1 AND s.is_active=1 AND q.is_active=1
AND (NEW.end_month IS NULL OR q.start_month<=NEW.end_month) AND (q.end_month IS NULL OR NEW.start_month<=q.end_month)
AND (q.frequency_months=0 OR NEW.frequency_months%q.frequency_months<>0 OR
((CAST(strftime('%Y', NEW.first_expected_month) AS INTEGER)-CAST(strftime('%Y', q.first_expected_month) AS INTEGER))*12
+CAST(strftime('%m', NEW.first_expected_month) AS INTEGER)-CAST(strftime('%m', q.first_expected_month) AS INTEGER))%q.frequency_months<>0))
BEGIN SELECT RAISE(ABORT, 'Cost patterns must align with the shared vendor invoices'); END""",
        )
    )
TRIGGERS.append(
    (
        "assignment_cost_dates",
        """CREATE TRIGGER assignment_cost_dates BEFORE UPDATE ON bills_servicevendorassignment
WHEN EXISTS (SELECT 1 FROM bills_servicecostschedule cp WHERE cp.service_vendor_assignment_id=OLD.id
AND (cp.start_month<NEW.start_month OR (NEW.end_month IS NOT NULL AND (cp.end_month IS NULL OR cp.end_month>NEW.end_month))))
OR EXISTS (SELECT 1 FROM bills_servicecostschedule cp JOIN bills_propertyservice s ON s.id=NEW.property_service_id
WHERE cp.service_vendor_assignment_id=OLD.id AND s.category<>'waste')
BEGIN SELECT RAISE(ABORT, 'Assignment changes must preserve cost pattern bounds and classification'); END""",
    )
)
TRIGGERS.append(
    (
        "service_cost_category",
        """CREATE TRIGGER service_cost_category BEFORE UPDATE OF category ON bills_propertyservice
WHEN NEW.category<>'waste' AND EXISTS (SELECT 1 FROM bills_servicecostschedule cp
JOIN bills_servicevendorassignment a ON a.id=cp.service_vendor_assignment_id WHERE a.property_service_id=OLD.id)
BEGIN SELECT RAISE(ABORT, 'Cost patterns require a waste service'); END""",
    )
)


def alignment_conflict(pattern, schedule):
    return f"""({schedule}.frequency_months=0 OR {pattern}.frequency_months%{schedule}.frequency_months<>0 OR
    ((CAST(strftime('%Y', {pattern}.first_expected_month) AS INTEGER)-CAST(strftime('%Y', {schedule}.first_expected_month) AS INTEGER))*12
     +CAST(strftime('%m', {pattern}.first_expected_month) AS INTEGER)-CAST(strftime('%m', {schedule}.first_expected_month) AS INTEGER))%{schedule}.frequency_months<>0)"""


def overlapping_schedule(pattern, schedule):
    return f"""({pattern}.end_month IS NULL OR {schedule}.start_month<={pattern}.end_month)
    AND ({schedule}.end_month IS NULL OR {pattern}.start_month<={schedule}.end_month)"""


for operation in ("INSERT", "UPDATE"):
    name = f"cost_schedule_vendor_required_{operation.lower()}"
    TRIGGERS.append(
        (
            name,
            f"""CREATE TRIGGER {name} BEFORE {operation} ON bills_servicecostschedule
    WHEN NEW.is_active=1 AND NEW.frequency_months>0 AND EXISTS (
        SELECT 1 FROM bills_servicevendorassignment a JOIN bills_propertyservice s ON s.id=a.property_service_id
        WHERE a.id=NEW.service_vendor_assignment_id AND a.is_active=1 AND s.is_active=1
        AND NOT EXISTS (SELECT 1 FROM bills_vendorbillingschedule q WHERE q.vendor_id=a.vendor_id
            AND q.is_active=1 AND {overlapping_schedule("NEW", "q")}))
    BEGIN SELECT RAISE(ABORT, 'Periodic costs require a shared vendor schedule'); END""",
        )
    )
    name = f"vendor_cost_alignment_{operation.lower()}"
    TRIGGERS.append(
        (
            name,
            f"""CREATE TRIGGER {name} BEFORE {operation} ON bills_vendorbillingschedule
    WHEN NEW.is_active=1 AND EXISTS (
        SELECT 1 FROM bills_servicecostschedule cp JOIN bills_servicevendorassignment a ON a.id=cp.service_vendor_assignment_id
        JOIN bills_propertyservice s ON s.id=a.property_service_id
        WHERE a.vendor_id=NEW.vendor_id AND a.is_active=1 AND s.is_active=1
        AND cp.is_active=1 AND cp.frequency_months>0 AND {overlapping_schedule("cp", "NEW")}
        AND {alignment_conflict("cp", "NEW")})
    BEGIN SELECT RAISE(ABORT, 'Vendor schedules must preserve cost pattern alignment'); END""",
        )
    )

for table, key in (
    ("bills_servicevendorassignment", "a.id=NEW.id"),
    ("bills_propertyservice", "s.id=NEW.id"),
):
    name = f"{table}_cost_alignment"
    # AFTER sees changed vendor/parent and enabled flags together.
    TRIGGERS.append(
        (
            name,
            f"""CREATE TRIGGER {name} AFTER UPDATE ON {table}
    WHEN EXISTS (
        SELECT 1 FROM bills_servicecostschedule cp JOIN bills_servicevendorassignment a ON a.id=cp.service_vendor_assignment_id
        JOIN bills_propertyservice s ON s.id=a.property_service_id
        WHERE {key} AND a.is_active=1 AND s.is_active=1 AND cp.is_active=1 AND cp.frequency_months>0
        AND (NOT EXISTS (SELECT 1 FROM bills_vendorbillingschedule q WHERE q.vendor_id=a.vendor_id
              AND q.is_active=1 AND {overlapping_schedule("cp", "q")})
             OR EXISTS (SELECT 1 FROM bills_vendorbillingschedule q WHERE q.vendor_id=a.vendor_id
              AND q.is_active=1 AND {overlapping_schedule("cp", "q")} AND {alignment_conflict("cp", "q")})))
    BEGIN SELECT RAISE(ABORT, 'Parent changes must preserve cost pattern alignment'); END""",
        )
    )
