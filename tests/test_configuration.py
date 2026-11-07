import pytest
from django.contrib.auth.models import Permission
from django.test import Client

from bills.models import Property, PropertyIdentifier, Vendor

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_user(django_user_model):
    return django_user_model.objects.create_superuser(
        "synthetic-admin", password="synthetic-password"
    )


def test_configuration_requires_staff_and_model_permissions(client, django_user_model):
    path = "/admin/bills/property/"
    assert client.get(path).status_code == 302
    ordinary = django_user_model.objects.create_user("synthetic-ordinary")
    client.force_login(ordinary)
    assert client.get(path).status_code == 302
    staff = django_user_model.objects.create_user("synthetic-staff", is_staff=True)
    client.force_login(staff)
    assert client.get(path).status_code == 403
    staff.user_permissions.add(Permission.objects.get(codename="view_property"))
    assert client.get(path).status_code == 200
    assert client.get("/admin/bills/property/add/").status_code == 403


def test_houses_can_be_configured_with_csrf_protection(admin_user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(admin_user)
    path = "/admin/bills/property/add/"
    assert (
        client.post(path, {"name": "Synthetic House", "slug": "synthetic-house"}).status_code == 403
    )
    assert client.get(path).status_code == 200
    response = client.post(
        path,
        {
            "name": "Synthetic House",
            "slug": "synthetic-house",
            "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
            "_save": "Save",
        },
    )
    assert response.status_code == 302
    assert Property.objects.get().slug == "synthetic-house"
    assert b"Synthetic House" in client.get("/admin/bills/property/").content
    assert b"Administration" in client.get("/invoices/").content


def test_vendor_configuration_saves_identifiers_layouts_and_both_houses(client, admin_user):
    houses = [
        Property.objects.create(name=f"Synthetic House {i}", slug=f"house-{i}") for i in (1, 2)
    ]
    client.force_login(admin_user)
    data = {
        "name": "Synthetic Utilities",
        "slug": "synthetic-utilities",
        "default_property": houses[0].pk,
        "_save": "Save",
        "issuer_identifiers-TOTAL_FORMS": 1,
        "issuer_identifiers-INITIAL_FORMS": 0,
        "issuer_identifiers-MIN_NUM_FORMS": 0,
        "issuer_identifiers-MAX_NUM_FORMS": 1000,
        "issuer_identifiers-0-kind": "vat",
        "issuer_identifiers-0-value": "se 000-000-999",
        "layout_families-TOTAL_FORMS": 2,
        "layout_families-INITIAL_FORMS": 0,
        "layout_families-MIN_NUM_FORMS": 0,
        "layout_families-MAX_NUM_FORMS": 1000,
        "layout_families-0-family": "layout_01",
        "layout_families-1-family": "layout_02",
        "property_identifiers-TOTAL_FORMS": 2,
        "property_identifiers-INITIAL_FORMS": 0,
        "property_identifiers-MIN_NUM_FORMS": 0,
        "property_identifiers-MAX_NUM_FORMS": 1000,
        "property_identifiers-0-kind": "installation",
        "property_identifiers-0-value": "000 001",
        "property_identifiers-0-property": houses[0].pk,
        "property_identifiers-1-kind": "installation",
        "property_identifiers-1-value": "000 002",
        "property_identifiers-1-property": houses[1].pk,
    }
    path = "/admin/bills/vendor/add/"
    response = client.post(path, data)
    assert response.status_code == 302, (
        response.context["adminform"].form.errors
        if response.status_code == 200
        else response.content
    )
    vendor = Vendor.objects.get()
    assert vendor.default_property == houses[0]
    assert vendor.issuer_identifiers.get().value == "SE000000999"
    assert set(vendor.layout_families.values_list("family", flat=True)) == {
        "layout_01",
        "layout_02",
    }
    assert set(vendor.property_identifiers.values_list("property_id", flat=True)) == {
        house.pk for house in houses
    }
    assert b"000001" in client.get(f"/admin/bills/vendor/{vendor.pk}/change/").content
    response = client.get("/admin/bills/vendor/")
    assert response.status_code == 200 and b"Synthetic House 1" in response.content


def test_configuration_errors_do_not_write_invalid_mapping(client, admin_user):
    house = Property.objects.create(name="Synthetic House", slug="house")
    vendor = Vendor.objects.create(name="Synthetic Utilities", slug="vendor")
    client.force_login(admin_user)
    response = client.post(
        "/admin/bills/propertyidentifier/add/",
        {
            "vendor": vendor.pk,
            "property": house.pk,
            "kind": "customer_account",
            "value": "9900",
            "_save": "Save",
        },
    )
    assert response.status_code == 200
    assert response.context["adminform"].form.errors["kind"]
    assert PropertyIdentifier.objects.count() == 0


def test_normalized_duplicate_inline_keys_show_form_errors(client, admin_user):
    client.force_login(admin_user)
    response = client.post(
        "/admin/bills/vendor/add/",
        {
            "name": "Synthetic Utilities",
            "slug": "vendor",
            "_save": "Save",
            "issuer_identifiers-TOTAL_FORMS": 2,
            "issuer_identifiers-INITIAL_FORMS": 0,
            "issuer_identifiers-MIN_NUM_FORMS": 0,
            "issuer_identifiers-MAX_NUM_FORMS": 1000,
            "issuer_identifiers-0-kind": "vat",
            "issuer_identifiers-0-value": "se 000-999",
            "issuer_identifiers-1-kind": "vat",
            "issuer_identifiers-1-value": "SE000999",
            "layout_families-TOTAL_FORMS": 0,
            "layout_families-INITIAL_FORMS": 0,
            "layout_families-MIN_NUM_FORMS": 0,
            "layout_families-MAX_NUM_FORMS": 1000,
            "property_identifiers-TOTAL_FORMS": 0,
            "property_identifiers-INITIAL_FORMS": 0,
            "property_identifiers-MIN_NUM_FORMS": 0,
            "property_identifiers-MAX_NUM_FORMS": 1000,
        },
    )
    assert response.status_code == 200
    assert response.context["inline_admin_formsets"][0].formset.non_form_errors()
    assert Vendor.objects.count() == 0


def test_deleting_a_specific_mapping_preserves_the_property_and_vendor_default(client, admin_user):
    house = Property.objects.create(name="Synthetic House", slug="house")
    vendor = Vendor.objects.create(name="Synthetic Vendor", slug="vendor", default_property=house)
    mapping = PropertyIdentifier.objects.create(
        vendor=vendor,
        property=house,
        kind="installation",
        value="000001",
    )
    client.force_login(admin_user)
    path = f"/admin/bills/vendor/{vendor.pk}/change/"
    response = client.get(path)
    assert response.status_code == 200
    assert b'id="delete_id_default_property"' not in response.content
    assert b'id="delete_id_property_identifiers-0-property"' not in response.content
    assert b'name="property_identifiers-0-DELETE"' in response.content
    response = client.get(f"/admin/bills/propertyidentifier/{mapping.pk}/change/")
    assert response.status_code == 200
    assert b'id="delete_id_property"' not in response.content
    response = client.post(
        path,
        {
            "name": vendor.name,
            "slug": vendor.slug,
            "default_property": house.pk,
            "_save": "Save",
            "issuer_identifiers-TOTAL_FORMS": 0,
            "issuer_identifiers-INITIAL_FORMS": 0,
            "issuer_identifiers-MIN_NUM_FORMS": 0,
            "issuer_identifiers-MAX_NUM_FORMS": 1000,
            "layout_families-TOTAL_FORMS": 0,
            "layout_families-INITIAL_FORMS": 0,
            "layout_families-MIN_NUM_FORMS": 0,
            "layout_families-MAX_NUM_FORMS": 1000,
            "property_identifiers-TOTAL_FORMS": 1,
            "property_identifiers-INITIAL_FORMS": 1,
            "property_identifiers-MIN_NUM_FORMS": 0,
            "property_identifiers-MAX_NUM_FORMS": 1000,
            "property_identifiers-0-id": mapping.pk,
            "property_identifiers-0-vendor": vendor.pk,
            "property_identifiers-0-kind": mapping.kind,
            "property_identifiers-0-value": mapping.value,
            "property_identifiers-0-property": house.pk,
            "property_identifiers-0-DELETE": "on",
        },
    )
    assert response.status_code == 302
    assert not PropertyIdentifier.objects.filter(pk=mapping.pk).exists()
    assert Property.objects.filter(pk=house.pk).exists()
    vendor.refresh_from_db()
    assert vendor.default_property_id == house.pk
