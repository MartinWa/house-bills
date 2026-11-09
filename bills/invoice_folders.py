"""Browse approved originals by their recorded paths without touching storage."""

from pathlib import PurePosixPath

from .models import Document, Property, Vendor


def _add_document(folders, parts, entry, *, manual=False):
    for key, name in parts:
        folder = folders.setdefault(
            key, {"name": name, "children": {}, "documents": [], "count": 0, "manual": manual}
        )
        folder["count"] += 1
        folders = folder["children"]
    folder["documents"].append(entry)


def _ordered_folders(folders):
    # Show recent years first; property and vendor names remain alphabetical.
    years = all(key.isdigit() for key in folders)
    ordered = sorted(folders.values(), key=lambda folder: folder["name"].casefold(), reverse=years)
    for folder in ordered:
        folder["children"] = _ordered_folders(folder["children"])
    return ordered


def invoice_folders(invoices):
    properties = dict(Property.objects.values_list("slug", "name"))
    vendors = dict(Vendor.objects.values_list("slug", "name"))
    documents = {}
    for invoice in invoices:
        entry = documents.setdefault(
            invoice.document_id,
            {
                "document": invoice.document,
                "invoices": [],
                "filename": PurePosixPath(invoice.document.original_path).name,
            },
        )
        entry["invoices"].append(invoice)

    folders, manual_folders, attention_folders = {}, {}, {}
    pdf_count = manual_count = 0
    for entry in documents.values():
        document = entry["document"]
        first = min(entry["invoices"], key=lambda invoice: (invoice.invoice_date, invoice.pk))
        if document.source == Document.Source.MANUAL:
            manual_count += 1
            houses = {
                section.property.slug: section.property.name
                for invoice in entry["invoices"]
                for section in invoice.sections.all()
            }
            house = next(iter(houses.items())) if len(houses) == 1 else ("shared", "Shared")
            parts = [
                ("manual", "Manual entries"),
                house,
                (first.vendor.slug, first.vendor.name),
                (str(first.invoice_date.year), str(first.invoice_date.year)),
            ]
            _add_document(manual_folders, parts, entry, manual=True)
            continue

        pdf_count += 1
        parts = PurePosixPath(document.original_path).parts
        if len(parts) < 2 or parts[0].startswith("."):
            _add_document(attention_folders, [("storage", "Storage needs attention")], entry)
            continue
        labels = []
        for index, part in enumerate(parts[:-1]):
            if index == 0:
                name = {"shared": "Shared", "inbox": "Inbox"}.get(part, properties.get(part, part))
            elif index == 1:
                name = vendors.get(part, part)
            else:
                name = part
            labels.append((part, name))
        _add_document(folders, labels, entry)

    return {
        "folders": [
            *_ordered_folders(folders),
            *_ordered_folders(attention_folders),
            *_ordered_folders(manual_folders),
        ],
        "pdf_count": pdf_count,
        "manual_count": manual_count,
    }
