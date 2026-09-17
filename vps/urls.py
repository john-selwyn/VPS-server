from django.urls import path
from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),

    path(
        "order/",
        views.order_vps,
        name="order_vps",
    ),

    path(
        "provisioning/<int:vps_id>/",
        views.provisioning,
        name="provisioning",
    ),

    path(
        "provisioning/<int:vps_id>/status/",
        views.provisioning_status,
        name="provisioning_status",
    ),
]