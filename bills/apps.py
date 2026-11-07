from django.apps import AppConfig


class BillsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "bills"

    def ready(self):
        from . import checks  # noqa: F401
