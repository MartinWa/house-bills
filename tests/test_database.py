import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from django.db import connection, transaction


@pytest.mark.django_db(transaction=True)
def test_connections_use_file_backed_wal_full_sync_timeout_and_immediate():
    connection.ensure_connection()
    assert connection.settings_dict["NAME"] != ":memory:"
    assert connection.settings_dict["ATOMIC_REQUESTS"] is False
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode")
        assert cursor.fetchone() == ("wal",)
        cursor.execute("PRAGMA synchronous")
        assert cursor.fetchone() == (2,)
        cursor.execute("PRAGMA busy_timeout")
        assert cursor.fetchone() == (10_000,)
    statements = []
    connection.connection.set_trace_callback(statements.append)
    try:
        with transaction.atomic():
            pass
    finally:
        connection.connection.set_trace_callback(None)
    assert "BEGIN IMMEDIATE" in statements
    assert "COMMIT" in statements


@pytest.mark.django_db(transaction=True)
def test_reader_remains_available_during_a_write_and_sees_commit(django_user_model):
    user = django_user_model.objects.create_user("synthetic-reader")
    database = connection.settings_dict["NAME"]
    with sqlite3.connect(database, timeout=10) as reader:
        with transaction.atomic():
            django_user_model.objects.filter(pk=user.pk).update(first_name="Updated")
            assert reader.execute(
                "SELECT first_name FROM auth_user WHERE id = ?", (user.pk,)
            ).fetchone() == ("",)
        assert reader.execute(
            "SELECT first_name FROM auth_user WHERE id = ?", (user.pk,)
        ).fetchone() == ("Updated",)


@pytest.mark.django_db(transaction=True)
def test_competing_immediate_writer_waits_then_preserves_both_writes(django_user_model):
    user = django_user_model.objects.create_user("synthetic-writer")
    database = connection.settings_dict["NAME"]
    started = Event()

    def second_write():
        with sqlite3.connect(database, timeout=10) as writer:
            started.set()
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE auth_user SET last_name = ? WHERE id = ?", ("Second", user.pk))

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            django_user_model.objects.filter(pk=user.pk).update(first_name="First")
            future = pool.submit(second_write)
            assert started.wait(timeout=2)
            assert not future.done()
        future.result(timeout=5)
    user.refresh_from_db()
    assert (user.first_name, user.last_name) == ("First", "Second")
