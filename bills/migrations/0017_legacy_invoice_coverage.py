"""Classify legacy pending waste receipts without changing immutable proposals."""

from importlib import import_module

from django.db import migrations

previous = import_module(".0016_service_invoice_exceptions", __package__)

# SQLite's default trim only removes spaces. Include the whitespace accepted by
# the compatibility adapter, including Swedish invoice non-breaking spaces.
WHITESPACE = (
    "char(9,10,11,12,13,28,29,30,31,32,133,160,5760,8192,8193,8194,8195,"
    "8196,8197,8198,8199,8200,8201,8202,8232,8233,8239,8287,12288)"
)
# Require a complete label followed by whitespace or punctuation; suffixes such
# as Restavfall_extra or Slamavskiljare2 do not identify billed waste costs.
WORD_END = (
    f"{WHITESPACE} || "
    "char(33,34,35,36,37,38,39,40,41,42,43,44,45,46,47,58,59,60,61,62,63,"
    "64,91,92,93,94,96,123,124,125,126,8211,8212,8216,8217,8220,8221)"
)


def word_prefix(column, label):
    following = f"substr({column}, {len(label) + 1}, 1)"
    return (
        f"(substr({column}, 1, {len(label)}) = '{label}' "
        f"AND ({following} = '' OR instr({WORD_END}, {following}) > 0))"
    )


TANK_ITEMS = " OR ".join(
    word_prefix("source", label) for label in ("slamavskiljare", "slamtömning", "tank emptying")
)
COLLECTION_ITEMS = " OR ".join(
    [
        *(
            word_prefix("source", label)
            for label in ("grundavgift permanentbostad", "restavfall", "matavfall")
        ),
        *(word_prefix("waste_type", label) for label in ("restavfall", "matavfall")),
    ]
)

LEGACY_CATEGORIES = f"""
legacy_sections AS (
    SELECT e.id AS extraction_id, b.key AS section_index, b.value AS section
    FROM bills_extractionattempt AS e
    JOIN bills_document AS d ON d.id = e.document_id AND d.extraction_revision = e.revision
    JOIN json_each(e.candidate, '$.sections') AS b
    WHERE json_extract(b.value, '$.service') = 'waste'
      AND json_type(b.value, '$.subcategory') IS NULL
      AND NOT EXISTS (
          SELECT 1 FROM bills_invoice AS approved
          WHERE approved.document_id = e.document_id AND approved.status = 'approved'
      )
), legacy_texts AS (
    SELECT extraction_id, section_index, json_extract(section, '$.period_evidence.text') AS text
    FROM legacy_sections
    UNION ALL
    SELECT s.extraction_id, s.section_index, json_extract(c.value, '$.evidence.text')
    FROM legacy_sections AS s JOIN json_each(s.section, '$.charges') AS c
    UNION ALL
    SELECT s.extraction_id, s.section_index, json_extract(u.value, '$.evidence.text')
    FROM legacy_sections AS s JOIN json_each(s.section, '$.usages') AS u
), legacy_normalized_texts AS (
    SELECT extraction_id, section_index,
           replace(lower(trim(coalesce(text, ''), {WHITESPACE})), 'Ö', 'ö') AS source
    FROM legacy_texts
), legacy_items AS (
    SELECT *,
           CASE WHEN substr(source, 1, 4) = 'tömn'
                     AND instr({WHITESPACE}, substr(source, 5, 1)) > 0
                THEN ltrim(substr(source, 5), {WHITESPACE})
                WHEN substr(source, 1, 7) = 'tömning'
                     AND instr({WHITESPACE}, substr(source, 8, 1)) > 0
                THEN ltrim(substr(source, 8), {WHITESPACE})
                ELSE '' END AS waste_type
    FROM legacy_normalized_texts
), legacy_classified_items AS (
    SELECT extraction_id, section_index,
           CASE WHEN {TANK_ITEMS} THEN 'tank_emptying'
                WHEN {COLLECTION_ITEMS} THEN 'waste_collection'
                ELSE '' END AS subcategory
    FROM legacy_items
), legacy_categories AS (
    SELECT extraction_id, section_index,
           CASE WHEN count(DISTINCT nullif(subcategory, '')) = 1
                THEN max(subcategory) ELSE '' END AS subcategory
    FROM legacy_classified_items
    GROUP BY extraction_id, section_index
)
"""

COVERAGE = previous.COVERAGE.replace(
    "), receipts AS (", f"), {LEGACY_CATEGORIES}, receipts AS ("
).replace(
    "coalesce(json_extract(b.value, '$.subcategory'), '')",
    """coalesce(json_extract(b.value, '$.subcategory'),
                    (SELECT l.subcategory FROM legacy_categories AS l
                     WHERE l.extraction_id = e.id AND l.section_index = b.key), '')""",
)


class Migration(migrations.Migration):
    dependencies = [
        ("bills", "0016_service_invoice_exceptions"),
    ]

    operations = [
        migrations.RunSQL("DROP VIEW reporting_invoice_coverage", reverse_sql=previous.COVERAGE),
        migrations.RunSQL(COVERAGE, reverse_sql="DROP VIEW reporting_invoice_coverage"),
    ]
