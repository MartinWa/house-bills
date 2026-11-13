"""Keep vendor not-sent explanations when no service costs are due."""

from django.db import migrations

from ._waste_fractions import COVERAGE as OLD_COVERAGE

COVERAGE = OLD_COVERAGE.replace(
    "WHEN status='not_expected' THEN exception_reason",
    "WHEN status='not_expected' OR vendor_not_sent_exception=1 THEN exception_reason",
)


class Migration(migrations.Migration):
    dependencies = [("bills", "0025_waste_fractions")]

    operations = [
        migrations.RunSQL("DROP VIEW reporting_invoice_coverage", reverse_sql=OLD_COVERAGE),
        migrations.RunSQL(COVERAGE, reverse_sql="DROP VIEW reporting_invoice_coverage"),
    ]
