"""
Load & soak harness (SC-4 start).

Usage (locust is a dev-only dependency, not in requirements.txt):

    pip install locust
    locust -f scripts/loadtest_locustfile.py --host https://<env> \
           -u 50 -r 5 --run-time 10m --headless \
           LOADTEST_USER=<username> LOADTEST_PASSWORD=<password> as env vars

Asserts nothing itself — compare the resulting p50/p95/p99 against the
PLATFORM_SLO_* targets (see /api/v1/platform/readiness/).
"""
import os

from locust import HttpUser, between, task


class PlatformUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        # Session login (CSRF dance) once per simulated user.
        r = self.client.get("/accounts/login/")
        token = r.cookies.get("csrftoken", "")
        self.client.post(
            "/accounts/login/",
            data={
                "username": os.environ.get("LOADTEST_USER", "loadtest"),
                "password": os.environ.get("LOADTEST_PASSWORD", ""),
                "csrfmiddlewaretoken": token,
            },
            headers={"Referer": self.host or ""},
        )

    @task(4)
    def monitoring_summary(self):
        self.client.get("/api/v1/monitoring/summary/")

    @task(3)
    def agents_list(self):
        self.client.get("/api/v1/agents/")

    @task(2)
    def costs(self):
        self.client.get("/api/v1/costs/summary/")

    @task(1)
    def registry(self):
        self.client.get("/api/v1/registry/")
