"""Backfill dated lineage without losing rows, views, or cross-row protections."""

import os
import subprocess
import sys


def test_lineage_migration_backfills_valid_audit_entries_and_preserves_sqlite_schema(tmp_path):
    script = r"""
import json
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

old = [('bills', '0023_document_idempotency_key_apitoken'),
       ('admin', '0003_logentry_add_action_flag_choices')]
new = [('bills', '0024_replacement_lineage')]
executor = MigrationExecutor(connection)
executor.migrate(old)
apps = executor.loader.project_state(old).apps
owner = apps.get_model('auth', 'User').objects.create(username='synthetic-owner')
house = apps.get_model('bills', 'Property').objects.create(name='House', slug='house')
vendors = [apps.get_model('bills', 'Vendor').objects.create(name=f'Vendor {i}', slug=f'vendor-{i}')
           for i in range(2)]
service = apps.get_model('bills', 'PropertyService').objects.create(
    property=house, category='water', mode='regular', is_active=True, start_month='2026-01-01')
assignments = [apps.get_model('bills', 'ServiceVendorAssignment').objects.create(
    property_service=service, vendor=vendor, is_active=True,
    start_month=f'{2026+i}-01-01', end_month='2026-12-01' if i == 0 else None)
    for i, vendor in enumerate(vendors)]
schedules = [apps.get_model('bills', 'VendorBillingSchedule').objects.create(
    vendor=vendors[0], is_active=True, start_month=f'{2026+i}-01-01',
    end_month=f'{2026+i}-12-01' if i < 2 else None,
    first_expected_month=f'{2026+i}-01-01', frequency_months=1, grace_days=0)
    for i in range(3)]
period_groups = [('ServiceVendorAssignment', assignments), ('VendorBillingSchedule', schedules)]
for model_name, periods in period_groups:
    content_type, _ = apps.get_model('contenttypes', 'ContentType').objects.get_or_create(
        app_label='bills', model=model_name.lower())
    def log(message, object_id):
        apps.get_model('admin', 'LogEntry').objects.create(
            user=owner, content_type=content_type, object_id=str(object_id),
            object_repr='Synthetic period', action_flag=1, change_message=message)
    # Bad historic audit data must neither abort the migration nor invent links.
    for message in ['"api_replacement"', '{"api_replacement": {}}',
                    '["api_replacement"]', '[{"api_replacement": null}]',
                    '[{"api_replacement": []}]', '[{"api_replacement":']:
        log(message, periods[-1].pk)
    for predecessor_id in [True, '1', 999999]:
        log(json.dumps([{'api_replacement': {'predecessor_id': predecessor_id,
            'replacement_id': periods[-1].pk}}]), periods[-1].pk)
    for predecessor, replacement in zip(periods, periods[1:]):
        log(json.dumps([{'added': {}}, {'api_replacement': {
            'predecessor_id': predecessor.pk, 'replacement_id': replacement.pk}}]), replacement.pk)

def schema_objects():
    with connection.cursor() as cursor:
        return dict(cursor.execute(
            "SELECT name, sql FROM sqlite_master WHERE type IN ('view', 'trigger')").fetchall())
def period_rows():
    with connection.cursor() as cursor:
        return {table: cursor.execute(
            f'SELECT {", ".join(columns)} FROM {table} ORDER BY id').fetchall()
            for table, columns in original_columns.items()}
with connection.cursor() as cursor:
    original_columns = {
        table: [row[1] for row in cursor.execute(f'PRAGMA table_info({table})').fetchall()]
        for table in ['bills_servicevendorassignment', 'bills_vendorbillingschedule']}
before_schema, before_rows = schema_objects(), period_rows()
for targets in (new, old, new):
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    assert schema_objects() == before_schema
    assert period_rows() == before_rows
    with connection.cursor() as cursor:
        for name in before_schema:
            if name.startswith('reporting_'):
                cursor.execute(f'SELECT * FROM {name} LIMIT 1').fetchall()
    if targets == new:
        state = executor.loader.project_state(new).apps
        for model_name, periods in period_groups:
            model = state.get_model('bills', model_name)
            assert model.objects.get(pk=periods[0].pk).predecessor_id is None
            for predecessor, replacement in zip(periods, periods[1:]):
                assert model.objects.get(pk=replacement.pk).predecessor_id == predecessor.pk
        with connection.cursor() as cursor:
            assert cursor.execute('PRAGMA foreign_key_check').fetchall() == []
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "house_bills.settings.development",
            "HOUSE_BILLS_DATA_DIR": str(tmp_path / "lineage-data"),
            "HOUSE_BILLS_STATE_DIR": str(tmp_path / "lineage-state"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
