"""Read older immutable proposals without promoting their itemized fee records."""

from copy import deepcopy
from datetime import date

from bills.categories import waste_cost_subcategory, waste_subcategory


def legacy_billing_period(section):
    rows = section.get("charges") or section.get("usages", [])
    periods = []
    for row in rows:
        try:
            start, end = date.fromisoformat(row["start"]), date.fromisoformat(row["end"])
            if start < end:
                periods.append((start, end, row.get("evidence") or {}))
        except KeyError, TypeError, ValueError:
            continue
    if not periods:
        return None, None, None
    start, end = min(row[0] for row in periods), max(row[1] for row in periods)
    evidence = next((row[2] for row in periods if row[:2] == (start, end)), None)
    if not evidence:
        evidence = {
            "page": periods[0][2].get("page", 1),
            "text": "Billing period covering the previously recorded service periods.",
            "bbox": [0, 0, 0, 0],
        }
    return start.isoformat(), end.isoformat(), evidence


def cost_totals_candidate(values):
    """Adapt in memory; stored extraction and approval snapshots stay immutable."""
    result = deepcopy(values)
    for section in result.get("sections", []):
        legacy_classification = not section.get("category")
        if "subcategory" not in section:
            texts = [(section.get("period_evidence") or {}).get("text", "")]
            texts.extend(
                (row.get("evidence") or {}).get("text", "")
                for row in [*section.get("charges", []), *section.get("usages", [])]
            )
            section["subcategory"] = (
                waste_cost_subcategory(texts) if section.get("service") == "waste" else ""
            )
        if "start" not in section and "end" not in section:
            section["start"], section["end"], section["period_evidence"] = legacy_billing_period(
                section
            )
        service = section.get("service", section.get("category", ""))
        section["category"] = section.get("category") or (
            "electricity" if service.startswith("electricity_") else service
        )
        if (
            legacy_classification
            and service.startswith("electricity_")
            and not section.get("subcategory")
        ):
            section["subcategory"] = service
        section["service"] = (
            section["subcategory"]
            if section["category"] == "electricity"
            and section.get("subcategory") in ("electricity_network", "electricity_supply")
            else section["category"]
        )
        section.pop("charges", None)
        for usage in section.get("usages", []):
            if "subcategory" not in usage:
                usage["subcategory"] = (
                    waste_subcategory((usage.get("evidence") or {}).get("text", ""))
                    if usage.get("utility") == "waste"
                    else ""
                )
            if (
                legacy_classification
                and usage.get("utility") == "electricity"
                and not usage.get("subcategory")
            ):
                usage["subcategory"] = section.get("subcategory", "")
    return result
