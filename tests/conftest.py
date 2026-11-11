"""Transaction tests flush data migrations; restore the initial classification catalogue."""

import pytest


@pytest.fixture(autouse=True)
def classification_catalogue(request):
    if request.node.get_closest_marker("django_db") or any(
        name in request.fixturenames for name in ("db", "transactional_db", "admin_user")
    ):
        request.getfixturevalue("db")
        from importlib import import_module

        from bills.models import Subcategory

        initial = import_module("bills.migrations.0019_shared_classification")
        for key, category, name in initial.INITIAL_SUBCATEGORIES:
            Subcategory.objects.get_or_create(pk=key, defaults={"category": category, "name": name})
