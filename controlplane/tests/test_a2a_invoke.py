"""
Phase 1 step 5 tests — A2A inbound invocation (the G2 gate).

Proves that an external A2A ``message/send`` runs the agent through
``PlatformAgentRuntime`` (real embedded Echo agent → an AgentRun is created and
audited) and that a runtime failure maps to an A2A *failed task*, never a raw
reply.  There is no path that reaches an adapter without the governed runtime.
"""
import json
from unittest.mock import patch

from django.test import TestCase, override_settings

from controlplane.models import Agent, AgentRun, AsyncAgentTask, AuditLog, BusinessUnit
from controlplane.services.interop.a2a_cards import publish_card


def _bu(name="Engineering"):
    return BusinessUnit.objects.get_or_create(name=name, defaults={"code": name[:8].upper()})[0]


def _agent(slug="echo-agent", bu=None, guardrail="off"):
    return Agent.objects.create(
        name=slug.replace("-", " ").title(), slug=slug, purpose="Echoes input",
        business_unit=(bu.name if bu else "Engineering"), owner="o", technical_owner="o",
        system_prompt="s", platform="embedded", status=Agent.Status.PILOT, risk_tier=1,
        org_unit=bu, guardrail_level=guardrail,
    )


def _msg(text, rpc_id=1, context_id=""):
    message = {"role": "user", "parts": [{"kind": "text", "text": text}]}
    if context_id:
        message["contextId"] = context_id
    return {"jsonrpc": "2.0", "id": rpc_id, "method": "message/send", "params": {"message": message}}


def _error_stream(self, message, session=None):
    yield f"event: error\ndata: {json.dumps({'message': 'Guardrail blocked'})}\n\n"


@override_settings(A2A_SERVER_ENABLED=True, A2A_ACCESS_TOKENS=["tok-123"])
class A2AInvokeTests(TestCase):
    AUTH = {"HTTP_AUTHORIZATION": "Bearer tok-123"}

    def setUp(self):
        self.bu = _bu()
        self.agent = _agent(bu=self.bu)
        publish_card(self.agent, base_url="https://platform.test")

    def _rpc(self, body):
        return self.client.post(
            f"/a2a/agents/{self.agent.slug}/rpc/",
            data=json.dumps(body), content_type="application/json", **self.AUTH,
        )

    def test_message_send_runs_through_runtime(self):
        resp = self._rpc(_msg("hello world"))
        self.assertEqual(resp.status_code, 200)
        result = resp.json()["result"]
        self.assertEqual(result["kind"], "task")
        self.assertEqual(result["status"]["state"], "completed")
        self.assertTrue(result["artifacts"][0]["parts"][0]["text"])
        # Proof it went through the governed runtime, not a shortcut:
        self.assertTrue(AgentRun.objects.filter(agent=self.agent).exists())
        self.assertTrue(
            AuditLog.objects.filter(action="a2a_inbound_invoke", resource_id=str(self.agent.id)).exists()
        )

    def test_runtime_failure_maps_to_failed_task(self):
        with patch("controlplane.services.agent_runtime.PlatformAgentRuntime.stream", new=_error_stream):
            resp = self._rpc(_msg("poisoned"))
        self.assertEqual(resp.status_code, 200)
        result = resp.json()["result"]
        self.assertEqual(result["status"]["state"], "failed")
        self.assertNotIn("artifacts", result)  # no raw reply leaked
        self.assertIn("Guardrail blocked", result["status"]["message"]["parts"][0]["text"])

    def test_tasks_get_polls_task(self):
        send = self._rpc(_msg("ping"))
        task_id = send.json()["result"]["id"]
        get = self._rpc({"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": task_id}})
        self.assertEqual(get.status_code, 200)
        self.assertEqual(get.json()["result"]["id"], task_id)

    def test_context_id_propagated(self):
        resp = self._rpc(_msg("hi", context_id="ctx-9"))
        task_id = resp.json()["result"]["id"]
        self.assertEqual(AsyncAgentTask.objects.get(id=task_id).context_id, "ctx-9")

    def test_unpublished_agent_not_invocable(self):
        hidden = _agent(slug="hidden", bu=self.bu)  # no card
        resp = self.client.post(
            f"/a2a/agents/{hidden.slug}/rpc/",
            data=json.dumps(_msg("hi")), content_type="application/json", **self.AUTH,
        )
        self.assertEqual(resp.status_code, 404)

    def test_missing_text_is_invalid_params(self):
        body = {"jsonrpc": "2.0", "id": 3, "method": "message/send",
                "params": {"message": {"role": "user", "parts": []}}}
        resp = self._rpc(body)
        self.assertEqual(resp.json()["error"]["code"], -32602)

    def test_unknown_method(self):
        resp = self._rpc({"jsonrpc": "2.0", "id": 4, "method": "tasks/cancel", "params": {}})
        self.assertEqual(resp.json()["error"]["code"], -32601)

    def test_auth_required(self):
        resp = self.client.post(
            f"/a2a/agents/{self.agent.slug}/rpc/",
            data=json.dumps(_msg("hi")), content_type="application/json",
        )
        self.assertEqual(resp.status_code, 401)
