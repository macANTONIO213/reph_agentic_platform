"""
Phase B3 + B4 tests — Eval Gate & Quality Drift Alerting.

Run with:  python manage.py test controlplane.tests.test_phase_b34
"""
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from controlplane.models import (
    Agent, AgentFeedback, Approval, EvalCase, EvalRun, EvalSuite,
    GovernanceReview, UserProfile,
)
from controlplane.services.governance import GovernanceService, TransitionError
from controlplane.services.eval_service import EvalService

governance = GovernanceService()
eval_svc   = EvalService()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_agent(name="Test Agent", status="pilot"):
    slug = name.lower().replace(" ", "-")
    return Agent.objects.create(
        name=name, slug=slug, platform="custom_api",
        owner="owner@test.com", purpose="Testing",
        system_prompt="You are a test agent.",
        status=status,
    )


def _make_staff():
    u, _ = User.objects.get_or_create(username="staff_b34", defaults={"is_staff": True})
    u.is_staff = True
    u.save()
    return u


def _approve_review(agent, actor):
    review = GovernanceReview.objects.create(
        agent=agent,
        status=GovernanceReview.Status.APPROVED,
        reviewer=actor.username,
    )
    return review


def _grant_approval(agent, actor):
    from datetime import timedelta
    return Approval.objects.create(
        agent=agent, approved_by=actor,
        approved_by_username=actor.username,
        scope="tier4_execution",
        expires_at=timezone.now() + timedelta(hours=8),
    )


# ── B3: Eval Gate ─────────────────────────────────────────────────────────────

class EvalGateTests(TestCase):

    def setUp(self):
        self.actor = _make_staff()
        self.agent = _make_agent("Eval Gate Agent")

    def _standard_gates(self):
        """Satisfy governance review + approval gates."""
        _approve_review(self.agent, self.actor)
        _grant_approval(self.agent, self.actor)

    # ── Suite model ───────────────────────────────────────────────────────────

    def test_create_suite_and_cases(self):
        suite = EvalSuite.objects.create(
            agent=self.agent, name="Smoke Tests", pass_threshold=80,
        )
        EvalCase.objects.create(
            suite=suite, name="Greet test",
            input_message="Hello",
            expected_keywords=["hello", "hi"],
        )
        self.assertEqual(suite.cases.count(), 1)

    def test_latest_passing_run_none_when_no_runs(self):
        suite = EvalSuite.objects.create(agent=self.agent, name="Empty Suite")
        self.assertIsNone(suite.latest_passing_run)

    # ── Production gate ───────────────────────────────────────────────────────

    def test_no_suite_allows_promotion(self):
        """Agents without a suite can still be promoted (suite is optional)."""
        self._standard_gates()
        governance.transition(
            actor=self.actor, agent=self.agent,
            to_status="production",
        )
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.status, "production")

    def test_suite_with_no_runs_blocks_promotion(self):
        self._standard_gates()
        EvalSuite.objects.create(
            agent=self.agent, name="Mandatory Suite",
            is_active=True, pass_threshold=80,
        )
        with self.assertRaises(TransitionError) as ctx:
            governance.transition(
                actor=self.actor, agent=self.agent,
                to_status="production",
            )
        self.assertIn("no passing run", str(ctx.exception))

    def test_suite_with_failing_run_blocks_promotion(self):
        self._standard_gates()
        suite = EvalSuite.objects.create(
            agent=self.agent, name="Failing Suite",
            is_active=True, pass_threshold=80,
        )
        EvalRun.objects.create(
            suite=suite, status=EvalRun.Status.COMPLETE,
            total_cases=5, passed_cases=2,
            pass_rate=Decimal("40.00"), passed=False,
            triggered_by="test",
        )
        with self.assertRaises(TransitionError) as ctx:
            governance.transition(
                actor=self.actor, agent=self.agent,
                to_status="production",
            )
        self.assertIn("no passing run", str(ctx.exception))

    def test_suite_with_passing_run_allows_promotion(self):
        self._standard_gates()
        suite = EvalSuite.objects.create(
            agent=self.agent, name="Passing Suite",
            is_active=True, pass_threshold=80,
        )
        EvalRun.objects.create(
            suite=suite, status=EvalRun.Status.COMPLETE,
            total_cases=5, passed_cases=5,
            pass_rate=Decimal("100.00"), passed=True,
            triggered_by="test",
        )
        governance.transition(
            actor=self.actor, agent=self.agent,
            to_status="production",
        )
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.status, "production")

    def test_inactive_suite_not_checked_at_gate(self):
        """Inactive suites are ignored — only active suites are gated."""
        self._standard_gates()
        EvalSuite.objects.create(
            agent=self.agent, name="Inactive Suite",
            is_active=False, pass_threshold=80,
        )
        # No passing run, but suite is inactive → should promote fine
        governance.transition(
            actor=self.actor, agent=self.agent,
            to_status="production",
        )
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.status, "production")

    # ── EvalRun.compute_pass_rate ─────────────────────────────────────────────

    def test_compute_pass_rate_all_pass(self):
        suite = EvalSuite.objects.create(
            agent=self.agent, name="Rate Suite", pass_threshold=80,
        )
        run = EvalRun.objects.create(
            suite=suite, status=EvalRun.Status.COMPLETE,
            total_cases=3, triggered_by="test",
            case_results=[
                {"passed": True, "weight": 1},
                {"passed": True, "weight": 1},
                {"passed": True, "weight": 1},
            ],
        )
        run.compute_pass_rate()
        self.assertEqual(run.pass_rate, Decimal("100.00"))
        self.assertTrue(run.passed)

    def test_compute_pass_rate_partial(self):
        suite = EvalSuite.objects.create(
            agent=self.agent, name="Partial Suite", pass_threshold=80,
        )
        run = EvalRun.objects.create(
            suite=suite, status=EvalRun.Status.COMPLETE,
            total_cases=4, triggered_by="test",
            case_results=[
                {"passed": True,  "weight": 1},
                {"passed": True,  "weight": 1},
                {"passed": False, "weight": 1},
                {"passed": False, "weight": 1},
            ],
        )
        run.compute_pass_rate()
        self.assertEqual(run.pass_rate, Decimal("50.00"))
        self.assertFalse(run.passed)

    # ── EvalService._score ────────────────────────────────────────────────────

    def test_score_all_keywords_present(self):
        suite = EvalSuite.objects.create(agent=self.agent, name="Score Suite")
        case = EvalCase(
            suite=suite, name="kw test",
            input_message="hi",
            expected_keywords=["paris", "france"],
            must_not_contain=[],
        )
        passed, reasons = eval_svc._score(case, "paris is the capital of france", 100)
        self.assertTrue(passed)
        self.assertEqual(reasons, [])

    def test_score_missing_keyword_fails(self):
        suite = EvalSuite.objects.create(agent=self.agent, name="Score Suite 2")
        case = EvalCase(
            suite=suite, name="missing kw",
            input_message="hi",
            expected_keywords=["london"],
            must_not_contain=[],
        )
        passed, reasons = eval_svc._score(case, "paris is the capital of france", 100)
        self.assertFalse(passed)
        self.assertTrue(any("london" in r for r in reasons))

    def test_score_forbidden_term_fails(self):
        suite = EvalSuite.objects.create(agent=self.agent, name="Score Suite 3")
        case = EvalCase(
            suite=suite, name="forbidden",
            input_message="hi",
            expected_keywords=[],
            must_not_contain=["restricted"],
        )
        passed, reasons = eval_svc._score(case, "this is restricted content", 100)
        self.assertFalse(passed)

    def test_score_latency_exceeded_fails(self):
        suite = EvalSuite.objects.create(agent=self.agent, name="Score Suite 4")
        case = EvalCase(
            suite=suite, name="latency",
            input_message="hi",
            expected_keywords=[],
            must_not_contain=[],
            max_latency_ms=500,
        )
        passed, reasons = eval_svc._score(case, "ok", 1500)
        self.assertFalse(passed)
        self.assertTrue(any("Latency" in r for r in reasons))


