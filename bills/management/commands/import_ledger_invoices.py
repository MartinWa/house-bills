"""Import reviewed ledger rows as manual invoices without original PDFs.

The input is the reviewed `comparison.json` produced from the household ledger
and the parsed-invoice snapshot, or a self-contained live input written by
`--export`. Only missing rows are imported: those exist in the ledger but have no
original PDF in the archive.

Every row is transcribed into the normal manual-entry path, so approval,
identity checks, validation, review history and reporting behave exactly like a
manual entry typed in the browser. Nothing is approved without the normal
service boundary.

VAT is derived from the ledger's gross amount. Ledger totals are whole kronor or
two-decimal kronor and contain no VAT split, while every matched invoice in the
same archive follows `net + 25 % VAT = gross`. The derived VAT is flagged in the
review note.

Billing periods are inferred from each vendor's own median convention among the
matched approved invoices: the offset from invoice date to period start and the
billed length. The resolved convention is printed and can be pinned with
`--period` for an exact repeat. Consumption quantities are not imported because
their inferred periods cannot be trusted; the cost allocation is the missing
reporting data.
"""

import json
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from statistics import median_low

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from bills.models import BillingSection, Document, Invoice, Property, Vendor
from bills.parsing.types import Candidate, Section
from bills.services import approve_document_review
from bills.storage import coordinated_write

# Ledger column names predate the configured vendor names and include a UTF-8
# read as Latin-1 in the reviewed snapshot. Keys are case-folded after repair.
VENDOR_ALIASES = {
    "seom": "seom",
    "borås elhandel ab": "boras-elhandel",
    "vattenfall": "vattenfall",
    "telinet energi": "telinet-energi",
    "borab": "borab",
    "elevio": "ellevio",
    "skellefteå kraft": "skelleftea-kraft",
    "telge energi ab": "telge-energi",
}
DEFAULT_OFFSET = 30
DEFAULT_LENGTH = 30
PARSER_REVISION = "ledger-import-v1"
LIVE_FORMAT = "house-bills-ledger-import-input"


def repair_text(value):
    """Undo a UTF-8 sequence read as Latin-1, without rejecting valid text."""
    if not isinstance(value, str):
        return ""
    try:
        return value.encode("latin-1").decode("utf-8")
    except UnicodeEncodeError, UnicodeDecodeError:
        return value


def minor_units(amount):
    return int(Decimal(str(amount)).quantize(Decimal("0.01"), ROUND_HALF_UP) * 100)


def derived_net(gross_minor):
    """Apply the archive's 25 % VAT relationship, rounded to whole öre."""
    return (gross_minor * 4 + 2) // 5


def parse_pairs(values, label):
    pairs = {}
    for value in values:
        key, _, target = value.partition("=")
        if not key.strip() or not target.strip():
            raise CommandError(f"Use {label} KEY=VALUE for {value!r}.")
        pairs[repair_text(key).strip().casefold()] = target.strip()
    return pairs


def parse_periods(values):
    periods = {}
    for value in values:
        key, _, target = value.partition("=")
        offset, _, length = target.partition(":")
        try:
            offset, length = int(offset), int(length)
        except ValueError:
            raise CommandError(f"Use --period VENDOR=OFFSET:LENGTH for {value!r}.") from None
        if not 0 <= offset <= 366 or not 1 <= length <= 366:
            raise CommandError("Billing period offsets and lengths must be within 0..366 days.")
        periods[repair_text(key).strip().casefold()] = (offset, length)
    return periods


