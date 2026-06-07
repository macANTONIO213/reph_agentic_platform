"""
GovernanceService unit tests.

Run with:  python manage.py test controlplane.tests.test_governance
"""
from datetime import timedelta

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.utils import timezone

from controlplane.models import Agent, Approval, AuditLog, GovernanceReview
from controlplane.services.governance import (
    GovernanceService,
    RegistrationError,
    TransitionError,
    governance,
)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_user(username="builder", is_staff=False):
    u, _ = User.objects.get_or_create(username=username)
    u.is_staff = is_staff
    u.set_unusable_password()
    u.save(update_fields=["is_staff", "password"])
    return u


def _add_group(user, name):
    g, _ = Group.objects.get_or_create(name=name)
    user.groups.add(g)
    return user


def _minimal_data(**overrides):
    base = {
        "name": "Test Agent",
        "platform": Agent.Platform.DJANGO,
        "owner": "Test Owner",
        "technical_owner": "Tech Owner",
        "purpose": "Testing GovernanceService.",
        "system_prompt": "You are a test agent.",
        "risk_tier": 1,
    }
    base.update(overrides)
    return base


# ── Registration tests ───────────────────────────────────────────────────────

class RegisterAgentTests(TestCase):
    def setUp(self):
        # Staff flag bypasses role gate — these tests exercise registration logic,
        # not the role gate (which is covered in test_phase_b.py).
        self.actor = _make_user("builder", is_staff=True)

    def test_creates_agent_in_draft(self):
        agent = governance.register_agent(actor=self.actor, data=_minimal_data())
        self.assertEqual(agent.status, Agent.Status.DRAFT)

    def test_writes_audit_log(self):
        agent = governance.register_agent(actor=self.actor, data=_minimal_data())
        log = AuditLog.objects.filter(action="agent.registered", resource_id=str(agent.id))
        self.assertTrue(log.exists())
        self.assertEqual(log.first().actor, self.actor.username)

    def test_missing_required_field_raises(self):
        data = _minimal_data()
        del data["name"]
        with self.assertRaises(RegistrationError):
            governance.register_agent(actor=self.actor, data=data)

    def test_invalid_platform_raises(self):
        with self.assertRaises(RegistrationError):
            governance.register_agent(actor=self.actor, data=_minimal_data(platform="nonsense"))

    def test_invalid_risk_tier_raises(self):
        with self.assertRaises(RegistrationError):
            governance.register_agent(actor=self.actor, data=_minimal_data(risk_tier=9))

    def test_disallowed_model_id_raises(self):
        with self.assertRaises(RegistrationError):
            governance.register_agent(
                actor=self.actor,
                data=_minimal_data(model_id="gpt-99-turbo-secret")
            )

    def test_allowed_model_id_accepted(self):
        agent = governance.register_agent(
            actor=self.actor,
            data=_minimal_data(model_id="claude-opus-4-8")
        )
        self.assertEqual(agent.model_id, "claude-opus-4-8")

    def test_slug_auto_generated(self):
        agent = governance.register_agent(actor=self.actor, data=_minimal_data(name="My Great Agent"))
        self.assertIn("my-great-agent", agent.slug)

    def test_duplicate_slug_gets_suffix(self):
        governance.register_agent(actor=self.actor, data=_minimal_data(name="Clash"))
        agent2 = governance.register_agent(actor=self.actor, data=_minimal_data(name="Clash"))
        self.assertNotEqual(agent2.slug, "clash")


# ── Transition tests ─────────────────────────────────────────────────────────

