"""Smoke test: the dashboard template renders, including the Agent Fabric UI."""
from django.contrib.auth.models import User
from django.test import TestCase


class DashboardRenderTests(TestCase):
    def setUp(self):
        self.user = User.objects.get_or_create(username="dash", defaults={"is_staff": True})[0]
        self.client.force_login(self.user)

    def test_dashboard_renders_with_fabric_ui(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        # nav + section + each sub-panel present
        self.assertIn('data-view-tab="fabric"', html)
        self.assertIn('data-view-panel="fabric"', html)
        for el in ("fabRegistryTbody", "fabScannersPanel", "fabBrokerPanel", "fabVizPanel"):
            self.assertIn(el, html)
