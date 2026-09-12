from django.contrib import admin
from django.urls import path
from vps.views import dashboard, vps_list


urlpatterns = [
    path("", dashboard, name="dashboard"),
    path("admin/", admin.site.urls),
    path("vps/", vps_list, name="vps_list"),
]