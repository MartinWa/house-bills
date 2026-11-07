from django.urls import path

from . import views

app_name = "bills"
urlpatterns = [path("", views.home, name="home")]
