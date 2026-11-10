"""Exercise the old-to-new data contract in a separate, disposable SQLite database."""

import os
import subprocess
import sys


def test_billing_schedule_migration_preserves_rows_and_supports_native_sqlite_and_reversal(
    tmp_path,
):
    data = tmp_path / "schedule-migration-data"
    data.mkdir()
    script = r"""
import sqlite3
from datetime import date
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

auth = ('auth', '0012_alter_user_first_name_max_length')
old = [('bills', '0012_property_product_identifier'), auth]
new = [('bills', '0014_invoice_coverage_view'), auth]
executor = MigrationExecutor(connection)
executor.migrate(old)
apps = executor.loader.project_state(old).apps
actor = apps.get_model('auth', 'User').objects.create(username='synthetic-schedule-migration')
house = apps.get_model('bills', 'Property').objects.create(name='Synthetic House', slug='house')
vendor = apps.get_model('bills', 'Vendor').objects.create(name='Synthetic Vendor', slug='vendor')
for index, source_text in enumerate([
    'Slamavskiljare 1.1-2 m³ ordinarie tömning 2024-02-06 - 2024-02-06',
    'Grundavgift permanentbostad 2024-01-01 - 2024-03-31',
    'Manually reviewed unspecified waste',
], 1):
    evidence = {'page': 1, 'text': source_text, 'bbox': [0, 0, 1, 1]}
    document = apps.get_model('bills', 'Document').objects.create(
        sha256=f'{index:064x}', original_filename=f'synthetic-{index}.pdf')
    invoice = apps.get_model('bills', 'Invoice').objects.create(
        document=document, vendor=vendor, reference=f'SYNTHETIC-SCHEDULE-{index}',
        invoice_date=date(2024, 3, 5), net_minor=1000, vat_minor=250, rounding_minor=0,
        invoice_total_minor=1250, amount_due_minor=1250, status='approved',
        approved_at=timezone.now(), approved_by=actor, review_revision=1)
    apps.get_model('bills', 'BillingSection').objects.create(
        invoice=invoice, property=house, section_id='section', service='waste',
        net_minor=1000, vat_minor=250, gross_minor=1250,
        start=date(2024, 2, 1), end=date(2024, 3, 1), period_evidence=evidence)
    attempt = apps.get_model('bills', 'ExtractionAttempt').objects.create(
        document=document, revision=1, state='review', layout_family='layout_03',
        layout_version='v1', parser_revision='synthetic', candidate={'private': 'retained'},
        page_evidence=[evidence])
    apps.get_model('bills', 'ReviewRevision').objects.create(
        invoice=invoice, revision=1, actor=actor, extraction=attempt,
        snapshot={'private': 'approval retained byte for byte'})

tables = ['auth_user', 'bills_property', 'bills_vendor', 'bills_document',
          'bills_invoice', 'bills_billingsection', 'bills_usage',
          'bills_extractionattempt', 'bills_reviewrevision']
def snapshot():
    with connection.cursor() as cursor:
        result = {}
        for table in tables:
            columns = [row[1] for row in cursor.execute(f'PRAGMA table_info({table})').fetchall()
                       if table != 'bills_billingsection' or row[1] != 'subcategory']
            result[table] = cursor.execute(
                f"SELECT {', '.join(columns)} FROM {table} ORDER BY id").fetchall()
        return result
before = snapshot()
for targets in (new, old, new):
    MigrationExecutor(connection).migrate(targets)
    assert snapshot() == before
    if targets == new:
        with connection.cursor() as cursor:
            classifications = cursor.execute(
                'SELECT subcategory FROM bills_billingsection ORDER BY id').fetchall()
            assert classifications == [('tank_emptying',), ('waste_collection',), ('',)]
        apps = MigrationExecutor(connection).loader.project_state(new).apps
        apps.get_model('bills', 'VendorBillingSchedule').objects.create(
            vendor_id=vendor.pk, property_id=house.pk, service='waste',
            subcategory='waste_collection', start_month=date(2024, 3, 1),
            end_month=date(2024, 3, 1), frequency_months=1)
        with sqlite3.connect(connection.settings_dict['NAME']) as native:
            assert native.execute('PRAGMA integrity_check').fetchone() == ('ok',)
            total = native.execute('SELECT sum(gross_minor) FROM reporting_costs').fetchone()
            assert total == (3750,)
            receipt = native.execute(
                'SELECT approved_invoice_count, status FROM reporting_invoice_coverage').fetchone()
            assert receipt == (1, 'received')
print('Billing schedules, waste classification, native SQLite and reversal verified.')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "house_bills.settings.development",
            "HOUSE_BILLS_DATA_DIR": str(data),
            "HOUSE_BILLS_STATE_DIR": str(tmp_path / "schedule-migration-state"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


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
# Runtime validation uses the current model schema; historical assertions above
# still exercise the cost-total migration independently.
latest = MigrationExecutor(connection)
latest.migrate(latest.loader.graph.leaf_nodes('bills') + [old_targets[1]])
validate_allocations(CurrentInvoice.objects.get(pk=invoice.pk))
MigrationExecutor(connection).migrate(new_targets)

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


def test_subcategory_migration_classifies_evidence_preserves_history_and_reverses_views(tmp_path):
    data = tmp_path / "subcategory-migration-data"
    data.mkdir()
    script = r"""
