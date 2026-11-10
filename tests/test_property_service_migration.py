"""Move existing schedules to property services without changing invoice history."""

import os
import subprocess
import sys


def test_property_service_migration_preserves_vendor_history_and_reverses(tmp_path):
    script = r"""
import sqlite3
from datetime import date
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

auth = ('auth', '0012_alter_user_first_name_max_length')
old = [('bills', '0014_invoice_coverage_view'), auth]
new = [('bills', '0015_property_services'), auth]
executor = MigrationExecutor(connection)
executor.migrate(old)
apps = executor.loader.project_state(old).apps
house = apps.get_model('bills', 'Property').objects.create(name='Synthetic House', slug='house')
Vendor = apps.get_model('bills', 'Vendor')
first = Vendor.objects.create(name='Synthetic First Vendor', slug='first')
second = Vendor.objects.create(name='Synthetic Second Vendor', slug='second')
Schedule = apps.get_model('bills', 'VendorBillingSchedule')
for vendor, start, end in [(first, 2, 3), (second, 3, 4), (first, 11, 11)]:
    Schedule.objects.create(
        vendor=vendor, property=house, service='electricity_supply',
        start_month=date(2024, start, 1), end_month=date(2024, end, 1))
Schedule.objects.create(
    vendor=first, property=house, service='waste', subcategory='waste_collection',
    start_month=date(2024, 2, 1), end_month=date(2024, 11, 1), frequency_months=3)
Schedule.objects.create(
    vendor=first, property=house, service='waste', subcategory='tank_emptying',
    start_month=date(2024, 1, 1), frequency_months=0)
Schedule.objects.create(
    vendor=first, property=house, service='water', is_active=False,
    start_month=date(2024, 1, 1), end_month=date(2024, 12, 1))

actor = apps.get_model('auth', 'User').objects.create(username='synthetic-migration-reviewer')
document = apps.get_model('bills', 'Document').objects.create(
    sha256='1' * 64, original_filename='synthetic.pdf')
invoice = apps.get_model('bills', 'Invoice').objects.create(
    document=document, vendor=first, reference='SYNTHETIC-SERVICE-MIGRATION',
    invoice_date=date(2024, 3, 5), net_minor=1000, vat_minor=250, rounding_minor=0,
    invoice_total_minor=1250, amount_due_minor=1250, status='approved',
    approved_at=timezone.now(), approved_by=actor, review_revision=1)
evidence = {'page': 1, 'text': 'Wholly invented migration evidence', 'bbox': [0, 0, 1, 1]}
apps.get_model('bills', 'BillingSection').objects.create(
    invoice=invoice, property=house, section_id='supply', service='electricity_supply',
    net_minor=1000, vat_minor=250, gross_minor=1250, start=date(2024, 1, 1),
    end=date(2024, 2, 1), period_evidence=evidence)
attempt = apps.get_model('bills', 'ExtractionAttempt').objects.create(
    document=document, revision=1, state='review', layout_family='layout_02',
    layout_version='v1', parser_revision='synthetic', candidate={'synthetic': 'immutable'})
apps.get_model('bills', 'ReviewRevision').objects.create(
    invoice=invoice, revision=1, actor=actor, extraction=attempt,
    snapshot={'synthetic': 'immutable approval'})

tables = ['bills_property', 'bills_vendor', 'bills_document', 'bills_invoice',
          'bills_billingsection', 'bills_usage', 'bills_extractionattempt',
          'bills_reviewrevision', 'auth_user']
def snapshot():
    with connection.cursor() as cursor:
        return {table: cursor.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()
                for table in tables}
before = snapshot()
with sqlite3.connect(connection.settings_dict['NAME']) as native:
    schedules = native.execute(
        'SELECT * FROM bills_vendorbillingschedule ORDER BY id').fetchall()
    expected = native.execute(
        'SELECT expected_month, vendor_id, approved_invoice_count, status '
        'FROM reporting_invoice_coverage ORDER BY expected_month, vendor_id').fetchall()
for targets in (new, old, new):
    MigrationExecutor(connection).migrate(targets)
    assert snapshot() == before
    with sqlite3.connect(connection.settings_dict['NAME']) as native:
        assert native.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert native.execute('PRAGMA foreign_key_check').fetchall() == []
        if targets == old:
            assert native.execute(
                'SELECT * FROM bills_vendorbillingschedule ORDER BY id').fetchall() == schedules
        else:
            assert native.execute('SELECT count(*) FROM bills_propertyservice').fetchone() == (4,)
            assert native.execute(
                'SELECT count(*) FROM bills_servicevendorassignment').fetchone() == (6,)
            received = native.execute(
                'SELECT expected_month, vendor_id, approved_invoice_count, status '
                'FROM reporting_invoice_coverage WHERE vendor_id IS NOT NULL '
                'ORDER BY expected_month, vendor_id').fetchall()
            assert received == expected
            assert native.execute(
                "SELECT count(*) FROM reporting_invoice_coverage "
                "WHERE status = 'unassigned'").fetchone() == (6,)
print('Property services, vendor transitions, missing assignments and reversal verified.')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "house_bills.settings.development",
            "HOUSE_BILLS_DATA_DIR": str(tmp_path / "property-service-migration-data"),
            "HOUSE_BILLS_STATE_DIR": str(tmp_path / "property-service-migration-state"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
