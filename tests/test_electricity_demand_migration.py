"""Demand upgrades preserve existing records, views, constraints and catalogue labels."""

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("conflict", [False, True])
def test_demand_migration_preserves_records_and_rejects_lossy_downgrades(tmp_path, conflict):
    script = r"""
import django
django.setup()
from django.db import connection, transaction, IntegrityError
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

auth = ('auth', '0012_alter_user_first_name_max_length')
old = [('bills', '0026_coverage_exception_reasons'), auth]
new = [('bills', '0027_electricity_demand'), auth]
MigrationExecutor(connection).migrate(old)
apps = MigrationExecutor(connection).loader.project_state(old).apps
owner = apps.get_model('auth', 'User').objects.create(username='synthetic-owner')
house = apps.get_model('bills', 'Property').objects.create(name='Synthetic House', slug='house')
vendor = apps.get_model('bills', 'Vendor').objects.create(name='Synthetic Vendor', slug='vendor')
catalogue = apps.get_model('bills', 'Subcategory')
catalogue.objects.create(key='electricity_demand', category='water' if CONFLICT else 'electricity',
                         name='Custom demand label', is_active=False)
document = apps.get_model('bills', 'Document').objects.create(
    source='manual', sha256='', original_filename='', state='ready', storage_state='none')
invoice = apps.get_model('bills', 'Invoice').objects.create(
    document=document, vendor=vendor, reference='SYNTHETIC-OLD', invoice_date='2024-03-05',
    net_minor=10000, vat_minor=2500, rounding_minor=0, invoice_total_minor=12500,
    amount_due_minor=12500, status='approved', review_revision=1,
    approved_at=timezone.now(), approved_by=owner)
section = apps.get_model('bills', 'BillingSection').objects.create(
    invoice=invoice, property=house, category='electricity', subcategory_id='electricity_network',
    section_id='network', start='2024-02-01', end='2024-03-01',
    net_minor=10000, vat_minor=2500, gross_minor=12500)
apps.get_model('bills', 'Usage').objects.create(
    section=section, position=0, utility='electricity', subcategory_id='electricity_network',
    unit='kWh', quantity_milli=123456, start='2024-02-01', end='2024-03-01', basis='measured')
apps.get_model('bills', 'ReviewRevision').objects.create(
    invoice=invoice, revision=1, actor=owner, snapshot={'private':'Immutable synthetic snapshot'})

def facts():
    with connection.cursor() as cursor:
        return {
            table: cursor.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()
            for table in ('bills_document', 'bills_invoice', 'bills_billingsection',
                          'bills_usage', 'bills_reviewrevision')}

before = facts()
if CONFLICT:
    try:
        MigrationExecutor(connection).migrate(new)
    except RuntimeError as error:
        assert 'another category' in str(error), str(error)
    else:
        raise AssertionError('Conflicting catalogue keys must reject the upgrade')
    assert facts() == before
else:
    for target in (new, old, new):
        MigrationExecutor(connection).migrate(target)
        assert facts() == before
        with connection.cursor() as cursor:
            assert cursor.execute('PRAGMA foreign_key_check').fetchall() == []
            assert cursor.execute(
                'SELECT quantity_milli FROM reporting_electricity_usage').fetchall() == [(123456,)]
            cursor.execute('SELECT * FROM reporting_invoice_coverage LIMIT 1').fetchall()
        apps = MigrationExecutor(connection).loader.project_state(target).apps
        custom = apps.get_model('bills', 'Subcategory').objects.get(pk='electricity_demand')
        assert custom.name == 'Custom demand label' and not custom.is_active
    Document = apps.get_model('bills', 'Document')
    Invoice = apps.get_model('bills', 'Invoice')
    document = Document.objects.create(source='manual', sha256='', original_filename='',
                                      state='ready', storage_state='none')
    demand_invoice = Invoice.objects.create(
        document=document, vendor_id=vendor.pk, reference='SYNTHETIC-DEMAND',
        invoice_date='2024-03-05',
        net_minor=10000, vat_minor=2500, rounding_minor=0, invoice_total_minor=12500,
        amount_due_minor=12500, status='review')
    demand = apps.get_model('bills', 'BillingSection').objects.create(
        invoice=demand_invoice, property_id=house.pk, category='electricity',
        subcategory_id='electricity_demand', section_id='demand', start='2024-02-01',
        end='2024-03-01', net_minor=10000, vat_minor=2500, gross_minor=12500)
    Usage = apps.get_model('bills', 'Usage')
    values = dict(section=demand, position=0, utility='electricity',
        subcategory_id='electricity_demand', unit='kW', quantity_milli=3138,
        start='2024-02-01', end='2024-03-01', basis='billed')
    Usage.objects.create(**values)
    for subcategory, unit in (
        (None, 'kW'), ('electricity_network', 'kW'), ('electricity_demand', 'kWh')):
        try:
            with transaction.atomic():
                Usage.objects.create(**{
                    **values, 'position':99, 'subcategory_id':subcategory, 'unit':unit})
        except IntegrityError:
            pass
        else:
            raise AssertionError('Database must reject incompatible power classification')
    Invoice.objects.filter(pk=demand_invoice.pk).update(
        status='approved', review_revision=1, approved_at=timezone.now(), approved_by_id=owner.pk)
    with connection.cursor() as cursor:
        assert cursor.execute(
            'SELECT quantity_milli FROM reporting_electricity_demand').fetchall() == [(3138,)]
        assert cursor.execute(
            'SELECT quantity_milli FROM reporting_electricity_usage').fetchall() == [(123456,)]
    preserved = facts()
    try:
        MigrationExecutor(connection).migrate(old)
    except RuntimeError as error:
        assert 'Billed kW' in str(error), str(error)
    else:
        raise AssertionError('Downgrade must not discard billed power')
    assert facts() == preserved
    with connection.cursor() as cursor:
        assert cursor.execute(
            'SELECT quantity_milli FROM reporting_electricity_demand').fetchall() == [(3138,)]
"""
    result = subprocess.run(
        [sys.executable, "-c", script.replace("CONFLICT", repr(conflict))],
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "house_bills.settings.development",
            "HOUSE_BILLS_DATA_DIR": str(tmp_path / "demand-data"),
            "HOUSE_BILLS_STATE_DIR": str(tmp_path / "demand-state"),
        },
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
