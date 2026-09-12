from django.shortcuts import render, redirect
from .models import VPS


def dashboard(request):
    vps_list = VPS.objects.all()

    return render(request, "vps/dashboard.html", {
        "vps_list": vps_list,
    })


def vps_list(request):
    vps = VPS.objects.all()

    return render(request, "vps/list.html", {
        "vps": vps,
    })


def order_vps(request):
    if request.method == "POST":

        # If coming from the billing page, simulate payment
        if request.POST.get("payment") == "success":

            VPS.objects.create(
                name=request.POST.get("name"),
                ip_address="192.168.80.100",
                status="Running",
                plan=request.POST.get("ram") + "GB VPS"
            )

            return redirect("dashboard")

        return render(request, "vps/billing.html", {
            "name": request.POST.get("name"),
            "cpu": request.POST.get("cpu"),
            "ram": request.POST.get("ram"),
            "storage": request.POST.get("storage"),
            "os": request.POST.get("os"),
            "billing": request.POST.get("billing"),
        })

    return render(request, "vps/order.html")