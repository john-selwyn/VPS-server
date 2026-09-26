from django.urls import path
from . import views
from . import internal_api

urlpatterns = [
    path("api/internal/provision/", internal_api.provision, name="internal_provision"),
    path("api/internal/provision/<int:billing_order_id>/status/", internal_api.status, name="internal_provision_status"),
    path("api/internal/vps/<int:billing_order_id>/", internal_api.vps_state, name="internal_vps_state"),
    path("api/internal/v1/vps/<int:billing_order_id>/", internal_api.vps_state, name="internal_vps_state_v1"),
    path("api/internal/v1/vps/<int:billing_order_id>/power/", internal_api.vps_power, name="internal_vps_power"),
    path("api/internal/v1/vps/<int:billing_order_id>/power-operations/<uuid:operation_id>/", internal_api.power_operation, name="internal_power_operation"),
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
