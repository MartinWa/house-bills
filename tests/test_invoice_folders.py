from datetime import date
from urllib.parse import parse_qs

import pytest
from django.test import Client

from bills.models import Document
from bills.storage import private_path

from . import test_reporting, test_review
from .test_reporting import allocation
from .test_review import approved, review_data, upload

ledger = test_reporting.ledger
configured = test_review.configured

pytestmark = pytest.mark.django_db(transaction=True)


def test_folder_view_counts_shared_original_once_and_preserves_source(configured, client, tmp_path):
    document, original = upload(client, tmp_path)
    invoice = approved(client, document)
    duplicate, _ = upload(client, tmp_path, filename="duplicate.pdf")
    assert duplicate.pk == document.pk
    document.refresh_from_db()
    path = document.original_path

    response = client.get("/invoices/", {"view": "folders"})
    assert response.status_code == 200
    assert response.context["pdf_count"] == 1 and response.context["manual_count"] == 0
    assert response.context["folders"][0]["name"] == "Shared"
    assert response.context["folders"][0]["count"] == 1
    assert response.context["folders"][0]["children"][0]["name"] == "Synthetic Utilities"
    page = response.content.decode()
    assert page.count(f'href="/invoices/{invoice.pk}/"') == 1
    assert page.count(f'href="/documents/{document.pk}/source/"') == 1
    assert path.rsplit("/", 1)[-1] in page
    assert path not in page and "View PDF" not in page and "File details" not in page
    assert "inbox/" not in page and ".staging" not in page
    document.refresh_from_db()
    assert document.original_path == path and private_path(path).read_bytes() == original


def test_folder_view_follows_published_corrections(configured, client, tmp_path):
    document, original = upload(client, tmp_path)
    invoice = approved(client, document)
    path, data = review_data(client, document)
    data["sections-1-property"] = str(configured[2][0].pk)
    data["sections-1-service"] = "electricity_network"
    data["invoice_date"] = "2025-03-05"
    data["due_date"] = ""
    response = client.post(path, data)
    assert response.status_code == 302, response.content.decode()

    response = client.get("/invoices/", {"view": "folders"})
    folder = response.context["folders"][0]
    assert folder["name"] == "House 1"
    assert folder["children"][0]["children"][0]["name"] == "2025"
    assert f'href="/invoices/{invoice.pk}/"' in response.content.decode()
    document.refresh_from_db()
    assert document.original_path.startswith("house-1/synthetic-utilities/2025/")
    assert private_path(document.original_path).read_bytes() == original


def test_manual_folder_counts_cover_all_pages_and_keep_years_ordered(ledger, client):
    client.force_login(ledger["actor"])
    invoices = [
        ledger["make"](
            [allocation(ledger["houses"][0], service="water")],
            source=Document.Source.MANUAL,
            invoice_date=date(2024 if index else 2023, 3, 5),
        )
        for index in range(51)
    ]
    assert client.get("/invoices/").context["page"].paginator.num_pages == 2
    response = client.get("/invoices/", {"view": "folders", "page": 2})
    assert response.status_code == 200
    assert response.context["pdf_count"] == 0 and response.context["manual_count"] == 51
    root = response.context["folders"][0]
    assert root["name"] == "Manual entries" and root["count"] == 51
    years = root["children"][0]["children"][0]["children"]
    assert [year["name"] for year in years] == ["2024", "2023"]
    page = response.content.decode()
    for invoice in invoices:
        assert f'href="/invoices/{invoice.pk}/"' in page
    assert "/view/" not in page and "/source/" not in page
    assert 'aria-label="Pagination"' not in page


def test_folder_filters_preserve_view_and_join_the_same_allocation(ledger, client):
    client.force_login(ledger["actor"])
    first, second = ledger["houses"]
    invoice = ledger["make"](
        [allocation(first, service="water"), allocation(second, service="waste")],
        source=Document.Source.MANUAL,
    )
    query = {
        "view": "folders",
        "property": first.pk,
        "service": "water",
        "date_from": "2024-03-05",
        "date_to": "2024-03-05",
        "page": 3,
    }
    response = client.get("/invoices/", query)
    assert response.context["manual_count"] == 1
    assert f'href="/invoices/{invoice.pk}/"' in response.content.decode()
    assert response.context["folders"][0]["children"][0]["name"] == "Shared"
    assert 'name="view" value="folders"' in response.content.decode()
    assert response.context["clear_view_url"] == "/invoices/?view=folders"
    for view, key in (("list", "list_query"), ("folders", "folders_query")):
        params = parse_qs(response.context[key])
        assert params["view"] == [view] and "page" not in params
        assert params["property"] == [str(first.pk)] and params["service"] == ["water"]

    for changes in (
        {"service": "waste"},
        {"date_from": "2024-03-06", "date_to": "2024-03-06"},
    ):
        response = client.get("/invoices/", {**query, **changes})
        assert response.context["manual_count"] == 0
        assert b"No approved invoices match" in response.content


@pytest.mark.parametrize(
    "filters",
    [
        {"property": "999999"},
        {"service": "unknown"},
        {"date_from": "2024-03-06", "date_to": "2024-03-05"},
    ],
)
def test_invalid_folder_filters_show_errors_and_no_records(configured, client, filters):
    response = client.get("/invoices/", {"view": "folders", **filters})
    assert response.status_code == 400 and response.context["form"].errors
    assert response.context["folders"] == []
    assert response.context["pdf_count"] == response.context["manual_count"] == 0


def test_folder_view_requires_login_and_unknown_view_falls_back_to_list(configured, client):
    response = Client().get("/invoices/?view=folders")
    assert response.status_code == 302 and response.url.startswith("/login/")
    response = client.get("/invoices/", {"view": "unknown"})
    assert response.status_code == 200 and response.context["invoice_view"] == "list"
