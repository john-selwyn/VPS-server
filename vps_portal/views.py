from django.shortcuts import render

def dashboard(request):
    vps_list = [
        {"name": "VPS-001", "status": "Running", "os": "Ubuntu 26.04 LTS", "ip": "192.168.80.101", "cpu": 2, "ram": 4, "disk": 50},
        {"name": "VPS-002", "status": "Running", "os": "Ubuntu 26.04 LTS", "ip": "192.168.80.102", "cpu": 2, "ram": 4, "disk": 80},
        {"name": "VPS-003", "status": "Stopped", "os": "Debian 13", "ip": "192.168.80.103", "cpu": 2, "ram": 4, "disk": 50},
    ]
    context = {
        "customer_name": "John",
        "vps_list": vps_list,
        "active_vps": sum(v["status"] == "Running" for v in vps_list),
        "total_cpu": sum(v["cpu"] for v in vps_list),
        "total_ram": sum(v["ram"] for v in vps_list),
    }
    return render(request, "vps_portal/dashboard.html", context)
