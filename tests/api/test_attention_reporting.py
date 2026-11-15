"""Synthetic coverage, human decisions and invoice totals share honest projections."""

from contextlib import contextmanager
from datetime import UTC, date, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from bills.attention_reporting import read_attention_report
from bills.invoice_coverage import mark_invoice_not_sent
from bills.models import (
    ApiToken,
    BillingSection,
    Document,
    ExtractionAttempt,
    Invoice,
    Property,
    PropertyService,
    ServiceVendorAssignment,
    Subcategory,
    Vendor,
    VendorBillingSchedule,
)
from bills.services import approve_review, record_extraction

from ..test_invoice_coverage import coverage  # noqa: F401
from .helpers import BASE
from .test_reporting_contract import get, records

pytestmark = pytest.mark.django_db(transaction=True)


def coverage_query(**kwargs):
    return {
        "as_of": date(2024, 3, 1),
        "date_from": date(2024, 2, 1),
        "date_to": date(2024, 2, 1),
        "property_id": None,
        "vendor_id": None,
        "category": None,
        "subcategory": None,
        "status": "all",
        **kwargs,
    }


def test_shared_expectation_and_deadline(api_client, coverage):  # noqa: F811
    client, _, _, _ = api_client
    for house, service in (
        (coverage["houses"][0], "water"),
        (coverage["houses"][0], "waste"),
        (coverage["houses"][1], "water"),
    ):
        coverage["schedule"](house, service)
    path = "missing-invoices?date_from=2024-02-20&date_to=2024-02-29&as_of=2024-03-01"
    result = get(client, path)
    assert (
        result["count"] == result["expected_invoice_count"] == result["missing_invoice_count"] == 1
    )
    row = result["items"][0]
    assert row["expectation_key"] == f"vendor:{coverage['vendor'].pk}:month:2024-02"
    assert row["days_overdue"] == 0 and row["missing_after"] == "2024-03-01"
    assert len(row["services"]) == 3 and len(row["properties"]) == 2
    assert row["period_start"] is row["period_end"] is row["document_id"] is None
    assert "schedule_id" not in row
    before = get(client, path.replace("as_of=2024-03-01", "as_of=2024-02-29"))
    assert before["items"][0]["status"] == "expected"
    assert before["items"][0]["days_overdue"] is None
    selected = get(client, path + f"&property_id={coverage['houses'][0].pk}")
    assert selected["count"] == 1 and len(selected["items"][0]["services"]) == 2
    summary = get(client, path + "&mode=summary")
    assert summary["counts_by_status"] == result["counts_by_status"]
    assert "items" not in summary and sum(summary["counts_by_status"].values()) == 1


def test_defaults_unknown_and_empty_selection(api_client, coverage):  # noqa: F811
    client, _, _, _ = api_client
    coverage["schedule"]()
    result = get(client, "missing-invoices?as_of=2024-04-15")
    assert result["filters"]["date_from"] == "2024-01-01"
    assert result["filters"]["date_to"] == "2024-04-01"
    for endpoint in ("missing-invoices", "review-queue", "attention", "invoice-costs"):
        empty = get(client, endpoint + "?property_id=999999")
        assert empty["count"] == 0 and empty["items"] == []
    from bills.api_reporting import read_invoice_costs

    # Internal empty sets have the same restrictive meaning as empty lists.
    empty_costs = read_invoice_costs(
        filters={
            "property_id": set(),
            "vendor_id": None,
            "category": None,
            "subcategory": None,
            "currency": None,
            "date_from": None,
            "date_to": None,
        }
    )
    assert empty_costs["count"] == 0 and empty_costs["items"] == []
    empty = read_attention_report(kind="coverage", filters=coverage_query(property_id=[]))
    assert empty["count"] == empty["expectation_count"] == 0
    empty = read_attention_report(
        kind="attention",
        filters={
            "property_id": [],
            "as_of": date(2024, 3, 1),
            "coverage_date_from": date(2024, 2, 1),
            "coverage_date_to": date(2024, 2, 1),
            "review_date_from": None,
            "review_date_to": None,
        },
    )
    assert (
        empty["count"] == empty["review_document_count"] == empty["coverage_attention_count"] == 0
    )


