"""Parser-directory health shared by the status page and the `parsers` command.

Like `bills.parser_profiles`, this module stays outside `bills.parsing` so that
rendering health never moves the host implementation digest.
"""

from django.conf import settings

from .models import Vendor, VendorLayoutFamily
from .parsing import registry
from .parsing.plugins import safe_filename

OK = "ok"
ERRORS = "errors"
EMPTY = "empty"
UNCONFIGURED = "unconfigured"
MAX_CODE = 40


def _code(value):
    text = value if isinstance(value, str) else ""
    return "".join(character for character in text if character.isalnum() or character in "_-")[
        :MAX_CODE
    ]


def configured_directory():
    """Whether a parser directory is configured, without exposing its path."""
    return bool(getattr(settings, "HOUSE_BILLS_PARSER_PLUGINS", "") or "")


def directory_state(status):
    """Ordered notice state: load errors first, then emptiness, then configuration.

    A failed discovery produces neither parsers nor files, and directory-level errors
    carry no filename, so the error branch has to decide before emptiness. Whether a
    directory is configured comes from the setting, never from `Discovery.directory`,
    which holds a realpath on the normal path and the raw setting after a failure.
    """
    errors = [
        {
            "filename": safe_filename(error.filename) if error.filename else "",
            "code": _code(error.code),
        }
        for error in status.errors
    ]
    if errors:
        state = ERRORS
    elif status.parsers:
        state = OK
    elif configured_directory():
        state = EMPTY
    else:
        state = UNCONFIGURED
    return {"state": state, "files": status.files, "errors": errors}


def missing_vendor_formats():
    """Configured vendor invoice formats with no installed parser, in family order."""
    installed_families = set(registry.layouts())
    families = VendorLayoutFamily.objects.order_by("family").values_list("family", flat=True)
    missing = []
    for family in dict.fromkeys(families):
        if family in installed_families:
            continue
        missing.append(
            {
                "family": family,
                "vendors": list(
                    Vendor.objects.filter(layout_families__family=family).order_by("name", "pk")
                ),
            }
        )
    return missing
