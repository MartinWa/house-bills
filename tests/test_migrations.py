"""Exercise the old-to-new data contract in a separate, disposable SQLite database."""

import os
import subprocess
import sys


def test_cost_totals_migration_preserves_approvals_usage_and_legacy_fee_evidence(tmp_path):
    data = tmp_path / "migration-data"
    data.mkdir()
    script = r"""
import copy
from datetime import date
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

old_targets = [
    ('bills', '0006_original_storage'), ('auth', '0012_alter_user_first_name_max_length')
]
new_targets = [('bills', '0007_cost_totals'), ('auth', '0012_alter_user_first_name_max_length')]
executor = MigrationExecutor(connection)
executor.migrate(old_targets)
apps = executor.loader.project_state(old_targets).apps
User = apps.get_model('auth', 'User')
Document = apps.get_model('bills', 'Document')
Invoice = apps.get_model('bills', 'Invoice')
Section = apps.get_model('bills', 'BillingSection')
Charge = apps.get_model('bills', 'Charge')
Usage = apps.get_model('bills', 'Usage')
Review = apps.get_model('bills', 'ReviewRevision')
Extraction = apps.get_model('bills', 'ExtractionAttempt')
Property = apps.get_model('bills', 'Property')
Vendor = apps.get_model('bills', 'Vendor')
actor = User.objects.create(username='synthetic-legacy-reviewer')
house = Property.objects.create(name='Synthetic House', slug='synthetic-house')
vendor = Vendor.objects.create(name='Synthetic Utilities', slug='synthetic-utilities')
document = Document.objects.create(sha256='a' * 64, original_filename='synthetic.pdf')
invoice = Invoice.objects.create(
    document=document, vendor=vendor, reference='SYNTHETIC-LEGACY-001',
    invoice_date=date(2024, 3, 5), net_minor=900, vat_minor=225, rounding_minor=0,
    invoice_total_minor=1125, amount_due_minor=1125, status='approved',
    approved_at=timezone.now(), approved_by=actor, review_revision=1,
)
section = Section.objects.create(
    invoice=invoice, property=house, section_id='section_01', service='water',
    net_minor=900, vat_minor=225, gross_minor=1125,
)
evidence = {'page': 2, 'text': 'Wholly synthetic legacy billing source.', 'bbox': [0, 0, 10, 10]}
for position, start, end, net, vat in (
    (0, date(2024, 2, 1), date(2024, 3, 1), 1000, 250),
    (1, date(2024, 1, 1), date(2024, 2, 1), -100, -25),
):
    Charge.objects.create(
        section=section, position=position, description='Synthetic fee or correction',
        start=start, end=end, net_minor=net, vat_minor=vat, gross_minor=net + vat,
        vat_basis='manual', evidence=evidence)
Usage.objects.create(section=section, position=0, utility='water', unit='m3',
    quantity_milli=-12000, start=date(2024, 1, 1), end=date(2024, 2, 1),
    basis='reversal', evidence=evidence)
legacy_rows = list(Charge.objects.values())
candidate = {'sections': [{'section_id': 'section_01', 'service': 'water', 'charges': [
    {**row, 'start': row['start'].isoformat(), 'end': row['end'].isoformat()} for row in legacy_rows
]}]}
attempt = Extraction.objects.create(document=document, revision=1, state='review',
    layout_family='layout_01', layout_version='v1', parser_revision='2',
    candidate=candidate, page_evidence=[evidence])
snapshot = {
    'values': copy.deepcopy(candidate), 'properties': {'section_01': house.pk},
    'vendor_id': vendor.pk, 'kind': 'invoice'
}
review = Review.objects.create(
    invoice=invoice, revision=1, actor=actor, extraction=attempt, snapshot=snapshot)
before_invoice = Invoice.objects.values().get(pk=invoice.pk)
before_usage = list(Usage.objects.values())

executor = MigrationExecutor(connection)
executor.migrate(new_targets)
apps = executor.loader.project_state(new_targets).apps
Document = apps.get_model('bills', 'Document')
Invoice = apps.get_model('bills', 'Invoice')
Section = apps.get_model('bills', 'BillingSection')
Usage = apps.get_model('bills', 'Usage')
Review = apps.get_model('bills', 'ReviewRevision')
Extraction = apps.get_model('bills', 'ExtractionAttempt')
assert 'bills_charge' not in connection.introspection.table_names()
assert Invoice.objects.values().get(pk=invoice.pk) == before_invoice
assert list(Usage.objects.values()) == before_usage
assert Review.objects.get(pk=review.pk).snapshot == snapshot
assert Extraction.objects.get(pk=attempt.pk).candidate == candidate
current = Section.objects.get(pk=section.pk)
assert (current.start, current.end) == (date(2024, 1, 1), date(2024, 3, 1))
assert (current.net_minor, current.vat_minor, current.gross_minor) == (900, 225, 1125)
archive = Document.objects.get(pk=document.pk).legacy_charge_details
assert len(archive) == len(legacy_rows)
for old, retained in zip(legacy_rows, archive, strict=True):
    assert {key: retained[key] for key in old} == {
        **old, 'start': old['start'].isoformat(), 'end': old['end'].isoformat()
    }
from bills.services import validate_allocations
from bills.models import Invoice as CurrentInvoice
validate_allocations(CurrentInvoice.objects.get(pk=invoice.pk))

draft = Invoice.objects.create(document_id=document.pk, reference='SYNTHETIC-TOTAL-002')
new_section = Section.objects.create(invoice=draft, section_id='section_02', service='water',
    net_minor=-100, vat_minor=-25, gross_minor=-125, start=date(2024, 4, 1), end=date(2024, 5, 1))
executor = MigrationExecutor(connection)
executor.migrate(old_targets)
apps = executor.loader.project_state(old_targets).apps
Charge = apps.get_model('bills', 'Charge')
assert list(Charge.objects.filter(section_id=section.pk).values()) == legacy_rows
total = Charge.objects.get(section_id=new_section.pk)
assert (total.net_minor, total.vat_minor, total.gross_minor) == (-100, -25, -125)
assert total.description == 'Reviewed service total'
assert (total.start, total.end) == (date(2024, 4, 1), date(2024, 5, 1))
executor = MigrationExecutor(connection)
executor.migrate(new_targets)
assert 'bills_charge' not in connection.introspection.table_names()
assert apps.get_model('bills', 'ReviewRevision').objects.get(pk=review.pk).snapshot == snapshot
print('Migration, history preservation and legacy restoration verified.')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "house_bills.settings.development",
            "HOUSE_BILLS_DATA_DIR": str(data),
            "HOUSE_BILLS_STATE_DIR": str(tmp_path / "migration-state"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