def pending_document(coverage, *, routed=True, failed=False):  # noqa: F811
    proposed, properties = coverage["candidate"](
        [(coverage["houses"][0], "water", "")], invoice_date=date(2024, 2, 20)
    )
    doc = coverage["document"](proposed.reference)
    record_extraction(
        doc.pk,
        candidate=proposed,
        page_evidence=[],
        error="Synthetic parse failure" if failed else "",
        routing={
            "vendor_id": coverage["vendor"].pk,
            "section_properties": {key: house.pk for key, house in properties.items()}
            if routed
            else {},
            "kind": "invoice",
        },
    )
    return doc


def test_unresolved_candidates_and_attention_union(api_client, coverage):  # noqa: F811
    client, _, _, _ = api_client
    coverage["schedule"]()
    first = pending_document(coverage, routed=False)
    second = pending_document(coverage)
    path = "missing-invoices?as_of=2024-03-01&date_from=2024-02-01&date_to=2024-02-29"
    result = get(client, path)
    row = result["items"][0]
    assert result["review_invoice_count"] == 1 and result["missing_invoice_count"] == 0
    assert row["pending_document_ids"] == [first.pk, second.pk]
    assert row["document_id"] is None
    queue = get(client, "review-queue")
    unresolved = next(row for row in queue["items"] if row["document_id"] == first.pk)
    assert unresolved["reason_key"] == "property_unresolved"
    assert unresolved["routing_status"] == "unresolved" and unresolved["properties"] == []
    assert unresolved["value_source"] == "proposal" and unresolved["gross_minor"] == 12500
    selected = get(client, f"review-queue?property_id={coverage['houses'][0].pk}")
    assert [row["document_id"] for row in selected["items"]] == [second.pk]
    combined = get(
        client, "attention?coverage_date_from=2024-02-01&coverage_date_to=2024-02-01&limit=1"
    )
    assert combined["count"] == combined["review_document_count"] == 2
    assert combined["coverage_attention_count"] == 1
    assert combined["items"][0]["kind"] == "document"
    assert combined["items"][0]["related_expectation_keys"] == [row["expectation_key"]]
    assert (
        combined["has_more"]
        and get(
            client, "attention?coverage_date_from=2024-02-01&coverage_date_to=2024-02-01&offset=2"
        )["items"]
        == []
    )
    next_params = parse_qs(urlsplit(combined["next"]).query)
    assert "as_of" in next_params and next_params["mode"] == ["rows"]
    assert next_params["coverage_date_from"] == ["2024-02-01"]


def test_manual_failed_worker_and_storage_membership(api_client, coverage):  # noqa: F811
    client, _, _, _ = api_client
    manual = Document.objects.create(source="manual", storage_state="none", state="review")
    pending_document(coverage, failed=True)
    for state in ("pending", "processing", "ready"):
        Document.objects.create(source="manual", storage_state="none", state=state)
    result = get(client, "review-queue?as_of=2020-01-01")
    assert result["count"] == 2
    row = next(row for row in result["items"] if row["document_id"] == manual.pk)
    assert row["reason_key"] == "manual_entry" and row["value_source"] == "empty"
    assert row["net_minor"] is row["vat_minor"] is row["gross_minor"] is row["invoice_date"] is None
    assert row["age_days"] == 0
    assert get(client, "review-queue?reason_key=parse_failed")["count"] == 1
    summary = get(client, "review-queue?mode=summary")
    assert sum(summary["counts_by_reason"].values()) == summary["review_document_count"] == 2
    assert get(client, "review-queue?date_to=2020-01-01")["count"] == 0


