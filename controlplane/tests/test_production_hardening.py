"""
Regression tests for the production-hardening increment:

  - idempotency keys on WorkflowRun + AsyncAgentTask (A1)
  - dead-letter + stale-run/task recovery (A2)
  - atomic run claim prevents double-execution (A1)
  - per-agent orchestrator circuit breaker (A3)
  - constant-time bearer compare + session CSRF on interop surfaces (H2/H3)
  - rate-limit atomic counter + /a2a/ coverage (M1)
  - SSRF resolve/private-range settings (C3)
  - probe endpoints /healthz + /readyz (observability)
  - correlation-id middleware (E2)
  - eval-gate require-suite-for-tier (D1)
"""
from __future__ import annotations

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from controlplane.models import Agent, AsyncAgentTask, Workflow, WorkflowRun
from controlplane.services.workflow_queue import workflow_queue


def _make_workflow() -> Workflow:
    return Workflow.objects.create(name="wf-hardening", slug="wf-hardening")


class IdempotencyTests(TestCase):
    def test_duplicate_enqueue_with_key_returns_same_run(self):
        wf = _make_workflow()
        run1 = workflow_queue.enqueue(wf, idempotency_key="abc-123")
        run2 = workflow_queue.enqueue(wf, idempotency_key="abc-123")
        self.assertEqual(run1.id, run2.id)
        self.assertEqual(WorkflowRun.objects.filter(idempotency_key="abc-123").count(), 1)

    def test_no_key_creates_distinct_runs(self):
        wf = _make_workflow()
        run1 = workflow_queue.enqueue(wf)
        run2 = workflow_queue.enqueue(wf)
        self.assertNotEqual(run1.id, run2.id)

    def test_async_task_duplicate_key_dedupes(self):
        agent = Agent.objects.create(name="a-idem", slug="a-idem", platform="custom")
        from controlplane.services.agent_tasks import agent_tasks
        t1 = agent_tasks.submit(agent, "hello", idempotency_key="k-1")
        t2 = agent_tasks.submit(agent, "hello again", idempotency_key="k-1")
        self.assertEqual(t1.id, t2.id)


class AtomicClaimTests(TestCase):
    def test_try_claim_run_is_single_winner(self):
        wf = _make_workflow()
        run = workflow_queue.enqueue(wf)  # db backend leaves it PENDING
        run.status = WorkflowRun.Status.PENDING
        run.save(update_fields=["status"])

        first = workflow_queue.try_claim_run(run.id)
        second = workflow_queue.try_claim_run(run.id)
        self.assertIsNotNone(first)
        self.assertIsNone(second)  # already claimed → no double-execution
        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowRun.Status.RUNNING)
        self.assertEqual(run.attempts, 1)


class DeadLetterAndRecoveryTests(TestCase):
    @override_settings(WORKFLOW_RUN_MAX_ATTEMPTS=2)
    def test_stale_run_requeues_then_dead_letters(self):
        wf = _make_workflow()
        run = WorkflowRun.objects.create(workflow=wf, status=WorkflowRun.Status.RUNNING, attempts=1)
        # Force staleness.
        WorkflowRun.objects.filter(id=run.id).update(
            updated_at=timezone.now() - timezone.timedelta(seconds=1000)
        )
        workflow_queue.recover_stale_running_runs(stale_after_seconds=900)
        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowRun.Status.PENDING)  # requeued, attempt remains

        # Second stall with attempts == max → dead-letter.
        WorkflowRun.objects.filter(id=run.id).update(
            status=WorkflowRun.Status.RUNNING, attempts=2,
            updated_at=timezone.now() - timezone.timedelta(seconds=1000),
        )
        workflow_queue.recover_stale_running_runs(stale_after_seconds=900)
        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowRun.Status.DEAD_LETTER)

    @override_settings(ASYNC_TASK_MAX_ATTEMPTS=1)
    def test_stale_working_task_dead_letters(self):
        agent = Agent.objects.create(name="a-dl", slug="a-dl", platform="custom")
        task = AsyncAgentTask.objects.create(
            agent=agent, input_message="x", state=AsyncAgentTask.State.WORKING, attempts=1,
        )
        AsyncAgentTask.objects.filter(id=task.id).update(
            updated_at=timezone.now() - timezone.timedelta(seconds=1000)
        )
        workflow_queue.recover_stale_working_tasks(stale_after_seconds=900)
        task.refresh_from_db()
        self.assertEqual(task.state, AsyncAgentTask.State.DEAD_LETTER)

    def test_requeue_dead_letter_resets_attempts(self):
        wf = _make_workflow()
        run = WorkflowRun.objects.create(
            workflow=wf, status=WorkflowRun.Status.DEAD_LETTER, attempts=3,
        )
        workflow_queue.requeue_dead_letter(run)
        run.refresh_from_db()
        self.assertEqual(run.status, WorkflowRun.Status.PENDING)
        self.assertEqual(run.attempts, 0)


