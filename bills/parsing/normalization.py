"""Exact normalization of numbers and explicit source date conventions."""

import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MONTHS = {
    name: i
    for i, name in enumerate(
        ("jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec"), 1
    )
}
NUMBER = r"[−+-]?(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[,.]\d+)?"
DATE = r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}\s+[a-zåäö]+\s+\d{4})"
PERIOD = re.compile(rf"(?P<start>{DATE})\s+-\s+(?P<end>{DATE})", re.I)


def normalized_text(value: str) -> str:
    return " ".join(value.replace("–", "-").replace("—", "-").split())


def text(line) -> str:
    """Match normalized whitespace without replacing the original evidence."""
    return normalized_text(line.text)


def anchor(line, label: str) -> bool:
    return text(line).casefold().rstrip(".:") == label.casefold().rstrip(".:")


def starts(line, *labels: str) -> bool:
    return text(line).casefold().startswith(tuple(label.casefold() for label in labels))


def unit(value: str) -> str:
    try:
        return {"kwh": "kWh", "kw": "kW", "m3": "m3", "m³": "m3", "kg": "kg"}[value.casefold()]
    except KeyError:
        raise ValueError("Unsupported usage unit") from None


def decimal_number(value: str) -> Decimal:
    value = value.strip().replace("−", "-")
    if not re.fullmatch(NUMBER, value):
        raise ValueError("Invalid Swedish number")
    try:
        return Decimal(re.sub(r"[ \u00a0\u202f]", "", value).replace(",", "."))
    except InvalidOperation as exc:
        raise ValueError("Invalid number") from exc


def scaled_integer(value: str, scale: int) -> int:
    # Decimal multiplication uses the active precision context. Large valid
    # integer amounts must not round away an overprecise fractional remainder.
    numerator, denominator = decimal_number(value).as_integer_ratio()
    scaled, remainder = divmod(numerator * scale, denominator)
    if remainder:
        raise ValueError(f"Value exceeds precision for scale {scale}")
    return scaled


def money(value: str) -> int:
    return scaled_integer(value, 100)


def quantity(value: str) -> int:
    return scaled_integer(value, 1000)


def source_date(value: str) -> date:
    value = value.strip().lower()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value)
    day, month, year = value.split()
    if month[:3] not in MONTHS:
        raise ValueError("Unknown Swedish month")
    return date(int(year), MONTHS[month[:3]], int(day))


def source_period(value: str, *, end_inclusive: bool = False) -> tuple[date, date]:
    match = PERIOD.search(normalized_text(value))
    if not match:
        raise ValueError("Missing explicit service period")
    start, end = source_date(match["start"]), source_date(match["end"])
    if end_inclusive:
        end += timedelta(days=1)
    if start >= end:
        raise ValueError("Service period must have positive duration")
    return start, end


def rounded_vat(net_minor: int, rate: Decimal) -> int:
    return int((Decimal(net_minor) * rate).quantize(Decimal(1), rounding=ROUND_HALF_UP))
