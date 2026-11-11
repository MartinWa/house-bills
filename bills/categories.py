"""Category labels shared by invoice review and approved reports."""

import re

WASTE_SUBCATEGORIES = [("restavfall", "Restavfall"), ("matavfall", "Matavfall")]
WASTE_COST_SUBCATEGORIES = [
    ("waste_collection", "Waste collection"),
    ("tank_emptying", "Tank emptying"),
]
SUBCATEGORY_LABELS = {
    **dict(WASTE_SUBCATEGORIES),
    **dict(WASTE_COST_SUBCATEGORIES),
    "electricity_network": "Elnät",
    "electricity_supply": "Elhandel",
}
CATEGORY_LABELS = {
    "electricity_network": "Electricity",
    "electricity_supply": "Electricity",
    "water": "Water",
    "waste": "Waste",
    "other": "Other",
    "electricity": "Electricity",
}


def waste_subcategory(source_text):
    """Infer only from an explicit waste type at the start of a billed usage row."""
    match = re.match(r"^(Restavfall|Matavfall)\b", source_text.strip(), re.I)
    return match[1].casefold() if match else ""


def waste_cost_subcategory(source_texts):
    """Classify explicit billed items; mixed or unspecified costs remain unclassified."""
    categories = set()
    for source in source_texts:
        if re.match(r"^(?:Slamavskiljare|Slamtömning|Tank emptying)\b", source.strip(), re.I):
            categories.add("tank_emptying")
        elif re.match(
            r"^(?:Grundavgift permanentbostad|Tömn(?:ing)?\s+(?:Restavfall|Matavfall)"
            r"|Restavfall|Matavfall)\b",
            source.strip(),
            re.I,
        ):
            categories.add("waste_collection")
    return next(iter(categories)) if len(categories) == 1 else ""


def subcategory_label(service, subcategory=""):
    key = getattr(subcategory, "pk", subcategory) or ""
    if not key and service in ("electricity_network", "electricity_supply"):
        key = service
    if key:
        from .models import Subcategory

        row = Subcategory.objects.filter(pk=key).first()
        return row.name if row else SUBCATEGORY_LABELS.get(key, key)
    return "Unspecified" if service == "waste" else ""
