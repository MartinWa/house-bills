import pytest
from django.contrib.auth.models import Permission
from django.test import Client

from bills.models import (
    Property,
    PropertyIdentifier,
    PropertyService,
    ServiceVendorAssignment,
    Vendor,
)

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


def property_service_data(house, vendors):
    data = {
        "property": house.pk,
        "service": "waste",
        "subcategory": "waste_collection",
        "is_active": "on",
        "start_month": "2024-02",
        "end_month": "2024-11",
        "frequency_months": 3,
        "grace_days": 0,
        "_save": "Save",
        "vendor_assignments-TOTAL_FORMS": len(vendors),
        "vendor_assignments-INITIAL_FORMS": 0,
        "vendor_assignments-MIN_NUM_FORMS": 0,
        "vendor_assignments-MAX_NUM_FORMS": 1000,
        "invoice_exceptions-TOTAL_FORMS": 0,
        "invoice_exceptions-INITIAL_FORMS": 0,
        "invoice_exceptions-MIN_NUM_FORMS": 0,
        "invoice_exceptions-MAX_NUM_FORMS": 1000,
    }
    for i, vendor in enumerate(vendors):
        data.update(
            {
                f"vendor_assignments-{i}-vendor": vendor.pk,
                f"vendor_assignments-{i}-is_active": "on",
                f"vendor_assignments-{i}-start_month": "2024-02",
                f"vendor_assignments-{i}-end_month": "2024-11",
            }
        )
    return data


def test_property_services_attach_a_vendor_to_both_properties(client, admin_user):
    from datetime import date

    houses = [
        Property.objects.create(name=f"Synthetic House {i}", slug=f"house-{i}") for i in (1, 2)
    ]
    vendor = Vendor.objects.create(name="Synthetic Scheduled Vendor", slug="scheduled-vendor")
    client.force_login(admin_user)
    for house in houses:
        response = client.post(
            "/admin/bills/propertyservice/add/", property_service_data(house, [vendor])
        )
        assert response.status_code == 302
    assert vendor.service_assignments.count() == PropertyService.objects.count() == 2
    assert set(PropertyService.objects.values_list("start_month", flat=True)) == {date(2024, 2, 1)}
    page = client.get(f"/admin/bills/vendor/{vendor.pk}/change/")
    assert b"Configure services and vendor assignments" in page.content
    page = client.get(f"/admin/bills/property/{houses[0].pk}/change/")
    assert b"Property services" in page.content and b"Waste collection" in page.content
    configured = PropertyService.objects.first()
    page = client.get(f"/admin/bills/propertyservice/{configured.pk}/change/")
    assert b'type="month"' in page.content and b"Vendor assignments" in page.content
    assert b'id="add_id_property"' not in page.content


def test_service_editor_can_save_with_view_only_vendor_assignments(client, django_user_model):
    from datetime import date

    house = Property.objects.create(name="Synthetic House", slug="house")
    vendor = Vendor.objects.create(name="Synthetic Vendor", slug="vendor")
    configured = PropertyService.objects.create(
        property=house,
        service="waste",
        subcategory="waste_collection",
        start_month=date(2024, 2, 1),
        end_month=date(2024, 11, 1),
        frequency_months=3,
    )
    assignment = ServiceVendorAssignment.objects.create(
        property_service=configured,
        vendor=vendor,
        start_month=configured.start_month,
        end_month=configured.end_month,
    )
    before = ServiceVendorAssignment.objects.values().get(pk=assignment.pk)
    staff = django_user_model.objects.create_user("synthetic-service-editor", is_staff=True)
    staff.user_permissions.add(
        *Permission.objects.filter(
            content_type__app_label="bills",
            codename__in=["change_propertyservice", "view_servicevendorassignment"],
        )
    )
    client.force_login(staff)
    path = f"/admin/bills/propertyservice/{configured.pk}/change/"
    page = client.get(path)
    assert page.status_code == 200
    assert not page.context["inline_admin_formsets"][0].has_change_permission
    data = property_service_data(house, [])
    data.update(
        {
            "grace_days": 2,
            "vendor_assignments-TOTAL_FORMS": 1,
            "vendor_assignments-INITIAL_FORMS": 1,
            "vendor_assignments-0-id": assignment.pk,
        }
    )
    response = client.post(path, data)
    assert response.status_code == 302
    configured.refresh_from_db()
    assert configured.grace_days == 2
    assert ServiceVendorAssignment.objects.count() == 1
    assert ServiceVendorAssignment.objects.values().get(pk=assignment.pk) == before


