from django.contrib import admin
from django.contrib.auth.views import LoginView, LogoutView
from django.urls import include, path

from bills.api.auth import PREFIX
from bills.api.router import api

urlpatterns = [
    path("login/", LoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("admin/", admin.site.urls),
    path(f"{PREFIX.lstrip('/')}/", api.urls),
    path("", include("bills.urls")),
]
