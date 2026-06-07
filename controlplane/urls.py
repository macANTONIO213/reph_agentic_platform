from django.urls import path

from . import views

app_name = "controlplane"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("manage/", views.manage_panel, name="manage_panel"),
    path("api/agents/<uuid:agent_id>/run/", views.run_agent, name="run_agent"),
    path("api/runs/<uuid:run_id>/feedback/", views.submit_feedback, name="submit_feedback"),
    path("api/telemetry/", views.telemetry_feed, name="telemetry_feed"),
    path("api/monitoring/", views.monitoring_data, name="monitoring_data"),
    path("api/org/children/", views.org_children, name="org_children"),
]
