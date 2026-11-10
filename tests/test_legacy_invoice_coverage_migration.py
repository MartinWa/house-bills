"""Legacy receipt coverage upgrades and reverses without altering invoice evidence."""

import os
import subprocess
import sys


def test_legacy_coverage_migration_preserves_data_and_restores_native_view(tmp_path):
    script = r"""
import sqlite3
from datetime import date
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

auth = ('auth', '0012_alter_user_first_name_max_length')
old = [('bills', '0016_service_invoice_exceptions'), auth]
new = [('bills', '0017_legacy_invoice_coverage'), auth]
executor = MigrationExecutor(connection)
executor.migrate(old)
apps = executor.loader.project_state(old).apps
actor = apps.get_model('auth', 'User').objects.create(username='synthetic-legacy-reviewer')
house = apps.get_model('bills', 'Property').objects.create(name='Synthetic House', slug='house')
vendor = apps.get_model('bills', 'Vendor').objects.create(name='Synthetic Vendor', slug='vendor')
services = {}
for category in ('waste_collection', 'tank_emptying'):
    service = apps.get_model('bills', 'PropertyService').objects.create(
        property=house, service='waste', subcategory=category,
        start_month=date(2024, 2, 1), end_month=date(2024, 3, 1))
    apps.get_model('bills', 'ServiceVendorAssignment').objects.create(
        property_service=service, vendor=vendor,
        start_month=service.start_month, end_month=service.end_month)
    services[category] = service
apps.get_model('bills', 'ServiceInvoiceException').objects.create(
    property_service=services['waste_collection'], vendor=vendor, month=date(2024, 2, 1),
    reason='Synthetic earlier confirmation; an invoice subsequently arrived.')
evidence = {'page': 1, 'text': 'Grundavgift permanentbostad synthetic fee', 'bbox': [0, 0, 1, 1]}
pending = apps.get_model('bills', 'Document').objects.create(
    sha256='1' * 64, original_filename='synthetic-legacy.pdf', extraction_revision=1,
    state='review')
extraction = apps.get_model('bills', 'ExtractionAttempt').objects.create(
    document=pending, revision=1, state='review', layout_family='synthetic',
    layout_version='v1', parser_revision='legacy',
    candidate={'invoice_date': '2024-02-05', 'sections': [
        {'section_id': 'waste', 'service': 'waste', 'period_evidence': evidence}],
        'routing': {'vendor_id': vendor.pk, 'kind': 'invoice',
                    'section_properties': {'waste': house.pk}}},
    page_evidence=[evidence])
approved = apps.get_model('bills', 'Document').objects.create(
    sha256='2' * 64, original_filename='synthetic-approved.pdf')
invoice = apps.get_model('bills', 'Invoice').objects.create(
    document=approved, vendor=vendor, reference='SYNTHETIC-LEGACY-COVERAGE',
    invoice_date=date(2024, 3, 5), net_minor=1000, vat_minor=250, rounding_minor=0,
    invoice_total_minor=1250, amount_due_minor=1250, status='approved',
    approved_at=timezone.now(), approved_by=actor, review_revision=1)
apps.get_model('bills', 'BillingSection').objects.create(
    invoice=invoice, property=house, section_id='waste', service='waste',
    subcategory='waste_collection', net_minor=1000, vat_minor=250, gross_minor=1250,
    start=date(2024, 2, 1), end=date(2024, 3, 1), period_evidence=evidence)
apps.get_model('bills', 'ReviewRevision').objects.create(
    invoice=invoice, revision=1, actor=actor,
    snapshot={'private': 'Wholly invented immutable approval evidence'})

tables = ['bills_property', 'bills_vendor', 'bills_document', 'bills_invoice',
          'bills_billingsection', 'bills_usage', 'bills_extractionattempt',
          'bills_reviewrevision', 'bills_propertyservice', 'bills_servicevendorassignment',
          'bills_serviceinvoiceexception']
def snapshot():
    with connection.cursor() as cursor:
        return {table: cursor.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()
                for table in tables}
before = snapshot()
for targets in (new, old, new):
    MigrationExecutor(connection).migrate(targets)
    assert snapshot() == before
    with sqlite3.connect(connection.settings_dict['NAME']) as native:
        assert native.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert native.execute('PRAGMA foreign_key_check').fetchall() == []
        assert native.execute('SELECT sum(gross_minor) FROM reporting_costs').fetchone() == (1250,)
        rows = native.execute(
            'SELECT expected_month, subcategory, status, review_document_count '
            'FROM reporting_invoice_coverage ORDER BY expected_month, subcategory').fetchall()
        assert rows == [
            ('2024-02-01', 'tank_emptying', 'missing', 0),
            ('2024-02-01', 'waste_collection',
             'needs_review' if targets == new else 'not_expected', 1 if targets == new else 0),
            ('2024-03-01', 'tank_emptying', 'missing', 0),
            ('2024-03-01', 'waste_collection', 'received', 0)]
print('Native legacy invoice coverage, immutable evidence and migration reversal verified.')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "house_bills.settings.development",
            "HOUSE_BILLS_DATA_DIR": str(tmp_path / "legacy-coverage-migration-data"),
            "HOUSE_BILLS_STATE_DIR": str(tmp_path / "legacy-coverage-migration-state"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