def test_overlapping_vendor_assignments_show_errors_and_write_nothing(client, admin_user):
    house = Property.objects.create(name="Synthetic House", slug="house")
    vendor = Vendor.objects.create(name="Synthetic Vendor", slug="vendor")
    client.force_login(admin_user)
    data = property_service_data(house, [vendor, vendor])
    data["vendor_assignments-1-start_month"] = "2024-05"
    response = client.post("/admin/bills/propertyservice/add/", data)
    assert response.status_code == 200
    inline = next(
        row
        for row in response.context["inline_admin_formsets"]
        if row.formset.prefix == "vendor_assignments"
    )
    assert "must not overlap" in str(inline.formset.non_form_errors())
    assert not PropertyService.objects.exists() and not ServiceVendorAssignment.objects.exists()


def test_vendor_transition_and_as_needed_tank_emptying_can_be_configured(client, admin_user):
    house = Property.objects.create(name="Synthetic House", slug="house")
    first = Vendor.objects.create(name="Synthetic First Vendor", slug="first")
    second = Vendor.objects.create(name="Synthetic Second Vendor", slug="second")
    client.force_login(admin_user)
    data = property_service_data(house, [first, second])
    data.update(
        {
            "vendor_assignments-0-end_month": "2024-05",
            "vendor_assignments-1-start_month": "2024-05",
        }
    )
    assert client.post("/admin/bills/propertyservice/add/", data).status_code == 302
    tank = property_service_data(house, [second])
    tank.update({"subcategory": "tank_emptying", "frequency_months": 0})
    assert client.post("/admin/bills/propertyservice/add/", tank).status_code == 302
    assert PropertyService.objects.count() == 2 and ServiceVendorAssignment.objects.count() == 3


def test_enabled_service_requires_a_vendor(client, admin_user):
    house = Property.objects.create(name="Synthetic House", slug="house")
    client.force_login(admin_user)
    response = client.post("/admin/bills/propertyservice/add/", property_service_data(house, []))
    assert response.status_code == 200
    inline = response.context["inline_admin_formsets"][0]
    assert "Attach at least one enabled vendor" in str(inline.formset.non_form_errors())
    assert not PropertyService.objects.exists()


@pytest.mark.parametrize("month,valid", [("2024-05", True), ("2024-03", False)])
def test_confirmed_no_invoice_month_can_be_configured_with_a_new_service(
    client, admin_user, month, valid
):
    from bills.models import ServiceInvoiceException

    house = Property.objects.create(name="Synthetic House", slug="house")
    vendor = Vendor.objects.create(name="Synthetic Vendor", slug="vendor")
    client.force_login(admin_user)
    data = property_service_data(house, [vendor])
    data.update(
        {
            "invoice_exceptions-TOTAL_FORMS": 1,
            "invoice_exceptions-0-vendor": vendor.pk,
            "invoice_exceptions-0-month": month,
            "invoice_exceptions-0-reason": "Synthetic vendor confirmed no invoice this month.",
        }
    )
    response = client.post("/admin/bills/propertyservice/add/", data)
    if valid:
        assert response.status_code == 302
        exception = ServiceInvoiceException.objects.get()
        assert exception.month.isoformat() == "2024-05-01"
        assert exception.property_service.vendor_assignments.get().vendor == exception.vendor
    else:
        assert response.status_code == 200
        assert b"Choose a recurring invoice month" in response.content
        assert not PropertyService.objects.exists() and not ServiceInvoiceException.objects.exists()


def test_duplicate_property_services_are_rejected(client, admin_user):
    house = Property.objects.create(name="Synthetic House", slug="house")
    vendor = Vendor.objects.create(name="Synthetic Vendor", slug="vendor")
    client.force_login(admin_user)
    data = property_service_data(house, [vendor])
    assert client.post("/admin/bills/propertyservice/add/", data).status_code == 302
    data["start_month"] = data["vendor_assignments-0-start_month"] = "2024-05"
    response = client.post("/admin/bills/propertyservice/add/", data)
    assert response.status_code == 200
    assert "already has an enabled service" in str(response.context["adminform"].form.errors)
    assert PropertyService.objects.count() == 1


def test_vendor_assignment_must_fit_the_service_months(client, admin_user):
    house = Property.objects.create(name="Synthetic House", slug="house")
    vendor = Vendor.objects.create(name="Synthetic Vendor", slug="vendor")
    client.force_login(admin_user)
    data = property_service_data(house, [vendor])
    data["vendor_assignments-0-start_month"] = "2024-01"
    response = client.post("/admin/bills/propertyservice/add/", data)
    assert response.status_code == 200
    assert "within the service's months" in str(
        response.context["inline_admin_formsets"][0].formset.non_form_errors()[0]
    )
    assert not PropertyService.objects.exists()