# ── B4: Quality Drift Alerting ────────────────────────────────────────────────

class DriftAlertingTests(TestCase):

    def _make_feedback(self, agent, rating, days_ago=0):
        from datetime import timedelta
        from controlplane.models import AgentRun
        run = AgentRun.objects.create(
            agent=agent, user_label="test", channel="test",
            input_text="test", status=AgentRun.Status.COMPLETED,
        )
        fb = AgentFeedback.objects.create(
            run=run, rating=rating, submitted_by="test",
        )
        # Backdate both run and feedback
        ts = timezone.now() - timedelta(days=days_ago)
        AgentFeedback.objects.filter(pk=fb.pk).update(created_at=ts)
        AgentRun.objects.filter(pk=run.pk).update(started_at=ts)
        return fb

    def test_no_alert_when_quality_stable(self):
        """No drift when recent score equals baseline."""
        from django.core.management import call_command
        from io import StringIO

        agent = _make_agent("Stable Agent", status="production")

        # Seed 7 days of ratings: all 4.5
        for i in range(7):
            self._make_feedback(agent, 4.5, days_ago=i)

        out = StringIO()
        call_command("compute_baselines", stdout=out)

        agent.refresh_from_db()
        self.assertFalse(agent.quality_alert)

    def test_alert_raised_on_significant_drop(self):
        """Alert raised when recent score drops >1.0 below baseline."""
        from django.core.management import call_command
        from io import StringIO

        agent = _make_agent("Drifting Agent", status="production")

        # Baseline: days 3–7 → rating 4.5
        for i in range(3, 8):
            self._make_feedback(agent, 4.5, days_ago=i)

        # Recent (days 0–1) → rating 2.0 (drop of 2.5)
        for i in range(3):
            self._make_feedback(agent, 2.0, days_ago=0)

        out = StringIO()
        call_command("compute_baselines", stdout=out)

        agent.refresh_from_db()
        self.assertTrue(agent.quality_alert)

    def test_dry_run_does_not_write(self):
        """--dry-run flag does not persist changes."""
        from django.core.management import call_command
        from io import StringIO

        agent = _make_agent("Dry Run Agent", status="production")
        for i in range(3, 8):
            self._make_feedback(agent, 4.5, days_ago=i)
        for _ in range(3):
            self._make_feedback(agent, 1.0, days_ago=0)

        out = StringIO()
        call_command("compute_baselines", "--dry-run", stdout=out)

        agent.refresh_from_db()
        self.assertFalse(agent.quality_alert)  # unchanged
        self.assertIn("DRY RUN", out.getvalue())