@pytest.mark.parametrize("approved", [False, True])
def test_review_reports_select_one_current_invoice_per_document(api_client, coverage, approved):  # noqa: F811
    client, _, _, _ = api_client
    coverage["schedule"]()
    proposed, properties = coverage["candidate"](
        [(coverage["houses"][0], "water", "")], invoice_date=date(2024, 2, 20)
    )
    doc = coverage["document"](proposed.reference)
    current = Invoice.objects.create(document=doc)
    if approved:
        # An approved record outranks an earlier draft as well as a later draft.
        current = Invoice.objects.create(document=doc)
        current = approve_review(
            current.pk,
            expected_revision=0,
            candidate=proposed,
            vendor=coverage["vendor"],
            section_properties=properties,
            actor=coverage["actor"],
        )
        properties = {proposed.sections[0].section_id: coverage["houses"][1]}
    Invoice.objects.create(document=doc)
    record_extraction(
        doc.pk,
        candidate=proposed,
        page_evidence=[],
        routing={
            "vendor_id": coverage["vendor"].pk,
            "section_properties": {key: house.pk for key, house in properties.items()},
            "kind": "invoice",
        },
    )
    queue = get(client, "review-queue?limit=1")
    assert queue["count"] == queue["review_document_count"] == 1 and not queue["has_more"]
    row = queue["items"][0]
    assert row["document_id"] == doc.pk and row["invoice_id"] == current.pk
    assert row["reason_key"] == ("proposal_changes" if approved else "awaiting_approval")
    if approved:
        assert {house["association"] for house in row["properties"]} == {"approved", "proposed"}
        selected = get(client, f"review-queue?property_id={coverage['houses'][0].pk}")
        assert selected["count"] == 1 and selected["items"][0]["invoice_id"] == current.pk
    summary = get(client, "review-queue?mode=summary")
    assert summary["review_document_count"] == sum(summary["counts_by_reason"].values()) == 1
    combined_path = "attention?coverage_date_from=2024-02-01&coverage_date_to=2024-02-01"
    combined = get(client, combined_path + "&limit=1")
    assert combined["count"] == combined["review_document_count"] == 1 and not combined["has_more"]
    assert combined["items"][0]["attention_key"] == f"document:{doc.pk}"
    assert combined["items"][0]["invoice_id"] == current.pk
    assert get(client, combined_path + "&offset=1")["items"] == []


def test_coverage_confirmation_reasons_preserve_legacy_rows(api_client, coverage):  # noqa: F811
    client, _, _, _ = api_client
    first = coverage["schedule"]()
    second = coverage["schedule"](coverage["houses"][1])
    mark_invoice_not_sent(
        first.pk, coverage["vendor"].pk, date(2024, 2, 1), reason="Synthetic scoped confirmation"
    )
    mark_invoice_not_sent(
        None, coverage["vendor"].pk, date(2024, 2, 1), reason="Synthetic combined bill confirmation"
    )
    bounds = "date_from=2024-02-01&date_to=2024-02-01&as_of=2024-03-01"
    row = get(client, "missing-invoices?" + bounds)["items"][0]
    assert row["status"] == "not_expected"
    services = {item["property_service_id"]: item for item in row["services"]}
    assert services[first.pk]["confirmation_reason"] == "Synthetic scoped confirmation"
    assert services[second.pk]["confirmation_reason"] == "Synthetic combined bill confirmation"
    response = client.get(BASE + "/reports/invoice-coverage?" + bounds)
    assert response.status_code == 200
    legacy = response.json()["coverage_rows"][0]
    legacy_services = {item["property_service_id"]: item for item in legacy["required_services"]}
    assert legacy_services[first.pk]["exception_reason"] == "Synthetic scoped confirmation"
    assert legacy_services[second.pk]["exception_reason"] == ""
    assert all("confirmation_reason" not in item for item in legacy_services.values())


def test_coverage_confirmation_reason_expires_with_review_revision(api_client, coverage):  # noqa: F811
    client, _, _, _ = api_client
    coverage["schedule"]()
    waste = coverage["schedule"](service="waste")
    invoice, proposed, properties = coverage["approve"](
        [(coverage["houses"][0], "water", "")], invoice_date=date(2024, 2, 20)
    )
    confirmation, _ = mark_invoice_not_sent(
        waste.pk,
        invoice.vendor_id,
        date(2024, 2, 1),
        reason="Synthetic reviewed allocation confirmation",
        reviewed_invoice_id=invoice.pk,
        reviewed_revision=invoice.review_revision,
    )
    path = "missing-invoices?date_from=2024-02-01&date_to=2024-02-01&as_of=2024-03-01"
    row = get(client, path)["items"][0]
    assert row["status"] == "received"
    services = {item["property_service_id"]: item for item in row["services"]}
    assert services[waste.pk]["status"] == "not_expected"
    assert services[waste.pk]["confirmation_reason"] == confirmation.reason
    assert all(
        item["confirmation_reason"] is None
        for item in row["services"]
        if item["status"] == "received"
    )
    approve_review(
        invoice.pk,
        expected_revision=invoice.review_revision,
        candidate=proposed,
        vendor=coverage["vendor"],
        section_properties=properties,
        actor=coverage["actor"],
    )
    row = get(client, path)["items"][0]
    assert row["status"] == "incomplete"
    services = {item["property_service_id"]: item for item in row["services"]}
    assert services[waste.pk]["status"] == "incomplete"
    assert services[waste.pk]["confirmation_reason"] is None
    confirmation.refresh_from_db()
    assert confirmation.reviewed_revision == 1
    assert confirmation.reason == "Synthetic reviewed allocation confirmation"


