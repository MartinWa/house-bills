"""django-axes locks one username and connection address after repeated wrong passwords."""

import pytest
from axes.models import AccessAttempt, AccessFailureLog, AccessLog
from axes.utils import reset
from django.test import override_settings

pytestmark = pytest.mark.django_db

AXES_FAILURE_LIMIT = 3
WRONG_PASSWORD = "synthetic-wrong-password"
CORRECT_PASSWORD = "synthetic-password"
VISITOR_ADDRESS = {"REMOTE_ADDR": "198.51.100.7"}
OTHER_ADDRESS = {"REMOTE_ADDR": "198.51.100.8"}


@pytest.fixture(autouse=True)
def empty_attempt_history():
    reset()
    yield
    reset()


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(
        "synthetic-user", password=CORRECT_PASSWORD, is_staff=True
    )


@pytest.fixture
def other_user(django_user_model):
    return django_user_model.objects.create_user(
        "synthetic-other", password=CORRECT_PASSWORD, is_staff=True
    )


def sign_in(client, path, username, password, **meta):
    return client.post(path, {"username": username, "password": password}, **meta)


@pytest.mark.parametrize("path", ["/login/", "/admin/login/"])
@override_settings(AXES_ENABLED=True, AXES_FAILURE_LIMIT=AXES_FAILURE_LIMIT)
def test_repeated_wrong_passwords_lock_the_account_against_the_correct_one(client, user, path):
    for _ in range(AXES_FAILURE_LIMIT - 1):
        assert sign_in(client, path, user.username, WRONG_PASSWORD).status_code == 200
    blocked = sign_in(client, path, user.username, WRONG_PASSWORD)
    assert blocked.status_code == 429
    response = sign_in(client, path, user.username, CORRECT_PASSWORD)
    assert response.status_code == 429
    assert client.get("/").status_code == 302


@pytest.mark.parametrize("path", ["/login/", "/admin/login/"])
@override_settings(AXES_ENABLED=True, AXES_FAILURE_LIMIT=AXES_FAILURE_LIMIT)
def test_lockout_covers_one_username_and_address_pair(client, user, other_user, path):
    signed_in = sign_in(client, path, user.username, CORRECT_PASSWORD, **VISITOR_ADDRESS)
    assert signed_in.status_code == 302
    for _ in range(AXES_FAILURE_LIMIT):
        sign_in(client, path, user.username, WRONG_PASSWORD, **VISITOR_ADDRESS)
    locked = sign_in(client, path, user.username, CORRECT_PASSWORD, **VISITOR_ADDRESS)
    assert locked.status_code == 429
    visitor = sign_in(client, path, other_user.username, CORRECT_PASSWORD, **VISITOR_ADDRESS)
    assert visitor.status_code == 302
    elsewhere = sign_in(client, path, user.username, CORRECT_PASSWORD, **OTHER_ADDRESS)
    assert elsewhere.status_code == 302


@pytest.mark.parametrize("path", ["/login/", "/admin/login/"])
@override_settings(AXES_ENABLED=True, AXES_FAILURE_LIMIT=AXES_FAILURE_LIMIT)
def test_a_blocked_retry_does_not_extend_the_lockout(client, user, path):
    for _ in range(AXES_FAILURE_LIMIT):
        sign_in(client, path, user.username, WRONG_PASSWORD, **VISITOR_ADDRESS)
    attempt = AccessAttempt.objects.get(username=user.username)
    locked = (attempt.attempt_time, attempt.failures_since_start)

    blocked = sign_in(client, path, user.username, CORRECT_PASSWORD, **VISITOR_ADDRESS)

    assert blocked.status_code == 429
    attempt.refresh_from_db()
    assert (attempt.attempt_time, attempt.failures_since_start) == locked


@override_settings(AXES_ENABLED=True)
def test_sign_in_activity_is_not_written_to_the_axes_log_tables(client, user):
    assert sign_in(client, "/login/", user.username, CORRECT_PASSWORD).status_code == 302
    sign_in(client, "/login/", user.username, WRONG_PASSWORD, **VISITOR_ADDRESS)

    assert AccessLog.objects.count() == 0
    assert AccessFailureLog.objects.count() == 0
