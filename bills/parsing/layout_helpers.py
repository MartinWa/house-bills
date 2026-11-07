"""Small shared operations for labelled single-service current invoice layouts."""

import re

from .normalization import DATE, NUMBER, money, quantity, source_date, text
from .types import Candidate, Identifier

QUANTITY = re.compile(rf"(?<![\w,.])({NUMBER})\s*(kWh|m[3³]|kg)\b", re.I)
EVIDENCE_KEYS = {
    "net_minor": "net",
    "vat_minor": "vat",
    "rounding_minor": "rounding",
    "invoice_total_minor": "invoice_total",
    "amount_due_minor": "amount_due",
}


def empty_header(pages, family, version, revision):
    candidate = Candidate(
        family, version, revision, "", None, None, "SEK", None, None, None, None, None, {}
    )
    if any(
        re.search(r"\b(?:EUR|USD|NOK|DKK)\b", text(line), re.I) for page in pages for line in page
    ):
        candidate.issues.append("Unsupported currency")
    return candidate


def capture(candidate, field, lines, pattern, convert=str, *, group=1, decode=text, optional=False):
    matched = []
    try:
        for line in lines:
            if match := re.search(pattern, decode(line), re.I):
                matched.append((convert(match[group]), line))
        if not matched:
            if optional:
                return
            raise ValueError("Missing field")
        value, evidence = matched[0]
        setattr(candidate, field, value)
        candidate.header_evidence[EVIDENCE_KEYS.get(field, field)] = evidence
        if len({value for value, _ in matched}) > 1:
            candidate.issues.append(f"Ambiguous {field}")
    except ValueError, IndexError:
        candidate.issues.append(f"Missing or invalid {field}")


def identity_fields(candidate, pages, reference, *, decode=text, due=None):
    lines = [line for page in pages for line in page]
    capture(
        candidate,
        "reference",
        lines,
        reference,
        lambda value: value.replace(" ", ""),
        decode=decode,
    )
    capture(
        candidate,
        "invoice_date",
        pages[0] if pages else [],
        rf"Fakturadatum\s*:?\s*({DATE})",
        source_date,
        decode=decode,
    )
    if due:
        capture(candidate, "due_date", lines, due, source_date, decode=decode)
    for line in pages[0] if pages else []:
        if match := re.search(r"Kund(?:nummer|nr)\s*:?\s*(\d+)\b", decode(line), re.I):
            candidate.identifiers.append(Identifier("customer_account", match[1], line))
            break


def installation(
    candidate,
    section,
    lines,
    pattern=r"Anläggnings[- ]?ID\s*:?\s*((?:\d\s*){18})\b",
    *,
    decode=text,
):
    values = {}
    for line in lines:
        if match := re.search(pattern, decode(line), re.I):
            values.setdefault(match[1].replace(" ", ""), line)
    if len(values) == 1:
        value, line = next(iter(values.items()))
        section.identifiers.append(Identifier("installation", value, line))
    else:
        candidate.issues.append(f"Missing or ambiguous installation in {section.section_id}")


def cost_period(section, periods):
    if periods:
        section.start = min(period[0] for period in periods)
        section.end = max(period[1] for period in periods)
        section.period_evidence = next(iter(periods.values()))


def price_quantities(candidate, section, lines, item, *, decode=text):
    values = {}
    for line in lines:
        if not item.match(decode(line)):
            continue
        match = QUANTITY.search(decode(line))
        if not match:
            continue
        try:
            if match[2].casefold() != "kwh":
                raise ValueError("Invalid electricity unit")
            values.setdefault(quantity(match[1]), line)
        except ValueError:
            candidate.issues.append(
                f"Invalid billed electricity consumption in {section.section_id}"
            )
    if len(values) > 1:
        candidate.issues.append(f"Inconsistent billed kWh in {section.section_id}")
    return values


def tax_basis(candidate, lines, pattern):
    capture(candidate, "net_minor", lines, pattern, money)
    capture(candidate, "vat_minor", lines, pattern, money, group=2)


def due_amount(candidate, lines, *, decode=text):
    """Accept a labelled value or a nearby standalone value in the printed header."""
    values = []
    for index, line in enumerate(lines):
        value = decode(line)
        match = re.search(rf"Belopp att betala\s*:?\s*({NUMBER})\s*kr\b", value, re.I)
        match = match or re.search(rf"({NUMBER})\s*kr\s*Belopp att betala\s*:?$", value, re.I)
        if match:
            values.append((match[1], line))
        elif re.fullmatch(r"Belopp att betala\s*:?", value, re.I):
            for other in lines[max(0, index - 1) : index + 2]:
                if abs(other.bbox[1] - line.bbox[1]) <= 20:
                    match = re.fullmatch(rf"({NUMBER})\s*kr", decode(other), re.I)
                    if match:
                        values.append((match[1], other))
    try:
        if not values:
            raise ValueError("Missing amount due")
        candidate.amount_due_minor = money(values[0][0])
        candidate.header_evidence["amount_due"] = values[0][1]
        if len({money(value) for value, _ in values}) > 1:
            candidate.issues.append("Ambiguous amount_due_minor")
    except ValueError:
        candidate.issues.append("Missing or invalid amount_due_minor")
