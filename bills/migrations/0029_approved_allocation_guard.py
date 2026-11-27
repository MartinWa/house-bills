"""Block low-level updates to allocation rows of approved invoices.

Only UPDATE is guarded: the review service rewrites rows only while the invoice
is back in review, so an update while approved has no sanctioned flow. DELETE is
deliberately unguarded because Django's flush and cascade deletes (whole-document
deletion in deleting.py, test teardown) must be able to remove rows of approved
invoices; in-app deletion is receipt-based and checked at the model layer.
"""

from django.db import migrations

TRIGGERS = [
    (
        "bills_billingsection_approved_update",
        """CREATE TRIGGER bills_billingsection_approved_update BEFORE UPDATE ON bills_billingsection
WHEN EXISTS (SELECT 1 FROM bills_invoice i WHERE
     (i.id=OLD.invoice_id OR i.id=NEW.invoice_id) AND i.status='approved')
BEGIN SELECT RAISE(ABORT,
     'Approved allocations can only be changed through the review service'); END""",
    ),
    (
        "bills_usage_approved_update",
        """CREATE TRIGGER bills_usage_approved_update BEFORE UPDATE ON bills_usage
WHEN EXISTS (SELECT 1 FROM bills_billingsection s JOIN bills_invoice i ON i.id=s.invoice_id
     WHERE (s.id=OLD.section_id OR s.id=NEW.section_id) AND i.status='approved')
BEGIN SELECT RAISE(ABORT,
     'Approved allocations can only be changed through the review service'); END""",
    ),
]


class Migration(migrations.Migration):
    dependencies = [("bills", "0028_invoice_document_unique")]
    operations = [
        *[migrations.RunSQL(sql, reverse_sql=f"DROP TRIGGER {name}") for name, sql in TRIGGERS],
    ]