from datetime import date
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

auth = ('auth', '0012_alter_user_first_name_max_length')
old_targets = [('bills', '0008_reporting_views'), auth]
new_targets = [('bills', '0009_usage_subcategories'), auth]
executor = MigrationExecutor(connection)
executor.migrate(old_targets)
apps = executor.loader.project_state(old_targets).apps
actor = apps.get_model('auth', 'User').objects.create(username='synthetic-subcategory-reviewer')
house = apps.get_model('bills', 'Property').objects.create(name='Synthetic House', slug='house')
vendor = apps.get_model('bills', 'Vendor').objects.create(name='Synthetic Utilities', slug='vendor')
document = apps.get_model('bills', 'Document').objects.create(
    sha256='c' * 64, original_filename='synthetic.pdf')
invoice = apps.get_model('bills', 'Invoice').objects.create(
    document=document, vendor=vendor, reference='SYNTHETIC-SUBCATEGORY-001',
    invoice_date=date(2024, 3, 5), net_minor=3000, vat_minor=750, rounding_minor=0,
    invoice_total_minor=3750, amount_due_minor=3750, status='approved',
    approved_at=timezone.now(), approved_by=actor, review_revision=1)
start, end = date(2024, 2, 1), date(2024, 3, 1)
Usage = apps.get_model('bills', 'Usage')
for service, utility, unit, source_rows in (
    ('waste', 'waste', 'kg', [
        'Matavfall 140L kärl 12 kg', 'restavfall 190L kärl 7,5 kg',
        'Manually reviewed: usages',
    ]),
    ('electricity_network', 'electricity', 'kWh', ['Wholly synthetic reading']),
    ('water', 'water', 'm3', ['Restavfall in unrelated synthetic evidence']),
):
    section = apps.get_model('bills', 'BillingSection').objects.create(
        invoice=invoice, property=house, section_id=service, service=service,
        net_minor=1000, vat_minor=250, gross_minor=1250, start=start, end=end)
    for position, source in enumerate(source_rows):
        Usage.objects.create(section=section, position=position, utility=utility, unit=unit,
            quantity_milli=1000, start=start, end=end, basis='billed',
            evidence={'page': 1, 'text': source, 'bbox': [0, 0, 1, 1]})
attempt = apps.get_model('bills', 'ExtractionAttempt').objects.create(
    document=document, revision=1, state='review', layout_family='layout_01',
    layout_version='v1', parser_revision='4', candidate={'private': 'unchanged candidate'})
review = apps.get_model('bills', 'ReviewRevision').objects.create(
    invoice=invoice, revision=1, actor=actor, extraction=attempt,
    snapshot={'private': 'unchanged approval'})
tables = ['auth_user', 'bills_property', 'bills_vendor', 'bills_document',
          'bills_invoice', 'bills_billingsection', 'bills_extractionattempt',
          'bills_reviewrevision']
def snapshot():
    with connection.cursor() as cursor:
        return {table: cursor.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()
                for table in tables}
before = snapshot()
before_usage = list(Usage.objects.order_by('pk').values())
expected = ['matavfall', 'restavfall', '', '', '']
for targets in (new_targets, old_targets, new_targets):
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    apps = executor.loader.project_state(targets).apps
    assert snapshot() == before
    current = list(apps.get_model('bills', 'Usage').objects.order_by('pk').values())
    if targets == new_targets:
        assert [row.pop('subcategory') for row in current] == expected
        with connection.cursor() as cursor:
            assert cursor.execute(
                'SELECT subcategory FROM reporting_usage ORDER BY usage_id').fetchall() == [
                ('matavfall',), ('restavfall',), ('',), ('electricity_network',), ('',)]
            assert cursor.execute(
                'SELECT sum(gross_minor) FROM reporting_costs').fetchone() == (3750,)
            assert cursor.execute(
                'SELECT quantity_milli FROM reporting_electricity_usage').fetchone() == (1000,)
    else:
        with connection.cursor() as cursor:
            assert 'subcategory' not in [row[1] for row in cursor.execute(
                'PRAGMA table_info(bills_usage)').fetchall()]
            assert cursor.execute('SELECT count(*) FROM reporting_usage').fetchone() == (5,)
            assert cursor.execute(
                'SELECT quantity_milli FROM reporting_electricity_usage').fetchone() == (1000,)
    assert current == before_usage