def test_incomplete_and_receipt_precedence(api_client, coverage):  # noqa: F811
    client, _, _, _ = api_client
    water = coverage["schedule"]()
    coverage["schedule"](coverage["houses"][0], "waste")
    invoice, _, _ = coverage["approve"](
        [(coverage["houses"][0], "water", "")], invoice_date=date(2024, 2, 20)
    )
    result = get(
        client, "missing-invoices?date_from=2024-02-01&date_to=2024-02-01&as_of=2024-03-01"
    )
    row = result["items"][0]
    assert row["status"] == "incomplete" and row["receipt_status"] == "received"
    assert result["received_invoice_count"] == 1
    assert row["approved_invoice_ids"] == [invoice.pk] and row["document_id"] == invoice.document_id
    assert row["services"][0]["property_service_id"] == water.pk
    combined = get(client, "attention?coverage_date_from=2024-02-01&coverage_date_to=2024-02-01")
    assert combined["count"] == 1 and combined["items"][0]["kind"] == "coverage"
    assert combined["items"][0]["coverage_path"].startswith("/invoices/coverage/")


def test_moved_reparse_preserves_associations_and_new_unknowns(api_client, coverage):  # noqa: F811
    client, _, _, _ = api_client
    invoice, proposed, _ = coverage["approve"]([(coverage["houses"][0], "water", "")])
    proposed.net_minor = None
    record_extraction(
        invoice.document_id,
        candidate=proposed,
        page_evidence=[],
        routing={
            "vendor_id": coverage["vendor"].pk,
            "section_properties": {proposed.sections[0].section_id: coverage["houses"][1].pk},
        },
    )
    queue = get(client, "review-queue")
    row = queue["items"][0]
    assert (
        row["reason_key"] == "proposal_changes" and row["net_minor"] is row["gross_minor"] is None
    )
    assert row["invoice_id"] == invoice.pk
    assert {p["association"] for p in row["properties"]} == {"approved", "proposed"}
    attempt = ExtractionAttempt.objects.get(
        document=invoice.document, revision=invoice.document.extraction_revision + 1
    )
    ExtractionAttempt.objects.filter(pk=attempt.pk).update(
        created_at=datetime(2026, 1, 5, 23, 59, tzinfo=UTC)
    )
    result = get(client, "review-queue?date_from=2026-01-05&date_to=2026-01-05&as_of=2026-01-06")
    assert result["items"][0]["age_days"] == 1
    for house in coverage["houses"]:
        assert get(client, f"review-queue?property_id={house.pk}")["count"] == 1


@pytest.mark.parametrize(
    "path",
    [
        "missing-invoices?property_id=",
        "missing-invoices?property_id=-1",
        "missing-invoices?status=bogus",
        "missing-invoices?as_of=2024-02-30",
        "missing-invoices?date_from=2024-02-20&date_to=2024-02-10",
        "review-queue?reason_key=unknown",
        "review-queue?mode=summary&offset=0",
        "attention?mode=summary&sort=attention_date",
        "attention?sort=kind,-kind",
        "attention?sort=",
        "attention?sort=kind,",
        "attention?sort=--kind",
        "attention?as_of=2024-01-01&as_of=2024-01-01",
        "attention?date_from=2024-01-01",
        "invoice-costs?mode=summary",
        "invoice-costs?limit=201",
        "invoice-costs?currency=EUR",
        "invoice-costs?sort=allocation_id",
    ],
)
def test_strict_parameters(api_client, path):
    response = api_client[0].get(BASE + "/reports/" + path)
    assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_parameter"


