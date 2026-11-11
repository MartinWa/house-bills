"Disposable migration recovery with invented originals and exact immutable history."

import os
import subprocess
import sys

import pytest

SCRIPT = r"""
import hashlib
from datetime import date
from pathlib import Path
import django

django.setup()
from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

auth = ("auth", "0012_alter_user_first_name_max_length")
old = [("bills", "0018_expected_invoice_month_labels"), auth]
new = [("bills", "0022_restructured_reporting"), auth]
executor = MigrationExecutor(connection)
executor.migrate(old)
apps = executor.loader.project_state(old).apps
house = apps.get_model("bills", "Property").objects.create(
    name="Invented Migration House", slug="invented-house"
)
vendor = apps.get_model("bills", "Vendor").objects.create(
    name="Invented Migration Vendor", slug="invented-vendor"
)
actor = apps.get_model("auth", "User").objects.create(username="invented-migration-reviewer")
service = apps.get_model("bills", "PropertyService").objects.create(
    property=house,
    service="electricity_network",
    start_month=date(2024, 1, 1),
    frequency_months=1,
    grace_days=2,
)
water = apps.get_model("bills", "PropertyService").objects.create(
    property=house, service="water", start_month=date(2024, 1, 1), frequency_months=1, grace_days=2
)
tank = apps.get_model("bills", "PropertyService").objects.create(
    property=house,
    service="waste",
    subcategory="tank_emptying",
    start_month=date(2024, 1, 1),
    frequency_months=0,
)
Assignment = apps.get_model("bills", "ServiceVendorAssignment")
for configured in (service, water, tank):
    Assignment.objects.create(
        property_service=configured, vendor=vendor, start_month=configured.start_month
    )
apps.get_model("bills", "PropertyIdentifier").objects.create(
    vendor=vendor, property=house, kind="installation", value="000001"
)
apps.get_model("bills", "ServiceInvoiceException").objects.create(
    property_service=water,
    vendor=vendor,
    month=date(2024, 2, 1),
    reason="Invented original service annotation",
)
original = settings.MEDIA_ROOT / "inbox" / "invented.pdf"
original.parent.mkdir(parents=True, exist_ok=True)
original.write_bytes(b"%PDF-1.4\nWholly invented migration source\n%%EOF")
digest = hashlib.sha256(original.read_bytes()).hexdigest()
document = apps.get_model("bills", "Document").objects.create(
    sha256=digest,
    original_filename="invented.pdf",
    original_path="inbox/invented.pdf",
    storage_state="inbox",
    state="ready",
    extraction_revision=1,
)
invoice = apps.get_model("bills", "Invoice").objects.create(
    document=document,
    vendor=vendor,
    reference="INVENTED-MIGRATION-CREDIT",
    kind="credit",
    invoice_date=date(2024, 3, 5),
    net_minor=-3000,
    vat_minor=-750,
    rounding_minor=-1,
    invoice_total_minor=-3751,
    amount_due_minor=-3751,
    status="approved",
    approved_at=timezone.now(),
    approved_by=actor,
    review_revision=1,
)
evidence = {"page": 1, "text": "Invented signed migration evidence", "bbox": [0, 0, 100, 20]}
Section = apps.get_model("bills", "BillingSection")
network = Section.objects.create(
    invoice=invoice,
    property=house,
    section_id="network",
    service="electricity_network",
    net_minor=-1000,
    vat_minor=-250,
    gross_minor=-1250,
    start=date(2024, 1, 1),
    end=date(2024, 2, 1),
    period_evidence=evidence,
)
waste = Section.objects.create(
    invoice=invoice,
    property=house,
    section_id="waste",
    service="waste",
    subcategory="waste_collection",
    net_minor=-2000,
    vat_minor=-500,
    gross_minor=-2500,
    start=date(2024, 1, 1),
    end=date(2024, 2, 1),
    period_evidence=evidence,
)
Usage = apps.get_model("bills", "Usage")
Usage.objects.create(
    section=network,
    position=0,
    utility="electricity",
    unit="kWh",
    quantity_milli=-12345,
    basis="reversal",
    start=date(2023, 12, 1),
    end=date(2024, 1, 1),
    evidence=evidence,
)
for position, key, quantity in ((0, "matavfall", -1250), (1, "restavfall", -3500)):
    Usage.objects.create(
        section=waste,
        position=position,
        utility="waste",
        subcategory=key,
        unit="kg",
        quantity_milli=quantity,
        basis="reversal",
        start=date(2023, 12, 15),
        end=date(2024, 1, 15),
        evidence=evidence,
    )
legacy = {
    "sections": [
        {
            "section_id": "legacy",
            "service": "waste",
            "period_evidence": {
                "page": 1,
                "text": "Restavfall invented billed item",
                "bbox": [0, 0, 1, 1],
            },
        }
    ],
    "private": "exact original JSON, never rewritten",
}
attempt = apps.get_model("bills", "ExtractionAttempt").objects.create(
    document=document,
    revision=1,
    layout_family="synthetic",
    layout_version="legacy",
    parser_revision="original",
    state="review",
    candidate=legacy,
    page_evidence=[evidence],
)
apps.get_model("bills", "ReviewRevision").objects.create(
    invoice=invoice,
    revision=1,
    actor=actor,
    extraction=attempt,
    note="Original immutable approval",
    snapshot={
        "values": legacy,
        "properties": {"legacy": house.pk},
        "vendor_id": vendor.pk,
        "kind": "credit",
    },
)
manual = apps.get_model("bills", "Document").objects.create(
    source="manual", sha256="", original_filename="", storage_state="none", state="review"
)
apps.get_model("bills", "Invoice").objects.create(
    document=manual, vendor=vendor, reference="INVENTED-PENDING", invoice_date=date(2024, 1, 5)
)
ContentType = apps.get_model("contenttypes", "ContentType")
Permission = apps.get_model("auth", "Permission")
rule_type, _ = ContentType.objects.get_or_create(app_label="bills", model="propertyidentifier")
rule_permission = Permission.objects.create(
    content_type=rule_type, codename="change_propertyidentifier", name="Can change property mapping"
)
service_type, _ = ContentType.objects.get_or_create(app_label="bills", model="propertyservice")
service_permission = Permission.objects.create(
    content_type=service_type, codename="change_propertyservice", name="Can change property service"
)
actor.user_permissions.add(rule_permission, service_permission)


def snapshot(tables):
    with connection.cursor() as cursor:
        result = {}
        for table in tables:
            cursor.execute(f"SELECT * FROM {table} ORDER BY id")
            names = [column[0] for column in cursor.description]
            result[table] = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
        return result


immutable_tables = [
    "bills_document",
    "bills_invoice",
    "bills_extractionattempt",
    "bills_reviewrevision",
    "bills_property",
    "bills_vendor",
    "bills_servicevendorassignment",
]
immutable = snapshot(immutable_tables)
legacy_tables = [
    "bills_propertyservice",
    "bills_billingsection",
    "bills_usage",
    "bills_propertyidentifier",
    "bills_serviceinvoiceexception",
]
before = snapshot(legacy_tables)

if CONFLICT and CONFLICT != "independent_source":
    changes = {
        "frequency": {"frequency_months": 3},
        "phase": {"frequency_months": 3, "start_month": date(2024, 2, 1)},
        "grace": {"grace_days": 9},
    }[CONFLICT]
    if CONFLICT == "phase":
        apps.get_model("bills", "PropertyService").objects.filter(pk=service.pk).update(
            frequency_months=3
        )
        Assignment.objects.filter(property_service=water).update(start_month=date(2024, 2, 1))
    apps.get_model("bills", "PropertyService").objects.filter(pk=water.pk).update(**changes)
    immutable = snapshot(immutable_tables)
    try:
        MigrationExecutor(connection).migrate(new)
    except ValueError as exc:
        assert "conflicting frequency, phase or grace" in str(exc), str(exc)
    else:
        raise AssertionError("Conflicting vendor schedules must abort the migration")
    assert ("bills", "0019_shared_classification") not in MigrationExecutor(
        connection
    ).loader.applied_migrations
    assert snapshot(immutable_tables) == immutable
    assert original.read_bytes().startswith(b"%PDF-1.4")
else:
    for targets in (new, old, new):
        MigrationExecutor(connection).migrate(targets)
        assert snapshot(immutable_tables) == immutable
        assert hashlib.sha256(original.read_bytes()).hexdigest() == digest
        state = MigrationExecutor(connection).loader.project_state(targets).apps
        if targets == old:
            assert snapshot(legacy_tables) == before
            assert (
                Permission.objects.get(pk=rule_permission.pk).codename
                == "change_propertyidentifier"
            )
        else:
            assert (
                state.get_model("bills", "InvoicePropertyRule").objects.get().pk
                == before["bills_propertyidentifier"][0]["id"]
            )
            periods = list(state.get_model("bills", "VendorBillingSchedule").objects.all())
            assert (
                len(periods) == 1
                and periods[0].frequency_months == 1
                and periods[0].grace_days == 2
            )
            assert (
                state.get_model("bills", "PropertyService").objects.get(pk=tank.pk).mode
                == "occasional"
            )
            assert (
                state.get_model("bills", "Usage").objects.get(section_id=network.pk).subcategory_id
                == "electricity_network"
            )
            assert list(
                state.get_model("bills", "Usage")
                .objects.filter(section_id=waste.pk)
                .values_list("subcategory_id", "quantity_milli")
            ) == [("matavfall", -1250), ("restavfall", -3500)]
            assert (
                Permission.objects.get(pk=rule_permission.pk).codename
                == "change_invoicepropertyrule"
            )
            assert actor.user_permissions.filter(codename="change_vendorbillingschedule").exists()
    if CONFLICT == "independent_source":
        state.get_model("bills", "Usage").objects.filter(section_id=network.pk).update(
            subcategory_id="electricity_supply"
        )
        independent = snapshot(["bills_billingsection", "bills_usage"])
        try:
            MigrationExecutor(connection).migrate(old)
        except RuntimeError as exc:
            assert "Independent electricity consumption classification" in str(exc), str(exc)
        else:
            raise AssertionError("Downgrade must preserve independent consumption classification")
        assert snapshot(["bills_billingsection", "bills_usage"]) == independent
        assert snapshot(immutable_tables) == immutable
        MigrationExecutor(connection).migrate(new)
        assert snapshot(["bills_billingsection", "bills_usage"]) == independent
        assert snapshot(immutable_tables) == immutable
        assert hashlib.sha256(original.read_bytes()).hexdigest() == digest
    call_command("check", databases=["default"])
print("Synthetic model-restructure migration preservation and recovery passed.")
"""


@pytest.mark.parametrize("conflict", ["", "frequency", "phase", "grace", "independent_source"])
def test_restructure_migration_preserves_and_recovers_or_aborts_conflicts(tmp_path, conflict):
    environment = {
        **os.environ,
        "DJANGO_SETTINGS_MODULE": "house_bills.settings.test",
        "HOUSE_BILLS_STATE_DIR": str(tmp_path),
    }
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT.replace("CONFLICT", repr(conflict))],
        env=environment,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