print('Subcategories, retained history and reversible reporting views verified.')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "house_bills.settings.development",
            "HOUSE_BILLS_DATA_DIR": str(data),
            "HOUSE_BILLS_STATE_DIR": str(tmp_path / "subcategory-migration-state"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_reporting_migration_is_reversible_and_preserves_all_approved_and_private_rows(tmp_path):
    data = tmp_path / "reporting-migration-data"
    data.mkdir()
    script = r"""
from datetime import date
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

auth = ('auth', '0012_alter_user_first_name_max_length')
old_targets = [('bills', '0007_cost_totals'), auth]
new_targets = [('bills', '0008_reporting_views'), auth]
executor = MigrationExecutor(connection)
executor.migrate(old_targets)
apps = executor.loader.project_state(old_targets).apps
actor = apps.get_model('auth', 'User').objects.create(username='synthetic-report-migration')
house = apps.get_model('bills', 'Property').objects.create(name='Synthetic House', slug='house')
vendor = apps.get_model('bills', 'Vendor').objects.create(name='Synthetic Utilities', slug='vendor')
document = apps.get_model('bills', 'Document').objects.create(
    sha256='b' * 64, original_filename='synthetic.pdf',
    legacy_charge_details=[{'private': 'retained'}])
invoice = apps.get_model('bills', 'Invoice').objects.create(
    document=document, vendor=vendor, reference='SYNTHETIC-MIGRATION-001',
    invoice_date=date(2024, 3, 5), net_minor=1000, vat_minor=250, rounding_minor=-1,
    invoice_total_minor=1249, amount_due_minor=1249, status='approved',
    approved_at=timezone.now(), approved_by=actor, review_revision=1)
section = apps.get_model('bills', 'BillingSection').objects.create(
    invoice=invoice, property=house, section_id='section', service='electricity_network',
    net_minor=1000, vat_minor=250, gross_minor=1250,
    start=date(2024, 2, 1), end=date(2024, 3, 1))
evidence = {'page': 1, 'text': 'Wholly synthetic evidence', 'bbox': [0, 0, 1, 1]}
apps.get_model('bills', 'Usage').objects.create(
    section=section, position=0, utility='electricity', unit='kWh', quantity_milli=123456,
    start=date(2024, 2, 1), end=date(2024, 3, 1), basis='measured', evidence=evidence)
attempt = apps.get_model('bills', 'ExtractionAttempt').objects.create(
    document=document, revision=1, state='review', layout_family='layout_01',
    layout_version='v1', parser_revision='synthetic', candidate={'private': 'candidate'},
    page_evidence=[evidence])
apps.get_model('bills', 'ReviewRevision').objects.create(
    invoice=invoice, revision=1, actor=actor, extraction=attempt, snapshot={'private': 'approval'})
tables = ['auth_user', 'bills_property', 'bills_vendor', 'bills_document',
          'bills_invoice', 'bills_billingsection', 'bills_usage',
          'bills_extractionattempt', 'bills_reviewrevision']
def snapshot():
    with connection.cursor() as cursor:
        return {table: cursor.execute(f'SELECT * FROM {table} ORDER BY id').fetchall()
                for table in tables}
def views():
    with connection.cursor() as cursor:
        return {row[0] for row in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type = 'view'").fetchall()}
before = snapshot()
expected_views = {'reporting_invoices', 'reporting_costs', 'reporting_usage',
                  'reporting_electricity_selection', 'reporting_electricity_usage'}
for targets in (new_targets, old_targets, new_targets):
    MigrationExecutor(connection).migrate(targets)
    assert snapshot() == before
    assert views() == (expected_views if targets == new_targets else set())
    if targets == new_targets:
        with connection.cursor() as cursor:
            assert cursor.execute(
                'SELECT sum(gross_minor) FROM reporting_costs').fetchone() == (1250,)
            assert cursor.execute(
                'SELECT quantity_milli FROM reporting_electricity_usage').fetchone() == (123456,)
            indexes = {row[1] for row in cursor.execute(
                'PRAGMA index_list(bills_usage)').fetchall()}
            assert 'usage_utility_period' in indexes
print('Reporting migration, reversal and exact row preservation verified.')
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "house_bills.settings.development",
            "HOUSE_BILLS_DATA_DIR": str(data),
            "HOUSE_BILLS_STATE_DIR": str(tmp_path / "reporting-migration-state"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
