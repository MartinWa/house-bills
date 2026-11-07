from django.urls import path

from . import views

app_name = "bills"
urlpatterns = [
    path("", views.home, name="home"),
    path("upload/", views.upload, name="upload"),
    path("documents/", views.documents, name="documents"),
    path("documents/<int:pk>/", views.document, name="document"),
    path("documents/<int:pk>/source/", views.source, name="source"),
    path("documents/<int:pk>/retry/", views.retry_storage, name="retry_storage"),
]
