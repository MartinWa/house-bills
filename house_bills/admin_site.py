"""Group billing configuration by ownership while retaining Django's permissions."""

from django.contrib.admin import AdminSite
from django.contrib.admin.apps import AdminConfig

BILLING_GROUPS = (
    (
        "Property configuration",
        ("Property", "PropertyService", "ServiceVendorAssignment", "ServiceCostSchedule"),
    ),
    ("Vendor configuration", ("Vendor", "VendorBillingSchedule", "InvoicePropertyRule")),
    ("Classification", ("Subcategory",)),
    ("Invoice follow-up", ("ServiceInvoiceException",)),
)


class HouseBillsAdminSite(AdminSite):
    def get_app_list(self, request, app_label=None):
        apps = super().get_app_list(request, app_label)
        for app in apps:
            if app["app_label"] != "bills":
                continue
            remaining = {model["object_name"]: model for model in app["models"]}
            groups = []
            for name, model_names in BILLING_GROUPS:
                models = [remaining.pop(key) for key in model_names if key in remaining]
                if models:
                    groups.append({"name": name, "models": models})
            if remaining:
                groups.append({"name": "Other configuration", "models": list(remaining.values())})
            app["model_groups"] = groups
            app["models"] = [model for group in groups for model in group["models"]]
        return apps


class HouseBillsAdminConfig(AdminConfig):
    default_site = "house_bills.admin_site.HouseBillsAdminSite"
