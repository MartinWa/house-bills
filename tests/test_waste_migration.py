"""Upgrade/reversal preserves approved facts and exact immutable review JSON."""

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("conflict", [False, True])
def test_waste_migration_preserves_facts_labels_and_legacy_defaults(tmp_path, conflict):
    script = r"""
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

old = [('bills', '0024_replacement_lineage')]
new = [('bills', '0025_waste_fractions')]
executor = MigrationExecutor(connection)
executor.migrate(old)
apps = executor.loader.project_state(old).apps
owner = apps.get_model('auth', 'User').objects.create(username='synthetic-owner')
house = apps.get_model('bills', 'Property').objects.create(name='House', slug='house')
vendor = apps.get_model('bills', 'Vendor').objects.create(name='Vendor', slug='vendor')
Catalogue = apps.get_model('bills', 'Subcategory')
Catalogue.objects.create(key='tradgardsavfall', category='waste',
                         name='Custom garden label', is_active=False)
Catalogue.objects.filter(pk='matavfall').update(name='Custom food label', is_active=False)
if CONFLICT:
    Catalogue.objects.create(key='plastic', category='other', name='Conflicting category')
else:
    Catalogue.objects.create(key='plastic', category='waste',
                             name='Custom plastic label', is_active=True)
services = []
for category, subcategory in [('waste','waste_collection'), ('waste','tradgardsavfall'),
                              ('waste','tank_emptying'), ('water',None)]:
    service = apps.get_model('bills', 'PropertyService').objects.create(
        property=house, category=category, subcategory_id=subcategory, start_month='2024-01-01')
    services.append(service)
    apps.get_model('bills', 'ServiceVendorAssignment').objects.create(
        property_service=service, vendor=vendor, start_month='2024-01-01')
apps.get_model('bills', 'VendorBillingSchedule').objects.create(
    vendor=vendor, start_month='2024-01-01', first_expected_month='2024-01-01', frequency_months=1)
document = apps.get_model('bills', 'Document').objects.create(
    source='manual', sha256='', original_filename='', state='ready', storage_state='none')
invoice = apps.get_model('bills', 'Invoice').objects.create(
    document=document, vendor=vendor, reference='SYNTHETIC', invoice_date='2024-03-05',
    net_minor=20000, vat_minor=5000, rounding_minor=0, invoice_total_minor=25000,
    amount_due_minor=25000, status='approved', approved_at=timezone.now(),
    approved_by=owner, review_revision=1)
Section = apps.get_model('bills', 'BillingSection')
combined = Section.objects.create(invoice=invoice, property=house, category='waste',
    subcategory_id='waste_collection', section_id='combined', start='2024-02-01', end='2024-03-01',
    net_minor=16000, vat_minor=4000, gross_minor=20000)
Section.objects.create(invoice=invoice, property=house, category='waste',
    subcategory_id='tradgardsavfall', section_id='garden', start='2024-02-01', end='2024-03-01',
    net_minor=4000, vat_minor=1000, gross_minor=5000)
apps.get_model('bills', 'Usage').objects.create(section=combined, position=0, utility='waste',
    subcategory_id='matavfall', unit='kg', quantity_milli=0, start='2024-02-01', end='2024-03-01',
    basis='billed_weight', evidence={'page':1,'text':'Matavfall 0 kg','bbox':[0,0,100,20]})
apps.get_model('bills', 'ReviewRevision').objects.create(invoice=invoice, revision=1, actor=owner,
    snapshot={'values':{'sections':[{'service':'waste','subcategory':'waste_collection'}]},
              'private': 'Exact immutable JSON, including combined classification'})

def facts():
    with connection.cursor() as cursor:
        return {table:cursor.execute(f'SELECT * FROM {table} ORDER BY id').fetchall() for table in (
            'bills_document','bills_invoice','bills_billingsection','bills_usage','bills_reviewrevision',
            'bills_servicevendorassignment','bills_vendorbillingschedule')}

before = facts()
executor = MigrationExecutor(connection)
if CONFLICT:
    try:
        executor.migrate(new)
    except RuntimeError as error:
        assert 'another category' in str(error)
    else:
        raise AssertionError('Catalogue conflict must reject upgrade')
    assert facts() == before
    with connection.cursor() as cursor:
        columns = cursor.execute('PRAGMA table_info(bills_propertyservice)').fetchall()
        assert 'service_group' not in [row[1] for row in columns]
        cursor.execute('SELECT * FROM reporting_invoice_coverage LIMIT 1').fetchall()
else:
    for target in (new, old, new):
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        assert facts() == before
        with connection.cursor() as cursor:
            cursor.execute('SELECT * FROM reporting_invoice_coverage LIMIT 1').fetchall()
            assert cursor.execute('PRAGMA foreign_key_check').fetchall() == []
        if target == new:
            apps = executor.loader.project_state(new).apps
            assert apps.get_model('bills','ServiceCostSchedule').objects.count() == 0
            current = apps.get_model('bills','PropertyService')
            assert [current.objects.get(pk=s.pk).service_group for s in services] == [
                'waste_collection','waste_collection','tank_emptying','water']
            catalogue = apps.get_model('bills','Subcategory')
            for key, label, active in [('matavfall','Custom food label',False),
                                       ('tradgardsavfall','Custom garden label',False),
                                       ('plastic','Custom plastic label',True)]:
                row = catalogue.objects.get(pk=key)
                assert row.is_waste_fraction and row.name == label and row.is_active == active
            assert not catalogue.objects.get(pk='cardboard').is_active
            assert not catalogue.objects.get(pk='glass').is_active
            with connection.cursor() as cursor:
                costs = cursor.execute(
                    'SELECT sum(gross_minor) FROM reporting_waste_costs').fetchone()
                assert costs == (25000,)
                weights = cursor.execute(
                    'SELECT subcategory,quantity_milli FROM reporting_waste_usage').fetchall()
                assert weights == [('matavfall',0)]
"""
    result = subprocess.run(
        [sys.executable, "-c", script.replace("CONFLICT", repr(conflict))],
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "house_bills.settings.development",
            "HOUSE_BILLS_DATA_DIR": str(tmp_path / "waste-data"),
            "HOUSE_BILLS_STATE_DIR": str(tmp_path / "waste-state"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_coverage_reason_migration_preserves_existing_exceptions_and_reverses(tmp_path):
    script = r"""
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

old = [('bills', '0025_waste_fractions')]
new = [('bills', '0026_coverage_exception_reasons')]
executor = MigrationExecutor(connection)
executor.migrate(old)
apps = executor.loader.project_state(old).apps
house = apps.get_model('bills', 'Property').objects.create(name='House', slug='house')
vendor = apps.get_model('bills', 'Vendor').objects.create(name='Vendor', slug='vendor')
service = apps.get_model('bills', 'PropertyService').objects.create(
    property=house, category='waste', subcategory_id='restavfall',
    service_group='waste_collection', start_month='2024-01-01')
assignment = apps.get_model('bills', 'ServiceVendorAssignment').objects.create(
    property_service=service, vendor=vendor, start_month='2024-01-01')
apps.get_model('bills', 'VendorBillingSchedule').objects.create(
    vendor=vendor, start_month='2024-01-01', first_expected_month='2024-01-01', frequency_months=1)
apps.get_model('bills', 'ServiceCostSchedule').objects.create(
    service_vendor_assignment=assignment, start_month='2024-01-01', frequency_months=0)
reason = 'Synthetic vendor confirmed no invoice was sent'
apps.get_model('bills', 'ServiceInvoiceException').objects.create(
    vendor=vendor, month='2024-02-01', reason=reason)

with connection.cursor() as cursor:
    before = cursor.execute('SELECT * FROM bills_serviceinvoiceexception').fetchall()
for target in (new, old, new):
    MigrationExecutor(connection).migrate(target)
    with connection.cursor() as cursor:
        assert cursor.execute('SELECT * FROM bills_serviceinvoiceexception').fetchall() == before
        assert cursor.execute(
            "SELECT status FROM reporting_service_coverage WHERE expected_month='2024-02-01'"
        ).fetchone() == ('not_due',)
        assert cursor.execute(
            "SELECT status, exception_reason FROM reporting_invoice_coverage "
            "WHERE expected_month='2024-02-01'"
        ).fetchone() == ('not_expected', reason if target == new else None)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "house_bills.settings.development",
            "HOUSE_BILLS_DATA_DIR": str(tmp_path / "coverage-data"),
            "HOUSE_BILLS_STATE_DIR": str(tmp_path / "coverage-state"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
