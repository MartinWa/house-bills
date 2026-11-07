"""Quarterly waste invoice with a printed VAT basis and a payment summary."""

import re

from .common import reconcile
from .layout_helpers import (
    capture,
    cost_period,
    empty_header,
    identity_fields,
    installation,
    tax_basis,
)
from .normalization import DATE, NUMBER, PERIOD, money, source_period, text
from .types import Section

FAMILY, VERSION, REVISION = "layout_03", "v1", "1"
TAX = rf"^Moms\s+25\s*%\s+beräknat på\s+({NUMBER})\s+({NUMBER})$"
ITEM = re.compile(r"^(Grundavgift permanentbostad|Tömn(?:ing)?\s+(?:Restavfall|Matavfall))\b", re.I)


def matches(pages):
    lines = [line for page in pages for line in page]
    return (
        any(re.search(r"\bRenhållning\b", text(line), re.I) for line in lines)
        and any(re.match(r"^Anläggning\s+\d+\b", text(line), re.I) for line in lines)
        and any(ITEM.match(text(line)) for line in lines)
        and any(re.search(r"Summa oss tillhanda senast", text(line), re.I) for line in lines)
    )


def read_header(pages):
    candidate = empty_header(pages, FAMILY, VERSION, REVISION)
    lines = [line for page in pages for line in page]
    identity_fields(
        candidate, pages, r"^Fakturanr\s*:?\s*(\d+)\b", due=rf"Förfallodatum\s*:?\s*({DATE})"
    )
    tax_basis(candidate, lines, TAX)
    capture(
        candidate, "rounding_minor", lines, rf"^Öresavrundning\s*:?\s*({NUMBER})(?:\s*kr)?$", money
    )
    capture(
        candidate,
        "amount_due_minor",
        lines,
        rf"^(?:Betalningsavsändare\s+)?senast\s+{DATE}\s+({NUMBER})(?:\s*kr)?$",
        money,
    )
    candidate.invoice_total_minor = candidate.amount_due_minor
    candidate.header_evidence["invoice_total"] = candidate.header_evidence.get("amount_due")
    if candidate.header_evidence["invoice_total"] is None:
        del candidate.header_evidence["invoice_total"]
    return candidate


def parse(pages, *, candidate=None):
    candidate = candidate or read_header(pages)
    lines = [line for page in pages for line in page]
    section = Section("section_01", "waste", [])
    candidate.sections.append(section)
    installation(candidate, section, lines, r"^Anläggning\s+(\d+)\b")
    section.net_minor, section.vat_minor = candidate.net_minor, candidate.vat_minor
    section.tax_evidence = candidate.header_evidence.get("vat")
    section.total_evidence = candidate.header_evidence.get("amount_due")
    if None not in (candidate.invoice_total_minor, candidate.rounding_minor):
        section.gross_minor = candidate.invoice_total_minor - candidate.rounding_minor
    periods = {}
    for line in lines:
        if ITEM.match(text(line)) and PERIOD.search(text(line)):
            try:
                periods[source_period(text(line), end_inclusive=True)] = line
            except ValueError:
                candidate.issues.append("Invalid billing period in section_01")
    cost_period(section, periods)
    # Collections, container litres and annual price quantities are never weight consumption.
    return reconcile(candidate)