class TransitionTests(TestCase):
    def setUp(self):
        self.actor = _make_user("platform_admin", is_staff=True)
        self.agent = governance.register_agent(
            actor=self.actor, data=_minimal_data()
        )

    def _promote_to_pilot(self):
        governance.transition(actor=self.actor, agent=self.agent, to_status="review")
        governance.transition(actor=self.actor, agent=self.agent, to_status="pilot")

    def _add_approved_review(self):
        GovernanceReview.objects.create(
            agent=self.agent,
            reviewer=self.actor.username,
            status=GovernanceReview.Status.APPROVED,
        )

    def _add_valid_approval(self):
        Approval.objects.create(
            agent=self.agent,
            approved_by=self.actor,
            approved_by_username=self.actor.username,
            scope="tier4_execution",
            expires_at=timezone.now() + timedelta(hours=8),
        )

    def test_draft_to_review_succeeds(self):
        governance.transition(actor=self.actor, agent=self.agent, to_status="review")
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.status, "review")

    def test_illegal_transition_raises(self):
        with self.assertRaises(TransitionError):
            governance.transition(actor=self.actor, agent=self.agent, to_status="production")

    def test_transition_writes_audit_log(self):
        governance.transition(actor=self.actor, agent=self.agent, to_status="review")
        self.assertTrue(
            AuditLog.objects.filter(
                action="agent.transition",
                resource_id=str(self.agent.id)
            ).exists()
        )

    def test_promote_to_production_requires_governance_review(self):
        self._promote_to_pilot()
        self._add_valid_approval()
        # No GovernanceReview → must fail
        with self.assertRaises(TransitionError):
            governance.transition(actor=self.actor, agent=self.agent, to_status="production")

    def test_promote_to_production_requires_valid_approval(self):
        self._promote_to_pilot()
        self._add_approved_review()
        # No Approval → must fail
        with self.assertRaises(TransitionError):
            governance.transition(actor=self.actor, agent=self.agent, to_status="production")

    def test_promote_to_production_succeeds_with_both_gates(self):
        self._promote_to_pilot()
        self._add_approved_review()
        self._add_valid_approval()
        governance.transition(actor=self.actor, agent=self.agent, to_status="production")
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.status, "production")

    def test_bypass_skips_gates(self):
        self._promote_to_pilot()
        # No review, no approval — but bypass=True (staff break-glass)
        governance.transition(
            actor=self.actor, agent=self.agent, to_status="production",
            bypass=True, reason="incident response"
        )
        self.agent.refresh_from_db()
        self.assertEqual(self.agent.status, "production")

    def test_bypass_writes_forced_audit_log(self):
        self._promote_to_pilot()
        governance.transition(
            actor=self.actor, agent=self.agent, to_status="production",
            bypass=True, reason="drill"
        )
        log = AuditLog.objects.filter(
            action="agent.transition.forced",
            resource_id=str(self.agent.id)
        )
        self.assertTrue(log.exists())
        self.assertIn("FORCED", log.first().payload)

    def test_expired_approval_does_not_satisfy_gate(self):
        self._promote_to_pilot()
        self._add_approved_review()
        # Expired approval
        Approval.objects.create(
            agent=self.agent,
            approved_by=self.actor,
            approved_by_username=self.actor.username,
            scope="tier4_execution",
            expires_at=timezone.now() - timedelta(hours=1),
        )
        with self.assertRaises(TransitionError):
            governance.transition(actor=self.actor, agent=self.agent, to_status="production")


# ── record_approval RBAC test ────────────────────────────────────────────────

class RecordApprovalTests(TestCase):
    def setUp(self):
        self.actor = _make_user("builder", is_staff=True)
        self.approver = _add_group(_make_user("approver"), "agent_approver")
        self.agent = governance.register_agent(
            actor=self.actor, data=_minimal_data()
        )

    def test_non_approver_raises_permission_error(self):
        # Use a plain non-staff user without any approver role/group
        plain_user = _make_user("plain_non_approver", is_staff=False)
        with self.assertRaises(PermissionError):
            governance.record_approval(actor=plain_user, agent=self.agent)

    def test_approver_can_record_approval(self):
        approval = governance.record_approval(actor=self.approver, agent=self.agent)
        self.assertFalse(approval.is_consumed)
        self.assertTrue(approval.is_valid)

    def test_approval_writes_audit_log(self):
        governance.record_approval(actor=self.approver, agent=self.agent)
        self.assertTrue(
            AuditLog.objects.filter(action="agent.approved", resource_type="Approval").exists()
        )
