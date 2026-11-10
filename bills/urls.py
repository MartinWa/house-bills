from django.urls import path

from . import views

app_name = "bills"
urlpatterns = [
    path("", views.home, name="home"),
    path("release-status/", views.release_status, name="release-status"),
    path("upload/", views.upload, name="upload"),
    path("documents/", views.documents, name="documents"),
    path("documents/<int:pk>/", views.document, name="document"),
    path("documents/<int:pk>/delete/", views.delete_document, name="delete_document"),
    path("documents/<int:pk>/cancel/", views.cancel_manual_entry, name="cancel_manual_entry"),
    path("documents/<int:pk>/source/", views.source, name="source"),
    path("documents/<int:pk>/view/", views.source, {"download": False}, name="source_view"),
    path("documents/<int:pk>/retry/", views.retry_storage, name="retry_storage"),
    path("documents/<int:pk>/review/", views.review, name="review"),
    path("documents/<int:pk>/reparse/", views.reparse, name="reparse"),
    path("invoices/", views.invoices, name="invoices"),
    path("invoices/manual/", views.manual_invoice, name="manual_invoice"),
    path("invoices/<int:pk>/", views.invoice, name="invoice"),
    path("reports/", views.reports, name="reports"),
    path("invoices/coverage/", views.invoice_coverage, name="invoice_coverage"),
    path(
        "invoices/coverage/<int:property_service_id>/<int:vendor_id>/<str:month>/not-sent/",
        views.invoice_not_sent,
        name="invoice_not_sent",
    ),
]
