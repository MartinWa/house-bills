"""The no-invoice annotation extends native coverage without changing approved data."""

import os
import subprocess
import sys


def test_invoice_exception_migration_preserves_receipts_and_restores_native_view(tmp_path):
    script = r"""
import sqlite3
from datetime import date
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

auth = ('auth', '0012_alter_user_first_name_max_length')
old = [('bills', '0015_property_services'), auth]
new = [('bills', '0016_service_invoice_exceptions'), auth]
executor = MigrationExecutor(connection)
executor.migrate(old)
apps = executor.loader.project_state(old).apps
actor = apps.get_model('auth', 'User').objects.create(username='synthetic-exception-reviewer')
house = apps.get_model('bills', 'Property').objects.create(name='Synthetic House', slug='house')
vendor = apps.get_model('bills', 'Vendor').objects.create(name='Synthetic Vendor', slug='vendor')
service = apps.get_model('bills', 'PropertyService').objects.create(
    property=house, service='water', start_month=date(2024, 2, 1), end_month=date(2024, 4, 1))
apps.get_model('bills', 'ServiceVendorAssignment').objects.create(
    property_service=service, vendor=vendor,
    start_month=date(2024, 2, 1), end_month=date(2024, 4, 1))
document = apps.get_model('bills', 'Document').objects.create(
    sha256='1' * 64, original_filename='synthetic.pdf')
invoice = apps.get_model('bills', 'Invoice').objects.create(
    document=document, vendor=vendor, reference='SYNTHETIC-INVOICE-EXCEPTION',
    invoice_date=date(2024, 3, 5), net_minor=1000, vat_minor=250, rounding_minor=0,
    invoice_total_minor=1250, amount_due_minor=1250, status='approved',
    approved_at=timezone.now(), approved_by=actor, review_revision=1)
evidence = {'page': 1, 'text': 'Wholly invented migration evidence', 'bbox': [0, 0, 1, 1]}
apps.get_model('bills', 'BillingSection').objects.create(
    invoice=invoice, property=house, section_id='water', service='water',
    net_minor=1000, vat_minor=250, gross_minor=1250,
    start=date(2024, 2, 1), end=date(2024, 3, 1), period_evidence=evidence)

tables = ['bills_property', 'bills_vendor', 'bills_document', 'bills_invoice',
          'bills_billingsection', 'bills_usage', 'bills_extractionattempt',
          'bills_reviewrevision', 'bills_propertyservice', 'bills_servicevendorassignment']
def snapshot():
    with connection.cursor() as cursor:
        return {table: cursor.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()
                for table in tables}
before = snapshot()
for targets in (new, old, new):
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    assert snapshot() == before
    if targets == new:
        apps = executor.loader.project_state(new).apps
        Exception = apps.get_model('bills', 'ServiceInvoiceException')
        for month in (2, 3):
            Exception.objects.create(property_service_id=service.pk, vendor_id=vendor.pk,
                month=date(2024, month, 1), reason='Synthetic confirmed no-invoice month.')
    with sqlite3.connect(connection.settings_dict['NAME']) as native:
        assert native.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert native.execute('PRAGMA foreign_key_check').fetchall() == []
        assert native.execute('SELECT sum(gross_minor) FROM reporting_costs').fetchone() == (1250,)
        rows = native.execute(
            'SELECT expected_month, status FROM reporting_invoice_coverage ORDER BY expected_month'
        ).fetchall()
        assert rows == [('2024-02-01', 'not_expected' if targets == new else 'missing'),
                        ('2024-03-01', 'received'), ('2024-04-01', 'missing')]
print('Native invoice exceptions, preserved receipts and migration reversal verified.')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "house_bills.settings.development",
            "HOUSE_BILLS_DATA_DIR": str(tmp_path / "exception-migration-data"),
            "HOUSE_BILLS_STATE_DIR": str(tmp_path / "exception-migration-state"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
