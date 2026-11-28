"""The uniqueness upgrade rejects duplicate invoices and preserves all records."""

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("duplicates", [False, True])
def test_cardinality_migration_enforces_one_invoice_per_document(tmp_path, duplicates):
    script = r"""
import django
django.setup()
from django.db import connection, transaction, IntegrityError
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

auth = ('auth', '0012_alter_user_first_name_max_length')
old = [('bills', '0027_electricity_demand'), auth]
new = [('bills', '0028_invoice_document_unique'), auth]
MigrationExecutor(connection).migrate(old)
apps = MigrationExecutor(connection).loader.project_state(old).apps
Document = apps.get_model('bills', 'Document')
Invoice = apps.get_model('bills', 'Invoice')
owner = apps.get_model('auth', 'User').objects.create(username='synthetic-owner')
vendor = apps.get_model('bills', 'Vendor').objects.create(name='Synthetic Vendor', slug='vendor')
document = Document.objects.create(
    source='manual', sha256='', original_filename='', state='ready', storage_state='none')
Invoice.objects.create(
    document=document, vendor=vendor, reference='SYNTHETIC-FIRST', invoice_date='2024-03-05')
if DUPLICATES:
    Invoice.objects.create(
        document=document, vendor=vendor, reference='SYNTHETIC-SECOND', invoice_date='2024-04-05')

def facts():
    with connection.cursor() as cursor:
        return {
            table: cursor.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()
            for table in ('bills_document', 'bills_invoice', 'bills_billingsection')}

before = facts()
if DUPLICATES:
    try:
        MigrationExecutor(connection).migrate(new)
    except RuntimeError as error:
        assert 'more than one invoice' in str(error), str(error)
    else:
        raise AssertionError('Duplicate invoices must reject the upgrade')
    assert facts() == before
else:
    for target in (new, old, new):
        MigrationExecutor(connection).migrate(target)
        assert facts() == before
        with connection.cursor() as cursor:
            assert cursor.execute('PRAGMA foreign_key_check').fetchall() == []
    apps = MigrationExecutor(connection).loader.project_state(new).apps
    Document = apps.get_model('bills', 'Document')
    Invoice = apps.get_model('bills', 'Invoice')
    document = Document.objects.create(
        source='manual', sha256='', original_filename='', state='ready', storage_state='none')
    Invoice.objects.create(document=document, vendor_id=vendor.pk, reference='SYNTHETIC-ONLY')
    try:
        with transaction.atomic():
            Invoice.objects.create(
                document=document, vendor_id=vendor.pk, reference='SYNTHETIC-EXTRA')
    except IntegrityError:
        pass
    else:
        raise AssertionError('Database must reject a second invoice for one document')
    MigrationExecutor(connection).migrate(old)
    Invoice.objects.create(document=document, vendor_id=vendor.pk, reference='SYNTHETIC-EXTRA')
    assert Invoice.objects.filter(document=document).count() == 2
    assert owner.pk and vendor.pk
"""
    result = subprocess.run(
        [sys.executable, "-c", script.replace("DUPLICATES", repr(duplicates))],
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "house_bills.settings.development",
            "HOUSE_BILLS_DATA_DIR": str(tmp_path / "cardinality-data"),
            "HOUSE_BILLS_STATE_DIR": str(tmp_path / "cardinality-state"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    # django-axes announces itself once at startup; nothing else may reach stderr.
    noise = [line for line in result.stderr.splitlines() if not line.startswith("AXES: BEGIN")]
    assert not noise, result.stderr
