"""Enforce the owner decision that one document retains at most one invoice.

SQLite rebuilds bills_invoice when the constraint is added. Every reporting view
references bills_invoice directly or through reporting_invoices, so the full
0027 view set and the exception triggers that reference the table are dropped
and recreated around the rebuild like earlier table-restructure migrations do.
"""

from importlib import import_module

from django.db import migrations, models

from ._waste_fractions import TRIGGERS

# reporting_invoices was never replaced by the later reporting chains; this is
# the first schema rebuild of bills_invoice since 0008 created the view.
INVOICES_VIEW = (
    "reporting_invoices",
    import_module(".0008_reporting_views", __package__).INVOICES,
)
REPORT_VIEWS = import_module(".0027_electricity_demand", __package__).NEW_VIEWS
SCOPE_TRIGGERS = [
    (name, sql) for name, sql in TRIGGERS if name.startswith("invoice_exception_scope_")
]


def preflight(apps, schema_editor):
    duplicates = (
        apps.get_model("bills", "Invoice")
        .objects.using(schema_editor.connection.alias)
        .values("document_id")
        .annotate(count=models.Count("id"))
        .filter(count__gt=1)
        .exists()
    )
    if duplicates:
        raise RuntimeError(
            "Some documents retain more than one invoice. Inventory those records "
            "read-only and reconcile them before enforcing one invoice per document."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("bills", "0027_electricity_demand"),
    ]

    operations = [
        migrations.RunPython(preflight, migrations.RunPython.noop),
        *[
            migrations.RunSQL(f"DROP TRIGGER {name}", reverse_sql=sql)
            for name, sql in SCOPE_TRIGGERS
        ],
        migrations.RunSQL(f"DROP VIEW {INVOICES_VIEW[0]}", reverse_sql=INVOICES_VIEW[1]),
        *[
            migrations.RunSQL(f"DROP VIEW {name}", reverse_sql=sql)
            for name, sql in reversed(REPORT_VIEWS)
        ],
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.UniqueConstraint(fields=["document"], name="invoice_document_unique"),
        ),
        migrations.RunSQL(INVOICES_VIEW[1], reverse_sql=f"DROP VIEW {INVOICES_VIEW[0]}"),
        *[migrations.RunSQL(sql, reverse_sql=f"DROP VIEW {name}") for name, sql in REPORT_VIEWS],
        *[
            migrations.RunSQL(sql, reverse_sql=f"DROP TRIGGER {name}")
            for name, sql in SCOPE_TRIGGERS
        ],
    ]
