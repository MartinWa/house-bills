"""Normalize classification without modifying invoice values or immutable JSON."""

from importlib import import_module

import django.db.models.deletion
from django.db import migrations, models
from django.db.models.functions import Coalesce

old_reports = import_module(".0013_vendor_billing_schedules", __package__)
old_coverage = import_module(".0017_legacy_invoice_coverage", __package__)

INITIAL_SUBCATEGORIES = [
    ("electricity_network", "electricity", "Electricity network (Elnät)"),
    ("electricity_supply", "electricity", "Electricity supply (Elhandel)"),
    ("matavfall", "waste", "Food waste (Matavfall)"),
    ("restavfall", "waste", "Residual waste (Restavfall)"),
    ("tank_emptying", "waste", "Tank emptying"),
    ("waste_collection", "waste", "Waste collection"),
]


def preflight(apps, schema_editor):
    from bills.schedule_migration import audit_legacy_configuration

    alias = schema_editor.connection.alias
    audit_legacy_configuration(
        list(apps.get_model("bills", "PropertyService").objects.using(alias).values()),
        list(apps.get_model("bills", "ServiceVendorAssignment").objects.using(alias).values()),
        list(apps.get_model("bills", "ServiceInvoiceException").objects.using(alias).values()),
    )


def forwards(apps, schema_editor):
    alias = schema_editor.connection.alias
    catalogue = apps.get_model("bills", "Subcategory")
    for key, category, name in INITIAL_SUBCATEGORIES:
        catalogue.objects.using(alias).create(key=key, category=category, name=name)
    for model in ("PropertyService", "BillingSection"):
        rows = apps.get_model("bills", model).objects.using(alias)
        for source in ("electricity_network", "electricity_supply"):
            rows.filter(category=source).update(category="electricity", subcategory=source)
        rows.filter(subcategory="").update(subcategory=None)
    usages = apps.get_model("bills", "Usage").objects.using(alias)
    for usage in usages.filter(utility="electricity").select_related("section"):
        usages.filter(pk=usage.pk).update(subcategory=usage.section.subcategory)
    usages.filter(subcategory="").update(subcategory=None)


def backwards(apps, schema_editor):
    alias = schema_editor.connection.alias
    expected_catalogue = {
        (key, category, name, True) for key, category, name in INITIAL_SUBCATEGORIES
    }
    catalogue = apps.get_model("bills", "Subcategory").objects.using(alias)
    if set(catalogue.values_list("key", "category", "name", "is_active")) != expected_catalogue:
        raise RuntimeError(
            "Changed subcategory catalogue cannot be represented by the old schema. "
            "Restore a backup instead."
        )
    usages = apps.get_model("bills", "Usage").objects.using(alias)
    if (
        usages.filter(utility="electricity")
        .exclude(subcategory=models.F("section__subcategory"))
        .exists()
    ):
        raise RuntimeError(
            "Independent electricity consumption classification cannot be represented by the "
            "old schema. Restore a backup instead."
        )
    # Refuse a lossy downgrade when the editable catalogue has been used.
    for model, allowed in (
        (
            "PropertyService",
            {"electricity_network", "electricity_supply", "waste_collection", "tank_emptying"},
        ),
        (
            "BillingSection",
            {"electricity_network", "electricity_supply", "waste_collection", "tank_emptying"},
        ),
        ("Usage", {"electricity_network", "electricity_supply", "matavfall", "restavfall"}),
    ):
        rows = apps.get_model("bills", model).objects.using(alias)
        if rows.exclude(subcategory__isnull=True).exclude(subcategory__in=allowed).exists():
            raise RuntimeError(
                "New classifications cannot be represented by the old schema. Restore a "
                "backup instead."
            )
    for model in ("PropertyService", "BillingSection"):
        rows = apps.get_model("bills", model).objects.using(alias)
        if (
            rows.filter(category="electricity")
            .exclude(subcategory__in=["electricity_network", "electricity_supply"])
            .exists()
        ):
            raise RuntimeError("Unspecified electricity cannot be represented by the old schema.")
        for source in ("electricity_network", "electricity_supply"):
            rows.filter(category="electricity", subcategory=source).update(
                category=source, subcategory=""
            )
        rows.filter(subcategory__isnull=True).update(subcategory="")
    usages = apps.get_model("bills", "Usage").objects.using(alias)
    usages.filter(utility="electricity").update(subcategory="")
    usages.filter(subcategory__isnull=True).update(subcategory="")