class Command(BaseCommand):
    help = "Import reviewed ledger rows without original PDFs as manual invoices."

    def add_arguments(self, parser):
        parser.add_argument(
            "--source",
            required=True,
            type=Path,
            help="Reviewed comparison JSON; only the only_manual rows are imported.",
        )
        parser.add_argument("--actor", required=True, help="Active username recorded as reviewer.")
        parser.add_argument(
            "--vendor",
            action="append",
            default=[],
            metavar="LEDGER_VENDOR=SLUG",
            help="Override the ledger vendor resolution; repeatable.",
        )
        parser.add_argument(
            "--property",
            action="append",
            default=[],
            metavar="LEDGER_ID=SLUG",
            help="Override the ledger property resolution; repeatable.",
        )
        parser.add_argument(
            "--period",
            action="append",
            default=[],
            metavar="VENDOR=OFFSET:LENGTH",
            help="Pin an inferred billing period convention; repeatable.",
        )
        parser.add_argument(
            "--plan",
            type=Path,
            help="Write the resolved, reproducible import plan as JSON.",
        )
        parser.add_argument(
            "--export",
            type=Path,
            help="Write a self-contained live import input; no invoices are created.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Process at most this many missing rows (for a staged import).",
        )
        parser.add_argument(
            "--commit",
            action="store_true",
            help="Approve and write invoices. Without it, only report the plan.",
        )

    def handle(self, *args, **options):
        source = options["source"].resolve()
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except OSError, ValueError:
            raise CommandError("The comparison file is unreadable or not JSON.") from None
        live_input = isinstance(payload, dict) and payload.get("format") == LIVE_FORMAT
        rows = (
            payload.get("rows")
            if live_input
            else payload.get("only_manual")
            if isinstance(payload, dict)
            else payload
        )
        if not isinstance(rows, list):
            raise CommandError("The source needs an only_manual or rows list.")
        if options["limit"] is not None:
            if options["limit"] < 1:
                raise CommandError("--limit must be a positive number.")
            rows = rows[: options["limit"]]
        actor = self.actor(options["actor"])
        vendors = parse_pairs(options["vendor"], "--vendor")
        properties = parse_pairs(options["property"], "--property")
        pinned = parse_periods(options["period"])

        resolved = []
        for row in rows:
            resolved.append(self.resolve(row, vendors, properties, pinned))

        plan = {
            "format": "house-bills-ledger-import",
            "version": 1,
            "generated_at": timezone.now().isoformat(),
            "mode": "commit" if options["commit"] else "dry-run",
            "source": {"path": str(source), "rows": len(rows)},
            "periods": {},
            "rows": [],
            "summary": {"imported": 0, "skipped": 0, "failed": 0, "gross_minor": 0},
        }
        seen_periods = {}
        for item in resolved:
            if item["error"]:
                plan["rows"].append(self.plan_row(item))
                plan["summary"]["failed"] += 1
                self.stderr.write(f"ERROR {item['label']}: {item['error']}")
                continue
            self.period_convention(item, pinned)
            seen_periods[(item["vendor"].pk, item["property"].pk)] = (
                item["offset"],
                item["length"],
            )
            reason = self.skip_reason(item)
            if reason:
                item["status"] = "skip"
                item["reason"] = reason
                plan["summary"]["skipped"] += 1
                self.stdout.write(f"SKIP  {item['label']}: {reason}")
                plan["rows"].append(self.plan_row(item))
                continue
            if options["commit"]:
                try:
                    self.import_row(item, actor)
                except Exception as exc:  # Keep an auditable partial import.
                    item["status"] = "failed"
                    item["error"] = f"{type(exc).__name__}: {exc}"
                    plan["summary"]["failed"] += 1
                    self.stderr.write(f"ERROR {item['label']}: {item['error']}")
                    plan["rows"].append(self.plan_row(item))
                    continue
                else:
                    item["status"] = "imported"
                    self.stdout.write(f"ADDED {item['label']}")
            else:
                item["status"] = "would import"
                self.stdout.write(f"WOULD {item['label']}")
            plan["summary"]["imported"] += 1
            plan["summary"]["gross_minor"] += item["invoice_total_minor"]
            plan["rows"].append(self.plan_row(item))

        for (vendor_pk, _property_pk), (offset, length) in sorted(seen_periods.items()):
            vendor = Vendor.objects.get(pk=vendor_pk)
            plan["periods"][vendor.slug] = {"offset_days": offset, "length_days": length}
        gross_total = Decimal(plan["summary"]["gross_minor"]) / 100
        plan["summary"]["gross_total"] = f"{gross_total.quantize(Decimal('0.01')):.2f}"

        if options["export"]:
            self.export_input(options["export"], source, resolved)
            self.stdout.write(f"Live import input written to {options['export']}.")
        if options["plan"]:
            options["plan"].write_text(
                json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            self.stdout.write(f"Plan written to {options['plan']}.")
        suffix = "" if options["commit"] else " (dry run; use --commit to write)"
        self.stdout.write(
            f"{len(rows)} rows: {plan['summary']['imported']} ready, "
            f"{plan['summary']['skipped']} already present, {plan['summary']['failed']} failed, "
            f"gross {plan['summary']['gross_total']} SEK{suffix}."
        )
        if plan["summary"]["failed"]:
            raise CommandError("Some rows failed; nothing else was hidden. Fix and rerun.")

    def export_input(self, destination, source, resolved):
        """Freeze resolved slugs and periods so a live import needs only this file."""
        rows = [self.live_row(item) for item in resolved if not item["error"]]
        payload = {
            "format": LIVE_FORMAT,
            "version": 1,
            "generated_at": timezone.now().isoformat(),
            "source": {"path": str(source), "rows": len(rows)},
            "note": (
                "Self-contained ledger import prepared by import_ledger_invoices. "
                "Rows contain the reviewed ledger amounts, derived service split, "
                "resolved vendor/property slugs and pinned billing periods. "
                "Keep it private; it contains real billing data."
            ),
            "rows": rows,
        }
        destination.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + chr(10), encoding="utf-8"
        )

    def live_row(self, item):
        return {
            "ledger_vendor": item["ledger_vendor"],
            "vendor_slug": item["vendor"].slug,
            "property_slug": item["property"].slug,
            "date": item["date"],
            "reference": item["reference"],
            "sheet": item["sheet"],
            "services": item["services"],
            "total": item["total"],
            "offset_days": item["offset"],
            "length_days": item["length"],
        }

    def plan_row(self, item):
        """Keep the plan JSON-only; model instances never enter the artifact."""
        row = dict(item)
        row["vendor"] = item["vendor"].slug if "vendor" in item else item["ledger_vendor"]
        row["property"] = item["property"].slug if "property" in item else item["property_id"]
        for field, value in row.items():
            if isinstance(value, date):
                row[field] = value.isoformat()
        return row

    def actor(self, username):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.filter(username=username, is_active=True).first()
        if user is None:
            raise CommandError("The review actor must be an existing active user.")
        return user

    def resolve(self, row, vendor_overrides, property_overrides, pinned):
        ledger_vendor = row.get("ledger_vendor") or row.get("vendor") or row.get("vendor_slug")
        item = {
            "property_id": row.get("property_id"),
            "vendor_slug": row.get("vendor_slug") or "",
            "property_slug": row.get("property_slug") or "",
            "offset_days": row.get("offset_days"),
            "length_days": row.get("length_days"),
            "ledger_vendor": repair_text(ledger_vendor or ""),
            "date": row.get("date"),
            "sheet": repair_text(row.get("sheet", "")),
            "reference": (row.get("reference") or "").strip(),
            "services": row.get("services") or {},
            "total": row.get("total"),
            "ledger_row": row.get("row") or {},
            "status": "error",
            "error": "",
        }
        place = item["property_slug"] or item["property_id"] or "?"
        item["label"] = f"{item['ledger_vendor']} {item['date']} property {place}"
        try:
            item["vendor"] = self.vendor(item, vendor_overrides)
            item["property"] = self.property(item, property_overrides)
            if not item["date"] or not item["services"] or item["total"] is None:
                raise ValueError("The row lacks a date, service allocation or total.")
            item["invoice_date"] = date.fromisoformat(item["date"])
            item["reference"] = item["reference"] or (
                f"LEDGER-{item['date']}-P{item['property'].pk}"
            )
            self.money(item)
        except (CommandError, ValueError, TypeError) as exc:
            item["error"] = str(exc) or "The row is invalid."
        return item

    def vendor(self, item, overrides):
        if item["vendor_slug"]:
            vendor = Vendor.objects.filter(slug=item["vendor_slug"]).first()
            if vendor is None:
                raise CommandError(f"Unknown vendor slug {item['vendor_slug']!r}.")
            return vendor
        key = (
            overrides.get(item["ledger_vendor"].casefold())
            or VENDOR_ALIASES.get(item["ledger_vendor"].casefold())
            or item["ledger_vendor"]
        )
        vendor = (
            Vendor.objects.filter(slug=key).first()
            or Vendor.objects.filter(name__iexact=item["ledger_vendor"]).first()
        )
        if vendor is None:
            raise CommandError(f"Unknown vendor {item['ledger_vendor']!r}; use --vendor.")
        return vendor

    def property(self, item, overrides):
        ledger_id = str(item["property_id"])
        slug = item["property_slug"] or overrides.get(ledger_id)
        house = (
            Property.objects.filter(slug=slug).first()
            if slug
            else Property.objects.filter(pk=item["property_id"]).first()
        )
        if house is None:
            raise CommandError(f"Unknown property {ledger_id!r}; use --property.")
        return house

    def money(self, item):
        sections, net_total, vat_total, gross_total = [], 0, 0, 0
        for index, (service, amount) in enumerate(sorted(item["services"].items()), 1):
            gross = minor_units(amount)
            if gross == 0:
                continue
            net = derived_net(gross)
            vat = gross - net
            net_total += net
            vat_total += vat
            gross_total += gross
            sections.append(
                {
                    "section_id": f"ledger-{index}-{service}",
                    "service": service,
                    "net_minor": net,
                    "vat_minor": vat,
                    "gross_minor": gross,
                }
            )
        if not sections:
            raise ValueError("All service allocations are zero.")
        total = minor_units(item["total"])
        rounding = total - gross_total
        if abs(rounding) > 100:
            raise ValueError(
                f"Service allocations differ from the ledger total by {rounding / 100:.2f} SEK."
            )
        item.update(
            net_minor=net_total,
            vat_minor=vat_total,
            rounding_minor=rounding,
            invoice_total_minor=total,
            amount_due_minor=total,
            sections=sections,
        )

    def period_convention(self, item, pinned):
        key = item["ledger_vendor"].casefold()
        if item["offset_days"] is not None and item["length_days"] is not None:
            item["offset"], item["length"] = int(item["offset_days"]), int(item["length_days"])
        elif key in pinned:
            item["offset"], item["length"] = pinned[key]
        elif item["vendor"].slug in pinned:
            item["offset"], item["length"] = pinned[item["vendor"].slug]
        else:
            samples = self.period_samples(item["vendor"])
            if samples:
                item["offset"] = median_low([offset for offset, _ in samples])
                item["length"] = median_low([length for _, length in samples])
            else:
                item["offset"], item["length"] = DEFAULT_OFFSET, DEFAULT_LENGTH
        start = item["invoice_date"] - timedelta(days=item["offset"])
        item["period_start"] = start
        item["period_end"] = start + timedelta(days=item["length"])

    def period_samples(self, vendor):
        sections = (
            BillingSection.objects.filter(
                invoice__status=Invoice.Status.APPROVED,
                invoice__vendor=vendor,
                start__isnull=False,
                end__isnull=False,
            )
            .select_related("invoice")
            .only("start", "end", "invoice__invoice_date")
        )
        samples = []
        for section in sections:
            offset = (section.invoice.invoice_date - section.start).days
            length = (section.end - section.start).days
            if 0 <= offset <= 90 and 1 <= length <= 92:
                samples.append((offset, length))
        return samples

    def skip_reason(self, item):
        existing = Invoice.objects.filter(
            status=Invoice.Status.APPROVED,
            vendor=item["vendor"],
            reference=item["reference"],
            kind=Invoice.Kind.INVOICE,
        )
        if existing.exists():
            return "an approved invoice already uses this vendor and reference"
        same_date = Invoice.objects.filter(
            status=Invoice.Status.APPROVED,
            vendor=item["vendor"],
            invoice_date=item["invoice_date"],
            kind=Invoice.Kind.INVOICE,
            invoice_total_minor=item["invoice_total_minor"],
            sections__property=item["property"],
        )
        if same_date.exists():
            return "an approved invoice already has this vendor, property, date and total"
        return ""

    @coordinated_write
    @transaction.atomic
    def import_row(self, item, actor):
        note = (
            f"Transcribed from the household ledger, sheet {item['sheet']!r}, "
            f"row dated {item['date']}. Original invoice unavailable. "
            f"VAT derived from the ledger gross amount at 25 %. "
            f"Billing period {item['period_start']} to {item['period_end']} inferred "
            f"from this vendor's matched invoices. Consumption was not transcribed. "
            f"Amounts were not verified against an original."
        )
        document = Document.objects.create(
            source=Document.Source.MANUAL,
            state=Document.State.REVIEW,
            processing_message="Manual ledger entry; no original PDF.",
        )
        sections = [
            Section(
                section_id=row["section_id"],
                service=row["service"],
                identifiers=[],
                start=item["period_start"],
                end=item["period_end"],
                net_minor=row["net_minor"],
                vat_minor=row["vat_minor"],
                gross_minor=row["gross_minor"],
            )
            for row in item["sections"]
        ]
        candidate = Candidate(
            layout_family="manual",
            layout_version="ledger-import",
            parser_revision=PARSER_REVISION,
            reference=item["reference"],
            invoice_date=item["invoice_date"],
            due_date=None,
            currency="SEK",
            net_minor=item["net_minor"],
            vat_minor=item["vat_minor"],
            rounding_minor=item["rounding_minor"],
            invoice_total_minor=item["invoice_total_minor"],
            amount_due_minor=item["amount_due_minor"],
            header_evidence={},
            sections=sections,
            issues=[],
            requires_review=True,
        )
        approve_document_review(
            document.pk,
            expected_invoice_id=None,
            expected_extraction_revision=0,
            expected_revision=0,
            candidate=candidate,
            vendor=item["vendor"],
            section_properties={section.section_id: item["property"] for section in sections},
            actor=actor,
            extraction=None,
            kind=Invoice.Kind.INVOICE,
            note=note,
            amount_due_confirmed=False,
        )
        invoice = document.invoices.get()
        item["invoice_id"] = invoice.pk
        item["document_id"] = document.pk
