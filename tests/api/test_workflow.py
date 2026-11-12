"""HTTP contract checks use invented records and disposable original storage."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connections
from django.test import Client

from bills.api.middleware import JSON_LIMIT
from bills.api.schemas import ReviewEditsIn
from bills.models import (
    BillingSection,
    Document,
    Invoice,
    ReviewRevision,
    Usage,
    Vendor,
)
from bills.review_edits import ReviewEdits, build_review
from bills.review_forms import ReviewForms
from bills.reviewing import current_review_context
from bills.storage import private_path, publish_original

from ..synthetic import shared_supply, write_pdf
from ..test_review import posted_fields
from .helpers import BASE, approve_manual, created, manual_body, send, setup

pytestmark = pytest.mark.django_db(transaction=True)


def test_largest_unicode_review_fits_body_limit_and_preserves_all_rows(api_client):
    client, _, _, _ = api_client
    house, vendor = setup(client)
    document = send(client, "post", "/manual-invoices", {}).json()["document"]
    source = client.get(document["links"]["review"]).json()
    edits = manual_body(house, vendor)
    edits["header"]["reference"] = "\U0001f30a" * 120
    edits["header"].update(
        net_minor=200000, vat_minor=50000, invoice_total_minor=250000, amount_due_minor=250000
    )
    sections = []
    for index in range(20):
        section = copy.deepcopy(edits["sections"][0])
        section["section_id"] = "\U0001f30a" * 118 + f"{index:02}"
        sections.append(section)
    edits["sections"] = sections
    edits["usages"] = [
        {**edits["usages"][0], "section_id": sections[index % 20]["section_id"]}
        for index in range(60)
    ]
    edits["identifiers"] = [
        {
            "scope": sections[index % 20]["section_id"],
            "kind": "delivery_address",
            "value": "\U0001f30a" * 200,
        }
        for index in range(60)
    ]
    edits["note"] = "\U0001f30a" * 2000
    payload = {"review_token": source["review_token"], "edits": edits}
    assert len(json.dumps(payload).encode()) <= JSON_LIMIT
    result = send(client, "post", f"/documents/{document['id']}/reviews", payload)
    assert result.status_code == 201, result.content
    assert BillingSection.objects.count() == 20 and Usage.objects.count() == 60
    assert result.json()["note"] == edits["note"]


@pytest.mark.parametrize("kind", ["delivery_address", "installation", "organization", "vat"])
@pytest.mark.parametrize(("character", "expansion"), [("ß", "ss"), ("ﬃ", "ffi")])
@pytest.mark.parametrize("scope", ["invoice", "water"])
def test_review_validates_normalized_identifier_length_and_keeps_corrections_usable(
    api_client, kind, character, expansion, scope
):
    client, _, _, _ = api_client
    house, vendor = setup(client)
    document = send(client, "post", "/manual-invoices", {}).json()["document"]
    source = client.get(document["links"]["review"]).json()
    value = "a" * (200 - len(expansion)) + character
    expected = "a" * (200 - len(expansion)) + expansion
    if kind != "delivery_address":
        expected = expected.upper()
    edits = manual_body(house, vendor)
    edits["identifiers"] = [{"scope": scope, "kind": kind, "value": value}]
    result = send(
        client,
        "post",
        f"/documents/{document['id']}/reviews",
        {"review_token": source["review_token"], "edits": edits},
    )
    assert result.status_code == 201, result.content
    source = client.get(document["links"]["review"]).json()
    assert source["edits"]["identifiers"][0]["value"] == expected
    source["edits"]["note"] = "Correction with the normalized identifier retained."
    correction = send(
        client,
        "post",
        f"/documents/{document['id']}/reviews",
        {"review_token": source["review_token"], "edits": source["edits"]},
    )
    assert correction.status_code == 201, correction.content
    snapshot = correction.json()["snapshot"]
    source = client.get(document["links"]["review"]).json()
    source["edits"]["identifiers"][0]["value"] = value + "a"
    rejected = send(
        client,
        "post",
        f"/documents/{document['id']}/reviews",
        {"review_token": source["review_token"], "edits": source["edits"]},
    )
    assert rejected.status_code == 422, rejected.content
    assert any(
        detail["path"] == "identifiers" and detail["code"] == "max_length"
        for detail in rejected.json()["error"]["details"]
    )
    invoice = Invoice.objects.get(document_id=document["id"])
    assert invoice.review_revision == 2 and invoice.reviews.count() == 2
    assert invoice.reviews.get(revision=2).snapshot == snapshot


def test_review_retains_late_source_evidence_when_large_extraction_is_trimmed(api_client):
    from dataclasses import replace

    from bills.parsing.types import Evidence, Identifier
    from bills.services import record_extraction

    client, _, _, _ = api_client
    house, vendor = setup(client)
    candidate, properties = build_review(
        ReviewEdits(**ReviewEditsIn.model_validate(manual_body(house, vendor)).model_dump()),
        values={},
    )
    candidate.layout_family, candidate.layout_version, candidate.parser_revision = (
        "layout_01",
        "v2",
        "synthetic",
    )
    source_count = 64
    original_usage = candidate.sections[0].usages[0]
    candidate.sections[0].usages = [
        replace(original_usage, evidence=Evidence(1, f"Observed usage {index}", (1, 2, 3, 4)))
        for index in range(source_count)
    ]
    candidate.identifiers = [
        Identifier(
            "contract",
            f"CONTRACT-{index}",
            Evidence(1, f"Observed identifier {index}", (2, 3, 4, 5)),
        )
        for index in range(source_count)
    ]
    document = Document.objects.create(
        original_filename="synthetic.pdf", sha256="a" * 64, state=Document.State.REVIEW
    )
    attempt = record_extraction(
        document.pk,
        candidate,
        vendor=Vendor.objects.get(pk=vendor["id"]),
        routing={
            "vendor_id": vendor["id"],
            "section_properties": {key: value.pk for key, value in properties.items()},
        },
    )
    source = client.get(BASE + f"/documents/{document.pk}/review").json()
    edits = source["edits"]
    edits["usages"] = [edits["usages"][63], edits["usages"][60]]
    edits["identifiers"] = [edits["identifiers"][62], edits["identifiers"][60]]
    for group in ("usages", "identifiers"):
        invalid = copy.deepcopy(edits)
        invalid[group][0]["source_index"] = source_count
        result = send(
            client,
            "post",
            f"/documents/{document.pk}/reviews",
            {"review_token": source["review_token"], "edits": invalid},
        )
        assert result.status_code == 422, result.content
        assert not ReviewRevision.objects.exists()
    result = send(
        client,
        "post",
        f"/documents/{document.pk}/reviews",
        {"review_token": source["review_token"], "edits": edits},
    )
    assert result.status_code == 201, result.content
    values = result.json()["snapshot"]["values"]
    assert [row["evidence"] for row in values["sections"][0]["usages"]] == [
        attempt.candidate["sections"][0]["usages"][index]["evidence"] for index in (63, 60)
    ]
    assert [row["evidence"] for row in values["identifiers"]] == [
        attempt.candidate["identifiers"][index]["evidence"] for index in (62, 60)
    ]
    assert Usage.objects.count() == 2
    original = copy.deepcopy(attempt.candidate)
    attempt.refresh_from_db()
    assert attempt.candidate == original


@pytest.mark.parametrize("indexes", [[0, 0], [59], [-1], [True], ["0"], [0.0]])
def test_invalid_review_source_indexes_do_not_change_approved_rows(api_client, indexes):
    client, _, _, _ = api_client
    house, vendor = setup(client)
    document, _ = approve_manual(client, house, vendor)
    source = client.get(document["links"]["review"]).json()
    edits = source["edits"]
    edits["usages"] = [{**edits["usages"][0], "source_index": value} for value in indexes]
    result = send(
        client,
        "post",
        f"/documents/{document['id']}/reviews",
        {"review_token": source["review_token"], "edits": edits},
    )
    assert result.status_code == 422, result.content
    assert ReviewRevision.objects.count() == 1 and Usage.objects.count() == 1


def test_committed_deletion_returns_pending_cleanup_and_recovery_finishes(api_client, monkeypatch):
    import bills.deleting as deleting

    client, _, _, _ = api_client
    house, vendor = setup(client)
    document, _ = approve_manual(client, house, vendor)
    token = client.get(document["links"]["self"]).json()["deletion_token"]

    def interrupted(path):
        raise OSError("Synthetic interrupted cleanup")

    with monkeypatch.context() as patch:
        patch.setattr(deleting, "_finish_deletion", interrupted)
        result = client.delete(document["links"]["self"], HTTP_DELETION_TOKEN=token)
    assert result.status_code == 200 and result.json()["cleanup_pending"] is True
    assert not Document.objects.exists() and not Invoice.objects.exists()
    deleting.recover_document_deletions()
    assert client.get(document["links"]["self"]).status_code == 404
    assert client.delete(document["links"]["self"], HTTP_DELETION_TOKEN=token).status_code == 404


def test_manual_approval_correction_history_reporting_and_deletion(api_client):
    client, _, _, _ = api_client
    house, vendor = setup(client)
    document, review = approve_manual(client, house, vendor)
    assert client.get(review["links"]["self"]).json()["snapshot"] == review["snapshot"]
    assert send(client, "patch", review["links"]["self"].removeprefix(BASE), {}).status_code == 405
    method = send(client, "delete", review["links"]["self"].removeprefix(BASE))
    assert method.status_code == 405 and "GET" in method["Allow"]
    duplicate = send(client, "post", "/manual-invoices", {"idempotency_key": "synthetic-paper-1"})
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"]
    assert Document.objects.count() == 1
    source = client.get(document["links"]["review"]).json()
    source["edits"]["header"]["reference"] = "CORRECTED-1"
    payload = {"review_token": source["review_token"], "edits": source["edits"]}
    correction = send(client, "post", f"/documents/{document['id']}/reviews", payload)
    assert correction.status_code == 201, correction.content
    assert correction.json()["revision"] == 2
    assert send(client, "post", f"/documents/{document['id']}/reviews", payload).status_code == 409
    assert ReviewRevision.objects.count() == 2
    summary = client.get(BASE + "/reports/summary").json()
    assert summary["costs"][0]["gross_minor"] == 12500
    assert summary["consumption"][0]["quantity_milli"] == 12345
    assert summary["manual_invoices"] == 1
    series = client.get(BASE + "/reports/costs?mode=invoice_month").json()
    assert series["group_count"] == 1 and "next" not in series
    assert series["groups"][0]["invoice_month_time"] == 1767225600
    usage = client.get(BASE + "/reports/consumption?mode=billed_period").json()
    assert usage["groups"][0]["period_end"] == "2026-02-01"
    raw = client.get(BASE + "/reports/costs?limit=1").json()
    assert raw["count"] == 1 and raw["next"] is None
    for query in ("mode=invalid", "mode=totals&limit=1", "date_from=1900-01-01&date_to=2026-01-01"):
        assert client.get(BASE + "/reports/costs?" + query).status_code == 422
    assert send(client, "delete", f"/documents/{document['id']}").status_code == 400
    token = client.get(document["links"]["self"]).json()["deletion_token"]
    result = client.delete(document["links"]["self"], HTTP_DELETION_TOKEN=token)
    assert result.status_code == 200 and result.json() == {
        "document_id": document["id"],
        "deleted": True,
        "cleanup_pending": False,
    }
    assert client.get(document["links"]["self"]).status_code == 404


@pytest.mark.parametrize("value", [True, "10000", 10000.0, 2**63])
def test_scaled_json_integers_are_strict_and_bad_reviews_roll_back(api_client, value):
    client, _, _, _ = api_client
    house, vendor = setup(client)
    response = send(client, "post", "/manual-invoices", {})
    document = response.json()["document"]
    source = client.get(document["links"]["review"]).json()
    edits = manual_body(house, vendor)
    edits["header"]["net_minor"] = value
    response = send(
        client,
        "post",
        f"/documents/{document['id']}/reviews",
        {"review_token": source["review_token"], "edits": edits},
    )
    assert response.status_code == 422
    assert ReviewRevision.objects.count() == 0


@pytest.mark.parametrize("sort", ["--id", "--invoice_date", "id,--invoice_date"])
def test_invoice_sort_rejects_repeated_minus_signs(api_client, sort):
    client, _, _, _ = api_client
    client.raise_request_exception = False
    response = client.get(BASE + "/invoices", {"sort": sort})
    assert response.status_code == 422, response.content
    assert response.json()["error"]["code"] == "invalid_parameter"
    assert response.json()["error"]["details"][0]["path"] == "sort"


def test_original_download_uses_current_path_after_concurrent_publication(
    api_client, tmp_path, monkeypatch
):
    import bills.api.workflow as workflow
    import bills.services as services

    client, _, _, _ = api_client
    house, vendor = setup(client)
    pdf = write_pdf(tmp_path / "synthetic.pdf", [["Wholly synthetic original"]]).read_bytes()
    uploaded = client.post(BASE + "/documents", {"pdf": SimpleUploadedFile("synthetic.pdf", pdf)})
    assert uploaded.status_code == 201, uploaded.content
    document = uploaded.json()["document"]
    source = client.get(document["links"]["review"]).json()
    edits = manual_body(house, vendor)
    edits["warnings_confirmed"] = True
    # Keep approval committed while original publication is still pending.
    with monkeypatch.context() as patch:
        patch.setattr(services, "publish_original", lambda key: None)
        approved = send(
            client,
            "post",
            f"/documents/{document['id']}/reviews",
            {"review_token": source["review_token"], "edits": edits},
        )
    assert approved.status_code == 201, approved.content
    old_path = Document.objects.get(pk=document["id"]).original_path
    assert old_path.startswith("inbox/")
    open_current_original = workflow.open_original

    def publish_from_other_connection():
        try:
            published = publish_original(document["id"])
            assert published.storage_state == Document.StorageState.APPROVED
            assert published.original_path != old_path
            assert not private_path(old_path).exists()
            assert private_path(published.original_path).read_bytes() == pdf
        finally:
            connections.close_all()

    def open_after_publication(key):
        # Publish after the endpoint's first document read, before it opens the file.
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(publish_from_other_connection).result(timeout=10)
        return open_current_original(key)

    monkeypatch.setattr(workflow, "open_original", open_after_publication)
    response = client.get(document["links"]["source"])
    assert response.status_code == 200, response.content
    assert response["Cache-Control"] == "private, no-store"
    assert b"".join(response.streaming_content) == pdf


def test_upload_review_evidence_equivalence_reordering_and_reparse(api_client, tmp_path):
    client, owner, _, _ = api_client
    house, vendor = setup(client)
    other = created(client, "/properties", {"name": "Other house", "slug": "other-house"})
    created(
        client, f"/vendors/{vendor['id']}/identifiers", {"kind": "vat", "value": "SE000000999901"}
    )
    send(client, "put", f"/vendors/{vendor['id']}/invoice-formats/layout_02")
    for index, property_id in ((1, house["id"]), (2, other["id"])):
        created(
            client,
            f"/vendors/{vendor['id']}/property-rules",
            {"property_id": property_id, "kind": "installation", "value": f"{index:018}"},
        )
    pages = copy.deepcopy(shared_supply())
    pages[0].append("VAT-nr SE000000999901")
    pdf = write_pdf(tmp_path / "synthetic.pdf", pages).read_bytes()
    response = client.post(BASE + "/documents", {"pdf": SimpleUploadedFile("synthetic.pdf", pdf)})
    assert response.status_code == 201, response.content
    document = response.json()["document"]
    duplicate = client.post(BASE + "/documents", {"pdf": SimpleUploadedFile("renamed.pdf", pdf)})
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"]
    source = client.get(document["links"]["review"]).json()
    edits = copy.deepcopy(source["edits"])
    edits["warnings_confirmed"] = True
    context = current_review_context(Document.objects.get(pk=document["id"]))
    origin = context.source()
    client.force_login(owner)
    html = client.get(f"/documents/{document['id']}/review/")
    forms = ReviewForms(
        **{key: origin[key] for key in ("values", "properties", "vendor_id", "kind", "warnings")},
        token=context.token(),
        data=posted_fields(html),
    )
    assert forms.is_valid(), forms.header.errors
    html_candidate, html_properties = forms.candidate()
    candidate, properties = build_review(
        ReviewEdits(**ReviewEditsIn.model_validate(edits).model_dump()),
        values=origin["values"],
        warnings=origin["warnings"],
    )
    assert candidate.to_dict() == html_candidate.to_dict()
    assert properties == html_properties
    edits["usages"].reverse()
    edits["identifiers"].reverse()
    result = send(
        client,
        "post",
        f"/documents/{document['id']}/reviews",
        {"review_token": source["review_token"], "edits": edits},
    )
    assert result.status_code == 201, result.content
    for section in result.json()["snapshot"]["values"]["sections"]:
        original_section = next(
            item
            for item in candidate.to_dict()["sections"]
            if item["section_id"] == section["section_id"]
        )
        assert section["usages"] == original_section["usages"]
    original = client.get(document["links"]["source"])
    assert original["Cache-Control"] == "private, no-store"
    assert b"".join(original.streaming_content) == pdf
    reparse = send(
        client,
        "post",
        f"/documents/{document['id']}/reparse",
        {"extraction_revision": document["extraction_revision"]},
    )
    assert reparse.status_code == 200
    assert ReviewRevision.objects.count() == 1
    assert (
        send(
            client,
            "post",
            f"/documents/{document['id']}/reparse",
            {"extraction_revision": document["extraction_revision"]},
        ).status_code
        == 409
    )
    stale_delete = client.delete(
        document["links"]["self"], HTTP_DELETION_TOKEN=document["deletion_token"]
    )
    assert stale_delete.status_code == 409


def test_upload_is_bounded_and_authorized_before_parsing(api_client, settings, monkeypatch):
    client, _, _, _ = api_client
    settings.MAX_PDF_UPLOAD_BYTES = 20
    response = client.post(
        BASE + "/documents", {"pdf": SimpleUploadedFile("large.pdf", b"%PDF-" + b"x" * 30)}
    )
    assert response.status_code == 413 and response.json()["error"]["code"] == "body_too_large"
    assert Document.objects.count() == 0
    from bills.uploads import BoundedPdfUploadHandler

    def parsed(*args, **kwargs):
        raise AssertionError("Unauthorized uploads must not be parsed")

    monkeypatch.setattr(BoundedPdfUploadHandler, "new_file", parsed)
    anonymous = Client().post(
        BASE + "/documents", {"pdf": SimpleUploadedFile("large.pdf", b"%PDF-" + b"x" * 30)}
    )
    assert anonymous.status_code == 401 and anonymous["WWW-Authenticate"] == "Bearer"
