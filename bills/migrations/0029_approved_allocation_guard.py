"""Block low-level writes to allocation rows of approved invoices.

Whole-document deletion removes an invoice's review revisions first (dependency
order in deleting.py) before the invoice cascade deletes its allocations, so a
DELETE is only aborted while the approved invoice still retains its approval
history. Updates have no such sanctioned flow: the review service rewrites rows
only while the invoice is back in review.
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
        "bills_billingsection_approved_delete",
        """CREATE TRIGGER bills_billingsection_approved_delete BEFORE DELETE ON bills_billingsection
WHEN EXISTS (SELECT 1 FROM bills_invoice i WHERE i.id=OLD.invoice_id AND i.status='approved'
     AND EXISTS (SELECT 1 FROM bills_reviewrevision r WHERE r.invoice_id=i.id))
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
    (
        "bills_usage_approved_delete",
        """CREATE TRIGGER bills_usage_approved_delete BEFORE DELETE ON bills_usage
WHEN EXISTS (SELECT 1 FROM bills_billingsection s JOIN bills_invoice i ON i.id=s.invoice_id
     WHERE s.id=OLD.section_id AND i.status='approved'
     AND EXISTS (SELECT 1 FROM bills_reviewrevision r WHERE r.invoice_id=i.id))
BEGIN SELECT RAISE(ABORT,
     'Approved allocations can only be changed through the review service'); END""",
    ),
]


class Migration(migrations.Migration):
    dependencies = [("bills", "0028_invoice_document_unique")]
    operations = [
        *[migrations.RunSQL(sql, reverse_sql=f"DROP TRIGGER {name}") for name, sql in TRIGGERS],
    ]
