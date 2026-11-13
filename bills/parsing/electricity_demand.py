"""Separate printed demand charges and billed kW from network costs and kWh."""

import re
from dataclasses import replace

from .common import last_money
from .normalization import NUMBER, PERIOD, money, quantity, source_period, text
from .types import Usage
from .waste_costs import BILLED_AMOUNT, CONTRACT, TAX, allocated_vat

SUBCATEGORY = "electricity_demand"
DEMAND = re.compile(r"^Effektavgift\b", re.I)
POWER = re.compile(rf"(?<![\w,.])({NUMBER})\s+(kW|kWh|m[3³]|kg)\b", re.I)


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
        and match[1].casefold() in ("elnät", "elnätavgifter")
        and match[2] in identifiers
    ]
    if not starts:
        # Reviewed allocations may include separately entered services that have
        # no matching source contract. Preserve them while splitting known rows.
        return []
    if len(starts) != 1:
        raise ValueError("The original network contract cannot be identified unambiguously.")
    start = starts[0]
    end = next(
        (index for index in range(start + 1, len(lines)) if CONTRACT.match(text(lines[index]))),
        len(lines),
    )
    return lines[start:end]


def _split_section(section, source):
    fees = [line for line in source if DEMAND.match(text(line))]
    if not fees:
        return [section], []
    taxes = [
        (index, match) for index, line in enumerate(source) if (match := TAX.match(text(line)))
    ]
    if len(taxes) != 1:
        raise ValueError("The network contract needs one printed net and VAT total.")
    tax_index, tax = taxes[0]
    tax_evidence = source[tax_index]
    printed_net, printed_vat = money(tax[1]), last_money(tax_evidence)
    if (section.net_minor, section.vat_minor, section.gross_minor) != (
        printed_net,
        printed_vat,
        printed_net + printed_vat,
    ):
        raise ValueError("Reviewed network totals differ from the printed contract; unchanged.")
    if section.start is None or section.end is None:
        raise ValueError("The network contract needs a valid billed period.")
    fees = [line for line in source[:tax_index] if DEMAND.match(text(line))]
    parts, warnings = [], []
    for index, line in enumerate(fees, 1):
        value = text(line)
        amount = BILLED_AMOUNT.search(value)
        if amount is None:
            raise ValueError("The printed Effektavgift charge is missing or invalid.")
        period = source_period(value) if PERIOD.search(value) else (section.start, section.end)
        usages = []
        # Unit prices may separate "/" from "kW"; retain the original source evidence.
        power_text = re.sub(r"/\s+", "/", PERIOD.sub("", value))
        power = POWER.search(power_text)
        try:
            if power is not None:
                if power[2].casefold() != "kw":
                    raise ValueError("Invalid power unit")
                usages.append(
                    Usage(
                        "electricity",
                        "kW",
                        quantity(power[1]),
                        *period,
                        "billed",
                        line,
                        subcategory=SUBCATEGORY,
                    )
                )
            elif re.search(r"(?<!/)\bkW\b", power_text, re.I):
                raise ValueError("Invalid billed power")
        except ValueError:
            warnings.append(f"Invalid billed power in {section.section_id}")
        parts.append(
            replace(
                section,
                section_id=f"{section.section_id}_{SUBCATEGORY}"
                + (f"_{index:02}" if len(fees) > 1 else ""),
                service="electricity",
                subcategory=SUBCATEGORY,
                net_minor=money(amount[1]),
                start=period[0],
                end=period[1],
                period_evidence=line,
                total_evidence=line,
                usages=usages,
            )
        )
    if not parts:
        raise ValueError("The demand charge is outside the printed network cost block.")
    remaining = section.net_minor - sum(part.net_minor for part in parts)
    if (printed_net > 0 and all(part.net_minor >= 0 for part in parts) and remaining < 0) or (
        printed_net < 0 and all(part.net_minor <= 0 for part in parts) and remaining > 0
    ):
        raise ValueError("The printed demand charges exceed the network contract total.")
    parts.insert(0, replace(section, net_minor=remaining))
    vats = allocated_vat([part.net_minor for part in parts], printed_net, printed_vat)
    for part, vat in zip(parts, vats, strict=True):
        part.vat_minor = vat
        part.gross_minor = part.net_minor + vat
        part.tax_evidence = tax_evidence
    return parts, warnings


def split_electricity_demand(candidate, pages, section_ids=None):
    """Keep totals and kWh intact; return source section IDs for new allocations.

    Each printed fee keeps its own period and optional billed power. Previously
    split allocations and reviewed amounts that differ from the source stay intact.
    """
    lines = [line for page in pages for line in page]
    sections, sources, warnings = [], {}, []
    by_id = {section.section_id: section for section in candidate.sections}
    for section in candidate.sections:
        if section.service != "electricity_network" or (
            section_ids is not None and section.section_id not in section_ids
        ):
            sections.append(section)
            continue
        prefix = f"{section.section_id}_{SUBCATEGORY}"
        existing = [
            part for key, part in by_id.items() if key == prefix or key.startswith(prefix + "_")
        ]
        if existing:
            sections.append(section)
            if not all(
                part.subcategory == SUBCATEGORY and part.identifiers == section.identifiers
                for part in existing
            ):
                warnings.append(f"{section.section_id}: Demand allocation identifiers conflict.")
            continue
        try:
            parts, issues = _split_section(section, _source_lines(section, lines))
            warnings.extend(issues)
        except ValueError as exc:
            parts = [section]
            warnings.append(f"{section.section_id}: {exc}")
        sections.extend(parts)
        if parts != [section]:
            sources.update((part.section_id, section.section_id) for part in parts)
    candidate.sections = sections
    return sources, warnings
