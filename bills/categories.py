"""Category labels shared by invoice review and approved reports."""

import re

WASTE_SUBCATEGORIES = [("restavfall", "Restavfall"), ("matavfall", "Matavfall")]
SUBCATEGORY_LABELS = {
    **dict(WASTE_SUBCATEGORIES),
    "electricity_network": "Elnät",
    "electricity_supply": "Elhandel",
}
CATEGORY_LABELS = {
    "electricity_network": "Electricity",
    "electricity_supply": "Electricity",
    "water": "Water",
    "waste": "Waste",
    "other": "Other charges",
}


def waste_subcategory(source_text):
    """Infer only from an explicit waste type at the start of a billed usage row."""
    match = re.match(r"^(Restavfall|Matavfall)\b", source_text.strip(), re.I)
    return match[1].casefold() if match else ""


def subcategory_label(service, subcategory=""):
    if service in ("electricity_network", "electricity_supply"):
        return SUBCATEGORY_LABELS[service]
    if service == "waste":
        return SUBCATEGORY_LABELS.get(subcategory, "Unspecified")
    return ""