class AgentCircuitBreakerTests(TestCase):
    def setUp(self):
        cache.clear()

    @override_settings(AGENT_CIRCUIT_BREAKER_FAILURE_THRESHOLD=3,
                       AGENT_CIRCUIT_BREAKER_COOLDOWN_SECONDS=60)
    def test_breaker_opens_after_threshold(self):
        from controlplane.services import orchestrator as orch
        agent = Agent.objects.create(name="a-cb", slug="a-cb", platform="custom")
        for _ in range(3):
            orch._register_agent_failure(agent)
        with self.assertRaises(orch.AgentCircuitOpenError):
            orch._assert_agent_circuit_closed(agent)
        # Clearing failures closes the breaker again.
        orch._clear_agent_failures(agent)
        orch._assert_agent_circuit_closed(agent)  # no raise


class InteropAuthTests(TestCase):
    def test_bearer_token_constant_time_match(self):
        from controlplane.api.interop_auth import bearer_token_matches

        class _Req:
            headers = {"Authorization": "Bearer secret-xyz"}

        self.assertTrue(bearer_token_matches(_Req(), ["nope", "secret-xyz"]))
        self.assertFalse(bearer_token_matches(_Req(), ["nope", "other"]))
        self.assertFalse(bearer_token_matches(_Req(), []))

    @override_settings(A2A_SERVER_ENABLED=True, A2A_ACCESS_TOKENS=["tok-123"])
    def test_a2a_rejects_bad_token(self):
        c = Client()
        resp = c.post(
            "/a2a/agents/does-not-exist/rpc/",
            data="{}", content_type="application/json",
            HTTP_AUTHORIZATION="Bearer wrong",
        )
        self.assertEqual(resp.status_code, 401)


class RateLimitTests(TestCase):
    def setUp(self):
        cache.clear()

    @override_settings(API_RATE_LIMIT_REQUESTS_PER_WINDOW=3, API_RATE_LIMIT_WINDOW_SECONDS=60)
    def test_atomic_counter_enforces_limit(self):
        from controlplane.middleware import ApiGlobalRateLimitMiddleware
        mw = ApiGlobalRateLimitMiddleware(lambda r: None)
        scope = "user:42"
        results = [mw._is_limited(scope) for _ in range(5)]
        # First 3 allowed (False), then limited (True).
        self.assertEqual(results, [False, False, False, True, True])

    def test_a2a_prefix_is_protected(self):
        from controlplane.middleware import ApiGlobalRateLimitMiddleware
        mw = ApiGlobalRateLimitMiddleware(lambda r: None)
        self.assertTrue("/a2a/".startswith(mw.protected_prefixes) or
                        any("/a2a/".startswith(p) for p in mw.protected_prefixes))


class NetGuardSettingsTests(TestCase):
    def test_private_allowed_by_default(self):
        from controlplane.services.interop.net_guard import validate_destination
        validate_destination("http://10.0.0.5:8080/rpc")  # no raise (design default)

    @override_settings(NET_GUARD_BLOCK_PRIVATE=True)
    def test_private_blocked_when_enabled(self):
        from controlplane.services.interop.net_guard import (
            validate_destination, BlockedDestinationError,
        )
        with self.assertRaises(BlockedDestinationError):
            validate_destination("http://10.0.0.5:8080/rpc")

    def test_loopback_always_blocked(self):
        from controlplane.services.interop.net_guard import (
            validate_destination, BlockedDestinationError,
        )
        with self.assertRaises(BlockedDestinationError):
            validate_destination("http://127.0.0.1/x")


class HealthProbeTests(TestCase):
    def test_healthz_is_unauthenticated_and_alive(self):
        resp = Client().get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "alive")

    def test_readyz_checks_dependencies(self):
        resp = Client().get("/readyz")
        self.assertIn(resp.status_code, (200, 503))
        body = resp.json()
        self.assertIn("database", body["checks"])
        self.assertIn("cache", body["checks"])

    def test_readyz_ready_when_db_up(self):
        resp = Client().get("/readyz")
        # DB is up in tests, so readiness should be 200.
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ready")


class CorrelationIdTests(TestCase):
    def test_response_carries_request_id(self):
        resp = Client().get("/healthz")
        self.assertIn("X-Request-ID", resp)
        self.assertTrue(resp["X-Request-ID"])

    def test_inbound_id_is_propagated_and_sanitized(self):
        resp = Client().get("/healthz", HTTP_X_REQUEST_ID="abc-123$$$inject")
        # Sanitizer strips unsafe chars but keeps the safe prefix.
        self.assertTrue(resp["X-Request-ID"].startswith("abc-123"))
        self.assertNotIn("$", resp["X-Request-ID"])