def test_invoice_costs_complete_filtered_null_signed_and_paged(api_client):
    client, house, _, invoices, sections, _ = records(api_client, count=2)
    other_house = Property.objects.create(
        name="Other synthetic house", slug="other-synthetic-house"
    )
    extra = [
        BillingSection(
            invoice=invoices[0],
            section_id=f"many-{i}",
            property_id=house["id"],
            category="water",
            net_minor=0,
            vat_minor=0,
            gross_minor=0,
        )
        for i in range(205)
    ]
    extra += [
        BillingSection(
            invoice=invoices[0],
            section_id="shared",
            property=other_house,
            category="water",
            net_minor=200,
            vat_minor=50,
            gross_minor=250,
        )
    ]
    BillingSection.objects.bulk_create(extra)
    from bills.api_reporting import read_invoice_costs

    empty = read_invoice_costs(
        filters={
            "property_id": set(),
            "vendor_id": None,
            "category": None,
            "subcategory": None,
            "currency": None,
            "date_from": None,
            "date_to": None,
        }
    )
    assert empty["count"] == 0 and empty["items"] == []
    first = get(client, "invoice-costs?limit=1&sort=invoice_id")
    assert first["count"] == 2 and len(first["items"]) == 1 and first["has_more"]
    row = first["items"][0]
    assert row["gross_minor"] == 375 and row["allocation_count"] == 207
    assert "sections" not in row and "allocation_id" not in row
    selected = get(client, f"invoice-costs?property_id={house['id']}&sort=invoice_id")["items"][0]
    assert selected["gross_minor"] == 125 and selected["allocation_count"] == 206
    assert selected["source"] == "manual" and len(selected["properties"]) == 1
    assert get(client, "invoice-costs?offset=2")["items"] == []
    BillingSection.objects.filter(pk=sections[0].pk).update(
        net_minor=None, vat_minor=None, gross_minor=None
    )
    unknown = get(client, "invoice-costs?sort=-gross_minor")
    assert unknown["items"][-1]["gross_minor"] is None
    Invoice.objects.filter(pk=invoices[1].pk).update(kind="credit")
    BillingSection.objects.filter(pk=sections[1].pk).update(
        net_minor=-100, vat_minor=-25, gross_minor=-125
    )
    assert get(client, "invoice-costs?sort=gross_minor")["items"][0]["gross_minor"] == -125


def test_reporting_credentials_and_guards(api_client, coverage, monkeypatch):  # noqa: F811
    client, owner, _, _ = api_client
    coverage["schedule"]()
    _, secret = ApiToken.issue(name="Synthetic reporting", owner=owner, profile="reporting")
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {secret}"
    for path in ("missing-invoices", "review-queue", "attention", "invoice-costs"):
        get(client, path)
    for path in ("/reports/invoice-coverage", "/documents", "/properties"):
        assert client.get(BASE + path).status_code == 403
    assert (
        client.post(
            BASE + "/reports/attention", data={}, content_type="application/json"
        ).status_code
        == 403
    )
    monkeypatch.setattr("bills.attention_reporting.MAX_GROUPS", 0)
    result = client.get(BASE + "/reports/missing-invoices?as_of=2024-03-01")
    assert result.status_code == 422 and result.json()["error"]["code"] == "filter_too_broad"


def test_identical_reparse_requires_completion(api_client, coverage):  # noqa: F811
    client, _, _, _ = api_client
    invoice, proposed, properties = coverage["approve"]([(coverage["houses"][0], "water", "")])
    record_extraction(
        invoice.document_id,
        candidate=proposed,
        page_evidence=[],
        routing={
            "vendor_id": coverage["vendor"].pk,
            "section_properties": {key: house.pk for key, house in properties.items()},
            "kind": "invoice",
        },
    )
    queue = get(client, "review-queue")
    assert queue["count"] == 1 and queue["items"][0]["reason_key"] == "reparse_confirmation"
    assert queue["items"][0]["properties"][0]["association"] == "both"
    Document.objects.filter(pk=invoice.document_id).update(
        state="ready", storage_error="Synthetic storage maintenance"
    )
    assert get(client, "review-queue")["count"] == 0


