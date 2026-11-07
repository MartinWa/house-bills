"""Issuer identification uses labelled cover fields, never filenames or payee hints."""

import re
from dataclasses import dataclass

from .normalization import text
from .types import Identifier

ORGANIZATION = re.compile(
    r"(?:Org(?:anisations)?\.?\s*(?:nr|nummer)\.?|Organisationsnummer)\s*:?\s*"
    r"([0-9](?:[0-9 -]*[0-9])?)",
    re.I,
)
VAT = re.compile(
    r"(?:VAT(?:[-. ]*(?:nr|nummer|number|no\.))?|Momsreg(?:istrerings)?\.?\s*(?:nr|nummer)\.?)"
    r"\s*:?\s*(SE(?:[0-9 -]*[0-9]))\b",
    re.I,
)
VAT_TOKEN = re.compile(r"(?<![\w])SE(?:[ -]*\d){12}\b", re.I)
ORGANIZATION_TOKEN = re.compile(r"(?<!\d)\d{6}-\d{4}(?!\d)")


@dataclass
class VendorRecognition:
    vendor: object | None
    identifiers: list[Identifier]
    issue: str = ""


def recognize_vendor(pages):
    from bills.models import Vendor, VendorIssuerIdentifier
    from bills.validators import normalize_identifier

    # Cover identity is separate from subcontractors and service companies named
    # on billing pages. Unlabelled VAT numbers in totals are not issuer evidence.
    identifiers = []
    for line in pages[0] if pages else []:
        for kind, pattern in (("organization", ORGANIZATION), ("vat", VAT)):
            for match in pattern.finditer(text(line)):
                identifiers.append(Identifier(kind, normalize_identifier(kind, match[1]), line))
    # The supply layout prints its legal identity in a bottom-page tax footer.
    # A tax-registration anchor bounds this block; arbitrary VAT-like text in
    # specifications, recipient addresses and payment instructions is excluded.
    for page in pages:
        footer_anchors = [
            line
            for line in page
            if line.bbox[1] >= 700 and re.search(r"\bF[- ]skatt\b", text(line), re.I)
        ]
        for line in page:
            if not any(abs(line.bbox[1] - marker.bbox[1]) <= 25 for marker in footer_anchors):
                continue
            for kind, pattern in (("organization", ORGANIZATION_TOKEN), ("vat", VAT_TOKEN)):
                for match in pattern.finditer(text(line)):
                    identifiers.append(Identifier(kind, normalize_identifier(kind, match[0]), line))
    vendor_ids = set()
    for identifier in identifiers:
        vendor_ids.update(
            VendorIssuerIdentifier.objects.filter(
                kind=identifier.kind, value=identifier.value
            ).values_list("vendor_id", flat=True)
        )
    if len(vendor_ids) > 1:
        return VendorRecognition(None, identifiers, "Vendor identification is ambiguous.")
    if not vendor_ids:
        return VendorRecognition(None, identifiers, "Vendor identification is unknown.")
    return VendorRecognition(Vendor.objects.get(pk=next(iter(vendor_ids))), identifiers)