def permissions(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    for model in ("propertyidentifier", "invoicepropertyrule"):
        content_type = (
            ContentType.objects.using(schema_editor.connection.alias)
            .filter(app_label="bills", model=model)
            .first()
        )
        if content_type:
            for action in ("add", "change", "delete", "view"):
                Permission.objects.using(schema_editor.connection.alias).filter(
                    content_type=content_type, codename=f"{action}_propertyidentifier"
                ).update(
                    codename=f"{action}_invoicepropertyrule",
                    name=f"Can {action} invoice property rule",
                )


def restore_permissions(apps, schema_editor):
    Permission = apps.get_model("auth", "Permission")
    for action in ("add", "change", "delete", "view"):
        Permission.objects.using(schema_editor.connection.alias).filter(
            content_type__app_label="bills",
            codename=f"{action}_invoicepropertyrule",
        ).update(codename=f"{action}_propertyidentifier", name=f"Can {action} property mapping")


class Migration(migrations.Migration):
    dependencies = [
        ("bills", "0018_expected_invoice_month_labels"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]
    operations = [
        migrations.RunPython(preflight, reverse_code=migrations.RunPython.noop),
        migrations.RunSQL(
            "DROP VIEW reporting_invoice_coverage", reverse_sql=old_coverage.COVERAGE
        ),
        *[
            migrations.RunSQL(f"DROP VIEW {name}", reverse_sql=sql)
            for name, sql in reversed(old_reports.NEW_VIEWS)
        ],
        migrations.RenameModel("PropertyIdentifier", "InvoicePropertyRule"),
        migrations.AlterModelOptions(
            "invoicepropertyrule",
            {
                "verbose_name": "invoice property rule",
                "verbose_name_plural": "invoice property rules",
            },
        ),
        migrations.RunPython(permissions, restore_permissions),
        migrations.CreateModel(
            "Subcategory",
            fields=[
                ("key", models.SlugField(primary_key=True, max_length=64, serialize=False)),
                (
                    "category",
                    models.CharField(
                        max_length=20,
                        choices=[
                            ("electricity", "Electricity"),
                            ("water", "Water"),
                            ("waste", "Waste"),
                            ("other", "Other"),
                        ],
                    ),
                ),
                ("name", models.CharField(max_length=120)),
                ("is_active", models.BooleanField("enabled", default=True)),
            ],
            options={
                "ordering": ["category", "name", "pk"],
                "verbose_name_plural": "subcategories",
            },
        ),
        *[
            migrations.RemoveConstraint(model, name)
            for model, name in [
                ("propertyservice", "property_service_type_valid"),
                ("propertyservice", "property_service_subcategory_valid"),
                ("propertyservice", "property_service_start_unique"),
                ("billingsection", "section_service_valid"),
                ("billingsection", "section_subcategory_valid"),
                ("usage", "usage_subcategory_matches_utility"),
                ("usage", "electricity_usage_once_per_basis"),
            ]
        ],
        migrations.RemoveIndex("billingsection", "section_property_service"),
        *[
            migrations.RenameField(model, "service", "category")
            for model in ("propertyservice", "billingsection")
        ],
        *[
            migrations.AlterField(
                model, "subcategory", models.CharField(max_length=64, null=True, blank=True)
            )
            for model in ("propertyservice", "billingsection", "usage")
        ],
        migrations.RunPython(forwards, backwards),
        *[
            migrations.AlterField(
                model,
                "subcategory",
                models.ForeignKey(
                    "bills.subcategory",
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name=related,
                    null=True,
                    blank=True,
                    db_column="subcategory",
                ),
            )
            for model, related in (
                ("propertyservice", "property_services"),
                ("billingsection", "billing_sections"),
                ("usage", "usages"),
            )
        ],
        migrations.AddConstraint(
            "usage",
            models.UniqueConstraint(
                "section",
                "utility",
                Coalesce("subcategory", models.Value("")),
                "start",
                "end",
                "basis",
                condition=models.Q(utility="electricity"),
                name="electricity_usage_once_per_basis",
            ),
        ),
    ]
