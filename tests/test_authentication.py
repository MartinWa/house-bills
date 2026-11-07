import pytest
from django.test import Client

pytestmark = pytest.mark.django_db


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user("synthetic-user", password="synthetic-password")


def test_anonymous_pages_require_login_and_login_is_public(client):
    response = client.get("/")
    assert response.status_code == 302
    assert response.url == "/login/?next=/"
    response = client.get("/admin/")
    assert response.status_code == 302
    assert response.url.startswith("/admin/login/")
    response = client.get("/login/")
    assert response.status_code == 200
    assert b"csrfmiddlewaretoken" in response.content


def test_login_and_post_logout_require_csrf_and_change_access(user):
    client = Client(enforce_csrf_checks=True)
    client.get("/login/")
    credentials = {"username": user.username, "password": "synthetic-password"}
    assert client.post("/login/", credentials).status_code == 403
    response = client.post(
        "/login/", {**credentials, "csrfmiddlewaretoken": client.cookies["csrftoken"].value}
    )
    assert response.status_code == 302 and response.url == "/"
    response = client.get("/")
    assert response.status_code == 200
    assert user.username.encode() in response.content
    assert "no-store" in response.headers["Cache-Control"]
    assert client.get("/logout/").status_code == 405
    assert client.post("/logout/").status_code == 403
    response = client.post("/logout/", {"csrfmiddlewaretoken": client.cookies["csrftoken"].value})
    assert response.status_code == 302 and response.url == "/login/"
    assert client.get("/").status_code == 302


def test_bad_credentials_and_external_next_cannot_grant_access(client, user):
    response = client.post("/login/", {"username": user.username, "password": "wrong"})
    assert response.status_code == 200
    assert response.context["form"].errors
    assert client.get("/").status_code == 302
    response = client.post(
        "/login/",
        {
            "username": user.username,
            "password": "synthetic-password",
            "next": "https://other.invalid/",
        },
    )
    assert response.status_code == 302 and response.url == "/"


def test_private_storage_has_no_public_route(client, user):
    client.force_login(user)
    assert client.get("/private-files/synthetic.pdf").status_code == 404
