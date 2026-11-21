"""Curated, documented parser API; names outside this module are internal.

API version 1 keeps the exported names, signatures, value conventions and
mutation/return semantics stable. Breaking changes require a new API version.
Installed plugins declare ``API_VERSION = 1`` in their own module.
"""

from .common import header, last_money, reconcile
from .configuration import configure
from .layout_helpers import (
    capture,
    cost_period,
    due_amount,
    empty_header,
    identity_fields,
    installation,
    price_quantities,
    tax_basis,
)
from .normalization import (
    DATE,
    NUMBER,
    PERIOD,
    anchor,
    money,
    normalized_text,
    quantity,
    source_date,
    source_period,
    starts,
    text,
    unit,
)
from .types import Candidate, Evidence, Identifier, Section, Usage

API_VERSION = 1

__all__ = [
    "API_VERSION",
    "DATE",
    "NUMBER",
    "PERIOD",
    "Candidate",
    "Evidence",
    "Identifier",
    "Section",
    "Usage",
    "anchor",
    "capture",
    "configure",
    "cost_period",
    "due_amount",
    "empty_header",
    "header",
    "identity_fields",
    "installation",
    "last_money",
    "money",
    "normalized_text",
    "price_quantities",
    "quantity",
    "reconcile",
    "source_date",
    "source_period",
    "starts",
    "tax_basis",
    "text",
    "unit",
]
