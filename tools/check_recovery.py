"""Verify authenticated HTTP and original bytes in the disposable restored production instance."""

import hashlib
from http.cookies import SimpleCookie
from urllib.request import Request, build_opener

import django
from django.conf import settings

from .check_http import NoRedirect, get


def main():
    django.setup()
    from django.contrib.auth import get_user_model
    from django.test import Client

    from bills.models import Document, Invoice
    from bills.storage import private_path

    assert Invoice.objects.filter(status="approved").count() == 2
    user = get_user_model().objects.get(username="synthetic-container-uploader")
    client = Client()
    client.force_login(user)
    cookie = SimpleCookie()
    cookie[settings.SESSION_COOKIE_NAME] = client.cookies[settings.SESSION_COOKIE_NAME].value
    headers = {
        "Host": "ci.invalid",
        "X-Forwarded-Proto": "https",
        "Cookie": cookie.output(header="", sep=";").strip(),
    }
    with build_opener(NoRedirect).open(
        Request("http://127.0.0.1:8000/reports/", headers=headers), timeout=5
    ) as response:
        page = response.read()
        assert response.status == 200 and b"100,000 kWh" in page and b"-1,250 m" in page
    for document in Document.objects.all():
        payload = private_path(document.original_path).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == document.sha256
        with build_opener(NoRedirect).open(
            Request(f"http://127.0.0.1:8000/documents/{document.pk}/source/", headers=headers),
            timeout=5,
        ) as response:
            assert response.status == 200 and response.read() == payload
        with get(f"/documents/{document.pk}/source/", host="ci.invalid", secure=True) as response:
            assert response.status == 302 and response.headers["Location"].startswith("/login/")
    print("Restored production reports, approvals, corrections and private source access passed.")


if __name__ == "__main__":
    main()