def test_review_and_invoice_pages_above_200(api_client):
    client, house, _, invoices, _, _ = records(api_client, count=211)
    first = get(client, f"invoice-costs?property_id={house['id']}&sort=-invoice_date&limit=200")
    assert first["count"] == 211 and len(first["items"]) == 200 and first["has_more"]
    continuation = parse_qs(urlsplit(first["next"]).query)
    assert continuation["date_from"] == ["2026-01-28"] and continuation["date_to"] == ["2026-02-28"]
    final = client.get(first["next"]).json()
    assert len(final["items"]) == 11 and final["count"] == 211 and not final["has_more"]
    docs = Document.objects.bulk_create(
        [Document(source="manual", storage_state="none", state="review") for _ in range(211)]
    )
    since = datetime(2026, 1, 1, 23, 59, tzinfo=UTC)
    Document.objects.filter(pk__in=[row.pk for row in docs]).update(created_at=since)
    first = get(client, "review-queue?limit=200&as_of=2026-01-02&sort=-attention_since")
    assert [row["document_id"] for row in first["items"]] == [row.pk for row in docs[:200]]
    assert first["count"] == 211 and first["items"][0]["age_days"] == 1
    final = client.get(first["next"]).json()
    assert [row["document_id"] for row in final["items"]] == [row.pk for row in docs[200:]]
    assert not final["has_more"] and final["previous"]
    combined = get(client, "attention?limit=200&as_of=2026-01-02")
    assert combined["count"] == 211 and len(combined["items"]) == 200
    assert len(set(row["attention_key"] for row in combined["items"])) == 200
    assert len(invoices) == 211


def test_one_snapshot_for_union_labels_membership_and_stats(api_client, coverage, monkeypatch):  # noqa: F811
    import bills.attention_reporting as reports

    client, _, _, _ = api_client
    coverage["schedule"]()
    doc = pending_document(coverage)
    original = reports.coverage_rows
    snapshots = []
    original_reader = reports.reporting_reader

    @contextmanager
    def reader():
        with original_reader() as connection:
            snapshots.append(connection)
            yield connection

    def concurrent_change(reader, filters, as_of):
        rows = original(reader, filters, as_of)
        Property.objects.filter(pk=coverage["houses"][0].pk).update(name="Changed concurrently")
        Document.objects.filter(pk=doc.pk).update(state="ready")
        return rows

    monkeypatch.setattr(reports, "reporting_reader", reader)
    monkeypatch.setattr(reports, "coverage_rows", concurrent_change)
    result = get(client, "attention?coverage_date_from=2024-02-01&coverage_date_to=2024-02-01")
    assert len(snapshots) == 1 and result["count"] == result["review_document_count"] == 1
    row = result["items"][0]
    assert (
        row["properties"][0]["property_name"]
        == row["services"][0]["property_name"]
        == "Synthetic House 1"
    )
    assert row["kind"] == "document"


def test_invoice_amount_and_labels_share_snapshot(api_client, monkeypatch):
    import bills.api_reporting as reports

    client, house, _, _, sections, _ = records(api_client)
    original_reader = reports.reporting_reader

    class ConcurrentReader:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, params=()):
            result = self.connection.execute(sql, params)
            if sql.endswith("SELECT count(*) FROM grouped"):
                Property.objects.filter(pk=house["id"]).update(name="Changed concurrently")
                BillingSection.objects.filter(pk=sections[0].pk).update(
                    net_minor=200, vat_minor=50, gross_minor=250
                )
            return result

    @contextmanager
    def reader():
        with original_reader() as connection:
            yield ConcurrentReader(connection)

    monkeypatch.setattr(reports, "reporting_reader", reader)
    result = get(client, "invoice-costs")
    assert result["items"][0]["gross_minor"] == 125
    assert result["items"][0]["properties"][0]["property_name"] == "Example house"


