"""Classify billed waste evidence and extend the reversible reporting projections."""

import re
from importlib import import_module

from django.db import migrations, models

previous = import_module(".0008_reporting_views", __package__)
COSTS = previous.COSTS.replace(
    "    s.service,\n",
    """    s.service,
    CASE WHEN s.service IN ('electricity_network', 'electricity_supply')
         THEN 'electricity' ELSE s.service END AS category,
    CASE WHEN s.service IN ('electricity_network', 'electricity_supply')
         THEN s.service ELSE '' END AS subcategory,
""",
)
USAGE = previous.USAGE.replace(
    "    u.utility,\n",
    """    u.utility,
    c.category,
    CASE WHEN u.utility = 'electricity' THEN c.subcategory
         ELSE u.subcategory END AS subcategory,
""",
)
OLD_VIEWS = [
    ("reporting_costs", previous.COSTS),
    ("reporting_usage", previous.USAGE),
    ("reporting_electricity_selection", previous.ELECTRICITY_SELECTION),
    ("reporting_electricity_usage", previous.ELECTRICITY_USAGE),
]
NEW_VIEWS = [
    ("reporting_costs", COSTS),
    ("reporting_usage", USAGE),
    *OLD_VIEWS[2:],
]


def classify_waste(apps, schema_editor):
    usage = apps.get_model("bills", "Usage")
    database = schema_editor.connection.alias
    rows = usage.objects.using(database).filter(utility="waste", subcategory="")
    for row in rows.iterator():
        source = (row.evidence or {}).get("text", "").strip()
        if match := re.match(r"^(Restavfall|Matavfall)\b", source, re.I):
            usage.objects.using(database).filter(pk=row.pk).update(subcategory=match[1].casefold())


class Migration(migrations.Migration):
    dependencies = [
        ("bills", "0008_reporting_views"),
    ]

    operations = [
        # SQLite remakes the usage table for the constraint. Drop dependent views
        # first, restoring their old definitions after a reverse migration.
        *[
            migrations.RunSQL(f"DROP VIEW {name}", reverse_sql=sql)
            for name, sql in reversed(OLD_VIEWS)
        ],
        migrations.AddField(
            model_name="usage",
            name="subcategory",
            field=models.CharField(
                blank=True,
                choices=[("restavfall", "Restavfall"), ("matavfall", "Matavfall")],
                default="",
                max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name="usage",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("subcategory", ""),
                    models.Q(
                        ("subcategory__in", ["restavfall", "matavfall"]), ("utility", "waste")
                    ),
                    _connector="OR",
                ),
                name="usage_subcategory_matches_utility",
            ),
        ),
        migrations.RunPython(classify_waste, reverse_code=migrations.RunPython.noop),
        *[migrations.RunSQL(sql, reverse_sql=f"DROP VIEW {name}") for name, sql in NEW_VIEWS],
    ]
