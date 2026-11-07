"""Framework-independent candidate contract. Monetary values are signed integer öre."""

from dataclasses import asdict, dataclass, field
from datetime import date


@dataclass(frozen=True)
class Evidence:
    page: int
    text: str
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True)
class Identifier:
    kind: str
    value: str
    evidence: Evidence


@dataclass(frozen=True)
class Charge:
    description: str
    start: date
    end: date
    net_minor: int
    evidence: Evidence
    vat_minor: int | None = None
    gross_minor: int | None = None
    vat_basis: str = "unallocated"


@dataclass(frozen=True)
class Usage:
    utility: str
    unit: str
    quantity_milli: int
    start: date
    end: date
    basis: str
    evidence: Evidence


@dataclass
class Section:
    section_id: str
    service: str
    identifiers: list[Identifier]
    charges: list[Charge] = field(default_factory=list)
    usages: list[Usage] = field(default_factory=list)
    net_minor: int | None = None
    vat_minor: int | None = None
    gross_minor: int | None = None
    tax_evidence: Evidence | None = None
    total_evidence: Evidence | None = None


@dataclass
class Candidate:
    layout_family: str
    layout_version: str
    parser_revision: str
    reference: str
    invoice_date: date
    due_date: date
    currency: str
    net_minor: int
    vat_minor: int
    rounding_minor: int
    invoice_total_minor: int
    amount_due_minor: int
    header_evidence: dict[str, Evidence]
    identifiers: list[Identifier] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    requires_review: bool = True

    def to_dict(self) -> dict:
        """Convert dates to ISO strings while keeping source evidence private."""

        def encode(value):
            if isinstance(value, date):
                return value.isoformat()
            if isinstance(value, dict):
                return {key: encode(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [encode(item) for item in value]
            return value

        return encode(asdict(self))