def test_population_guards_before_pagination(api_client, monkeypatch):
    client, _, _, _, _, _ = records(api_client)
    for kind, field in (("attention", "property_id"), ("invoice-costs", "vendor_id")):
        query = "&".join(f"{field}={pk}" for pk in range(1, 102))
        response = client.get(BASE + f"/reports/{kind}?" + query)
        assert (
            response.status_code == 422 and response.json()["error"]["code"] == "filter_too_broad"
        )
    monkeypatch.setattr("bills.api_reporting.MAX_GROUPS", 0)
    response = client.get(BASE + "/reports/invoice-costs?offset=100")
    assert response.status_code == 422 and response.json()["error"]["code"] == "filter_too_broad"
    Document.objects.create(source="manual", state="review", storage_state="none")
    monkeypatch.setattr("bills.attention_reporting.MAX_GROUPS", 0)
    response = client.get(BASE + "/reports/review-queue?offset=100")
    assert response.status_code == 422 and response.json()["error"]["code"] == "filter_too_broad"


def test_generated_examples_and_query_schemas():
    import json
    from pathlib import Path

    from bills.api.router import api
    from tools.generate_reporting_examples import generate

    assert generate() == Path("docs/api/reporting-examples.json").read_text(encoding="utf-8")
    artifact = json.loads(generate())
    assert len(artifact["infinity_queries"]) == 4
    schema = api.get_openapi_schema()
    for endpoint in ("missing-invoices", "review-queue", "attention", "invoice-costs"):
        path = schema["paths"][BASE + "/reports/" + endpoint]
        assert "get" in path and "post" not in path
    parameters = schema["paths"][BASE + "/reports/review-queue"]["get"]["parameters"]
    reasons = next(param["schema"]["enum"] for param in parameters if param["name"] == "reason_key")
    assert "reparse_confirmation" in reasons


def test_custom_catalogue_and_reparse_do_not_read_through_orm(coverage, django_assert_num_queries):  # noqa: F811
    subcategory = Subcategory.objects.create(
        key="synthetic-water", name="Synthetic water label", category="water"
    )
    coverage["schedule"](subcategory=subcategory)
    invoice, proposed, properties = coverage["approve"](
        [(coverage["houses"][0], "water", subcategory.pk)], invoice_date=date(2024, 2, 20)
    )
    record_extraction(
        invoice.document_id,
        candidate=proposed,
        page_evidence=[],
        routing={
            "vendor_id": coverage["vendor"].pk,
            "section_properties": {key: house.pk for key, house in properties.items()},
        },
    )
    with django_assert_num_queries(0):
        result = read_attention_report(
            kind="attention",
            filters={
                "property_id": None,
                "as_of": date(2024, 3, 1),
                "coverage_date_from": date(2024, 2, 1),
                "coverage_date_to": date(2024, 2, 1),
                "review_date_from": None,
                "review_date_to": None,
            },
        )
    assert result["items"][0]["reason_key"] == "reparse_confirmation"
    assert result["items"][0]["services"][0]["subcategory_name"] == "Synthetic water label"


def test_coverage_unique_keys_and_pagination_above_200(api_client):
    client, _, _, _ = api_client
    house = Property.objects.create(name="Synthetic shared house", slug="synthetic-shared-house")
    vendors = Vendor.objects.bulk_create(
        [Vendor(name="Tied synthetic vendor", slug=f"tie-{i}") for i in range(211)]
    )
    service = PropertyService.objects.create(
        property=house, category="water", start_month=date(2026, 1, 1)
    )
    ServiceVendorAssignment.objects.bulk_create(
        [
            ServiceVendorAssignment(
                property_service=service, vendor=vendor, start_month=date(2026, 1, 1)
            )
            for vendor in vendors
        ]
    )
    VendorBillingSchedule.objects.bulk_create(
        [
            VendorBillingSchedule(
                vendor=vendor,
                start_month=date(2026, 1, 1),
                first_expected_month=date(2026, 1, 1),
                frequency_months=1,
            )
            for vendor in vendors
        ]
    )
    first = get(
        client,
        "missing-invoices?as_of=2026-02-01&date_from=2026-01-01&date_to=2026-01-01&limit=200",
    )
    assert first["count"] == first["missing_invoice_count"] == 211 and len(first["items"]) == 200
    keys = [row["expectation_key"] for row in first["items"]]
    final = client.get(first["next"]).json()
    keys += [row["expectation_key"] for row in final["items"]]
    assert len(keys) == len(set(keys)) == 211 and keys == sorted(keys)
    assert final["count"] == 211 and len(final["items"]) == 11 and not final["has_more"]
