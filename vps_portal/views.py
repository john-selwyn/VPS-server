from django.shortcuts import render
from vps.models import VPS


def dashboard(request):
    vps_list = VPS.objects.all()

    return render(request, "dashboard.html", {
        "vps_list": vps_list,
    })


def vps_list(request):
    vps = VPS.objects.all()

    return render(request, "vps/list.html", {
        "vps": vps,
    })