"""Curated, documented parser API; names outside this module are internal.

API version 1 keeps the exported names, signatures, value conventions and
mutation/return semantics stable. Breaking changes require a new API version.
Installed plugins declare ``API_VERSION = 1`` in their own module. This surface is
sized to the shipped example layouts: a plugin can express them with no other import.
"""

from bills.categories import waste_cost_subcategory, waste_subcategory

from .common import AMOUNT, find, header, last_money, reconcile
from .configuration import configure
from .electricity_demand import split_electricity_demand
from .layout_helpers import (
    QUANTITY,
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
from .waste_costs import split_waste_costs

API_VERSION = 1

__all__ = [
    "AMOUNT",
    "API_VERSION",
    "DATE",
    "NUMBER",
    "PERIOD",
    "QUANTITY",
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
    "find",
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
    "split_electricity_demand",
    "split_waste_costs",
    "starts",
    "tax_basis",
    "text",
    "unit",
    "waste_cost_subcategory",
    "waste_subcategory",
]
