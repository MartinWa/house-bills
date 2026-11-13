"""Track demand charges and billed kW independently of electricity consumption."""

from importlib import import_module

from django.db import migrations, models

from ._waste_fractions import TRIGGERS, VIEWS

COVERAGE = import_module(".0026_coverage_exception_reasons", __package__).COVERAGE

OLD_VIEWS = [
    (name, COVERAGE if name == "reporting_invoice_coverage" else sql) for name, sql in VIEWS
]
NEW_VIEWS = [
    (
        name,
        sql.replace(
            "WHERE utility = 'electricity'", "WHERE utility = 'electricity' AND unit = 'kWh'"
        )
        if name == "reporting_electricity_selection"
        else sql,
    )
    for name, sql in OLD_VIEWS
] + [
    (
        "reporting_electricity_demand",
        "CREATE VIEW reporting_electricity_demand AS "
        "SELECT * FROM reporting_usage WHERE utility='electricity' AND unit='kW'",
    )
]


def seed_catalogue(apps, schema_editor):
    catalogue = apps.get_model("bills", "Subcategory").objects.using(schema_editor.connection.alias)
    row, _ = catalogue.get_or_create(
        pk="electricity_demand", defaults={"category": "electricity", "name": "Effektavgift"}
    )
    if row.category != "electricity" or row.is_waste_fraction:
        raise RuntimeError(
            "The electricity_demand key belongs to another category; resolve the catalogue "
            "conflict first."
        )


def unseed_catalogue(apps, schema_editor):
    alias = schema_editor.connection.alias
    for model in ("PropertyService", "BillingSection", "Usage"):
        if (
            apps.get_model("bills", model)
            .objects.using(alias)
            .filter(subcategory_id="electricity_demand")
            .exists()
        ):
            return
    apps.get_model("bills", "Subcategory").objects.using(alias).filter(
        pk="electricity_demand",
        category="electricity",
        name="Effektavgift",
        is_active=True,
        is_waste_fraction=False,
    ).delete()


def check_downgrade(apps, schema_editor):
    if (
        apps.get_model("bills", "Usage")
        .objects.using(schema_editor.connection.alias)
        .filter(unit="kW")
        .exists()
    ):
        raise RuntimeError(
            "Billed kW cannot be represented by the old schema. Restore a backup instead."
        )


class Migration(migrations.Migration):
    dependencies = [("bills", "0026_coverage_exception_reasons")]

    operations = [
        migrations.RunPython(seed_catalogue, unseed_catalogue),
        *[migrations.RunSQL(f"DROP TRIGGER {name}", reverse_sql=sql) for name, sql in TRIGGERS],
        *[
            migrations.RunSQL(f"DROP VIEW {name}", reverse_sql=sql)
            for name, sql in reversed(OLD_VIEWS)
        ],
        migrations.RemoveConstraint(model_name="usage", name="usage_unit_matches_utility"),
        migrations.AlterField(
            model_name="usage",
            name="unit",
            field=models.CharField(
                max_length=8, choices=[("kWh", "kWh"), ("kW", "kW"), ("m3", "m³"), ("kg", "kg")]
            ),
        ),
        migrations.AddConstraint(
            model_name="usage",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(utility="electricity", unit="kWh")
                    & ~models.Q(subcategory="electricity_demand")
                )
                | models.Q(
                    utility="electricity",
                    unit="kW",
                    subcategory="electricity_demand",
                    subcategory__isnull=False,
                )
                | models.Q(utility="water", unit="m3")
                | models.Q(utility="waste", unit="kg")
                | models.Q(utility="other", unit__in=["kWh", "m3", "kg"]),
                name="usage_unit_matches_utility",
            ),
        ),
        *[migrations.RunSQL(sql, reverse_sql=f"DROP VIEW {name}") for name, sql in NEW_VIEWS],
        *[migrations.RunSQL(sql, reverse_sql=f"DROP TRIGGER {name}") for name, sql in TRIGGERS],
        migrations.RunPython(migrations.RunPython.noop, check_downgrade),
    ]
