"""Summarize billed power without adding kW across properties or periods."""

from collections import Counter, defaultdict


def summarize_billed_power(rows):
    """Give each unreversed billed value equal weight and retain its source period."""
    properties = defaultdict(list)
    for row in rows:
        properties[row["property_id"]].append(row)
    summaries = []
    for property_id, property_rows in properties.items():
        property_rows = sorted(
            property_rows,
            key=lambda row: (
                row["period_start"],
                row["invoice_date"],
                row["invoice_id"],
                row["usage_id"],
            ),
            reverse=True,
        )
        positives, credits = defaultdict(list), Counter()
        for row in reversed(property_rows):
            key = (
                row["vendor_id"],
                row["period_start"],
                row["period_end"],
                abs(row["quantity_milli"]),
            )
            if row["quantity_milli"] < 0:
                credits[key] += 1
            elif row["quantity_milli"] > 0:
                positives[key].append(row["usage_id"])
        # One exact credit cancels one older matching positive. Unmatched credits
        # stay in the details; they cannot establish a physical power minimum.
        reversed_ids = {
            usage_id for key, count in credits.items() for usage_id in positives[key][:count]
        }
        values = [
            row
            for row in property_rows
            if row["quantity_milli"] >= 0 and row["usage_id"] not in reversed_ids
        ]
        count = len(values)
        summary = {
            "property_id": property_id,
            "property_name": property_rows[0]["property_name"],
            "value_count": count,
            "unmatched_credit_count": sum(credits.values()) - len(reversed_ids),
            "average_milli": None,
            "highest": None,
            "lowest": None,
            "highest_other_count": 0,
            "lowest_other_count": 0,
        }
        if values:
            total = sum(row["quantity_milli"] for row in values)
            # Round the mean to the stored 0.001 kW precision, halves upwards.
            summary["average_milli"] = (2 * total + count) // (2 * count)
            for extreme, select in (("highest", max), ("lowest", min)):
                quantity = select(row["quantity_milli"] for row in values)
                matches = [row for row in values if row["quantity_milli"] == quantity]
                summary[extreme] = matches[0]
                summary[f"{extreme}_other_count"] = len(matches) - 1
        summaries.append(summary)
    return summaries
