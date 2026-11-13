"""Split explicitly priced waste fractions while retaining printed contract totals."""

import re
from dataclasses import replace
from fractions import Fraction

from bills.categories import waste_cost_subcategory

from .common import last_money
from .normalization import NUMBER, money, text

CONTRACT = re.compile(
    r"^(Elnätavgifter|Vatten & Avlopp|Elhandelskostnad|Vatten|Hushållsavfall|Avfall|Elnät|Elhandel)"
    r"\s+(?:prodnr|avtal)\s+(\d+)\b",
    re.I,
)
FRACTION = re.compile(r"^(?:Tömn(?:ing)?\s+)?(Matavfall|Restavfall|Trädgårdsavfall)\b", re.I)
BILLED_AMOUNT = re.compile(rf"(?<![\w,.])({NUMBER})\s+kr\s*$", re.I)
TAX = re.compile(rf"^Moms\s+25\s*%\s+på\s+({NUMBER})\s+kr\b", re.I)
SHARED_FEE = re.compile(r"^(?:Grundavgift avfall|Hämtningsavgift|Tömningsavgift)\b", re.I)
KEYS = {"matavfall": "matavfall", "restavfall": "restavfall", "trädgårdsavfall": "tradgardsavfall"}


def allocated_vat(net_amounts, printed_net, printed_vat):
    """Apportion the printed VAT exactly, including credits and rounding ties."""
    if sum(net_amounts) != printed_net:
        raise ValueError("Waste net allocations do not match the printed contract.")
    if not printed_net:
        if printed_vat or any(net_amounts):
            raise ValueError("A zero-net contract needs an explicit VAT split.")
        return [0] * len(net_amounts)
    shares = [Fraction(printed_vat * amount, printed_net) for amount in net_amounts]
    rounded = [int(share) for share in shares]
    difference = printed_vat - sum(rounded)
    direction = 1 if difference >= 0 else -1
    order = sorted(
        range(len(shares)),
        key=lambda index: direction * (shares[index] - rounded[index]),
        reverse=True,
    )
    for index in order[: abs(difference)]:
        rounded[index] += direction
    return rounded


def _source_lines(section, lines):
    identifiers = {
        identifier.value
        for identifier in section.identifiers
        if identifier.kind in ("contract", "product")
    }
    starts = [
        index
        for index, line in enumerate(lines)
        if (match := CONTRACT.match(text(line)))
        and match[1].casefold() in ("hushållsavfall", "avfall")
        and match[2] in identifiers
    ]
    if len(starts) != 1:
        raise ValueError("The original waste contract cannot be identified unambiguously.")
    start = starts[0]
    end = next(
        (index for index in range(start + 1, len(lines)) if CONTRACT.match(text(lines[index]))),
        len(lines),
    )
    return lines[start:end]


def _split_section(section, lines, tracked):
    source = _source_lines(section, lines)
    taxes = [
        (index, match) for index, line in enumerate(source) if (match := TAX.match(text(line)))
    ]
    if len(taxes) != 1:
        raise ValueError("The waste contract needs one printed net and VAT total.")
    tax_index, tax = taxes[0]
    tax_evidence = source[tax_index]
    printed_net, printed_vat = money(tax[1]), last_money(tax_evidence)
    if (section.net_minor, section.vat_minor, section.gross_minor) != (
        printed_net,
        printed_vat,
        printed_net + printed_vat,
    ):
        raise ValueError(
            "Reviewed waste totals differ from the printed contract; retained unchanged."
        )
    classified = waste_cost_subcategory(text(line) for line in source[:tax_index])
    if classified in tracked:
        # A named fraction contract already supplies its entire net and VAT,
        # including a printed zero during a pause in garden collection.
        return [replace(section, subcategory=classified, tax_evidence=tax_evidence)]
    amounts, evidence = {}, {}
    shared_fee = None
    for line in source[:tax_index]:
        value = text(line)
        if SHARED_FEE.match(value):
            shared_fee = shared_fee or line
        match = FRACTION.match(value)
        if not match:
            continue
        key = KEYS[match[1].casefold()]
        if key not in tracked:
            continue
        # Container headings and measurements never establish a monetary split.
        billed = BILLED_AMOUNT.search(value)
        if not billed:
            raise ValueError(f"The printed {match[1]} charge is missing or invalid.")
        amount = money(billed[1])
        amounts[key] = amounts.get(key, 0) + amount
        evidence.setdefault(key, line)
    if not amounts:
        return [section]
    remaining = printed_net - sum(amounts.values())
    remaining_usages = [usage for usage in section.usages if usage.subcategory not in amounts]
    parts = []
    if remaining or shared_fee is not None or remaining_usages:
        parts.append(
            replace(
                section,
                subcategory="waste_collection",
                net_minor=remaining,
                usages=remaining_usages,
                total_evidence=shared_fee or tax_evidence,
            )
        )
    for key, amount in amounts.items():
        parts.append(
            replace(
                section,
                section_id=f"{section.section_id}_{key}"
                if parts or len(amounts) > 1
                else section.section_id,
                subcategory=key,
                net_minor=amount,
                usages=[usage for usage in section.usages if usage.subcategory == key],
                total_evidence=evidence[key],
            )
        )
    shares = allocated_vat([part.net_minor for part in parts], printed_net, printed_vat)
    for part, vat in zip(parts, shares, strict=True):
        part.vat_minor = vat
        part.gross_minor = part.net_minor + vat
        part.tax_evidence = tax_evidence
    return parts


def split_waste_costs(candidate, pages, tracked_sections):
    """Opt in by section/fraction; return source IDs for routing and any warnings.

    Fraction net amounts come only from explicit billed rows. Remaining fees stay
    in general collection, and allocated VAT sums to the untouched printed VAT.
    Missing/ambiguous evidence retains the approved or extracted combined cost.
    """
    lines = [line for page in pages for line in page]
    by_id = {section.section_id: section for section in candidate.sections}
    split_parents = set()
    for section in candidate.sections:
        suffix = f"_{section.subcategory}"
        if section.subcategory not in KEYS.values() or not section.section_id.endswith(suffix):
            continue
        parent_id = section.section_id[: -len(suffix)]
        parent = by_id.get(parent_id)
        if parent and parent.identifiers == section.identifiers:
            split_parents.add(parent_id)
    sections, sources, warnings = [], {}, []
    for section in candidate.sections:
        tracked = tracked_sections.get(section.section_id, ())
        if (
            section.service != "waste"
            or section.section_id in split_parents
            or not tracked
            or section.subcategory not in ("", "waste_collection")
        ):
            sections.append(section)
            continue
        try:
            parts = _split_section(section, lines, tracked)
        except ValueError as exc:
            warnings.append(f"{section.section_id}: {exc}")
            parts = [section]
        sections.extend(parts)
        if parts != [section]:
            sources.update((part.section_id, section.section_id) for part in parts)
    candidate.sections = sections
    return sources, warnings
