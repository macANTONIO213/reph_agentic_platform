from datetime import date

from django.contrib.auth.models import Group, Permission, User
from django.core.management.base import BaseCommand
from django.utils import timezone

from controlplane.models import Agent, BusinessUnit, Division, GovernanceReview, OrgProcess, TelemetryEvent, WorkStream


class Command(BaseCommand):
    help = "Seed demo agents for the Agentic Platform."

    def handle(self, *args, **options):
        seed_agents = [
            {
                "slug": "agent-deployment-advisor",
                "name": "Agent Deployment Advisor",
                "platform": Agent.Platform.DJANGO,
                "business_unit": "Enterprise AI",
                "owner": "Agentic Platform Team",
                "technical_owner": "Platform Engineering",
                "purpose": "Guides teams through agent registration, risk tiering, deployment gates, and telemetry expectations.",
                "system_prompt": "You are a deployment advisor for the internal Agentic Platform.",
                "status": Agent.Status.PRODUCTION,
                "risk_tier": 2,
                "data_sources": ["Agent registry", "Governance controls", "Telemetry events"],
                "tool_names": ["registry_search", "risk_classifier", "deployment_gate_builder"],
                "monthly_active_users": 128,
                "monthly_runs": 460,
                "monthly_cost_usd": 92,
                "satisfaction_score": 4.7,
                "deployed_at": timezone.now(),
                "next_review_at": date(2026, 8, 2),
            },
            {
                "slug": "contract-review-assistant",
                "name": "Contract Review Assistant",
                "platform": Agent.Platform.COPILOT,
                "business_unit": "Legal",
                "owner": "Legal Operations",
                "technical_owner": "Enterprise Apps",
                "purpose": "Summarizes contracts, identifies risky clauses, and routes exceptions to legal reviewers.",
                "system_prompt": "Summarize legal documents and identify review exceptions.",
                "status": Agent.Status.PRODUCTION,
                "risk_tier": 3,
                "data_sources": ["Contract repository", "Legal playbooks", "SharePoint"],
                "tool_names": ["clause_extractor", "matter_intake", "review_queue"],
                "monthly_active_users": 420,
                "monthly_runs": 1880,
                "monthly_cost_usd": 1840,
                "satisfaction_score": 4.4,
                "deployed_at": timezone.now(),
                "next_review_at": date(2026, 8, 14),
            },
            {
                "slug": "support-case-summarizer",
                "name": "Support Case Summarizer",
                "platform": Agent.Platform.EMBEDDED,
                "business_unit": "Customer Ops",
                "owner": "Support Excellence",
                "technical_owner": "Service Engineering",
                "purpose": "Summarizes case history, highlights blockers, and suggests next best actions.",
                "system_prompt": "Summarize support cases and produce concise handoff notes.",
                "status": Agent.Status.PRODUCTION,
                "risk_tier": 2,
                "data_sources": ["Case management", "Knowledge articles", "Customer profile"],
                "tool_names": ["case_timeline", "knowledge_retrieval"],
                "monthly_active_users": 760,
                "monthly_runs": 3920,
                "monthly_cost_usd": 2360,
                "satisfaction_score": 4.6,
                "deployed_at": timezone.now(),
                "next_review_at": date(2026, 7, 30),
            },
            {
                "slug": "finance-variance-explainer",
                "name": "Finance Variance Explainer",
                "platform": Agent.Platform.AZURE_AI,
                "business_unit": "Finance",
                "owner": "FP&A",
                "technical_owner": "Analytics Engineering",
                "purpose": "Explains monthly budget variance using ledger data and management commentary.",
                "system_prompt": "Explain financial variance with concise management-ready language.",
                "status": Agent.Status.REVIEW,
                "risk_tier": 3,
                "data_sources": ["General ledger", "Forecast models", "Commentary archive"],
                "tool_names": ["variance_query", "narrative_builder"],
                "monthly_active_users": 0,
                "monthly_runs": 0,
                "monthly_cost_usd": 0,
                "satisfaction_score": 0,
                "next_review_at": date(2026, 6, 11),
            },
            {
                "slug": "incident-triage-agent",
                "name": "Incident Triage Agent",
                "platform": Agent.Platform.CUSTOM,
                "business_unit": "Technology",
                "owner": "Platform Reliability",
                "technical_owner": "SRE",
                "purpose": "Correlates alerts, recent deployments, and runbook steps during service incidents.",
                "system_prompt": "Assist incident commanders by correlating technical signals.",
                "status": Agent.Status.PILOT,
                "risk_tier": 4,
                "data_sources": ["Observability logs", "Deployment history", "Runbooks"],
                "tool_names": ["alert_lookup", "runbook_retrieval", "timeline_builder"],
                "monthly_active_users": 92,
                "monthly_runs": 310,
                "monthly_cost_usd": 680,
                "satisfaction_score": 3.9,
                "deployed_at": timezone.now(),
                "next_review_at": date(2026, 6, 18),
            },
        ]

        for data in seed_agents:
            slug = data["slug"]
            deployed_at = data.pop("deployed_at", None)
            defaults = {k: v for k, v in data.items() if k != "slug"}
            agent_obj, created = Agent.objects.update_or_create(slug=slug, defaults=defaults)
            # Only stamp deployed_at once — never overwrite an existing timestamp.
            if created and deployed_at and agent_obj.deployed_at is None:
                agent_obj.deployed_at = deployed_at
                agent_obj.save(update_fields=["deployed_at"])

        advisor = Agent.objects.get(slug="agent-deployment-advisor")
        if not TelemetryEvent.objects.filter(agent=advisor, event_type="agent_deployed", actor="seed_demo").exists():
            TelemetryEvent.objects.create(
                agent=advisor,
                event_type="agent_deployed",
                actor="seed_demo",
                business_unit=advisor.business_unit,
                payload={"status": advisor.status, "platform": advisor.platform},
            )

        self.stdout.write(self.style.SUCCESS(f"Seeded {len(seed_agents)} demo agents."))

        self._seed_org_hierarchy()
        self._seed_rbac_groups()
        self._seed_governance_for_production_agents()

    def _seed_org_hierarchy(self):
        # ── Business Units ────────────────────────────────────────────────────
        bu_data = [
            {"name": "LexisNexis", "code": "lnl", "description": "LexisNexis Legal & Professional"},
            {"name": "Elsevier", "code": "elsevier", "description": "Elsevier Research & Academic"},
            {"name": "LexisNexis Risk", "code": "lnr", "description": "LexisNexis Risk Solutions"},
            {"name": "Reed Elsevier", "code": "relx", "description": "Reed Elsevier Group (shared services & corporate)"},
        ]
        bus = {}
        for data in bu_data:
            obj, _ = BusinessUnit.objects.update_or_create(code=data["code"], defaults=data)
            bus[data["code"]] = obj

        # ── Divisions (shared across BUs — business_unit left null) ───────────
        div_data = [
            {"name": "CMO", "code": "cmo", "description": "Chief Marketing Officer"},
            {"name": "CSO", "code": "cso", "description": "Chief Strategy Officer"},
            {"name": "EdOps", "code": "edops", "description": "Editorial Operations"},
            {"name": "FAH", "code": "fah", "description": "Finance, Analytics & Helpdesk"},
            {"name": "HRSS", "code": "hrss", "description": "HR Shared Services"},
            {"name": "Sales", "code": "sales", "description": "Sales & Commercial"},
            {"name": "Tech Ops", "code": "techops", "description": "Technology Operations"},
            {"name": "HR", "code": "hr", "description": "Human Resources"},
            {"name": "Corporate Services", "code": "corp-services", "description": "Corporate & Shared Services"},
        ]
        divs = {}
        for data in div_data:
            obj, _ = Division.objects.update_or_create(
                code=data["code"], business_unit=None,
                defaults={"name": data["name"], "description": data["description"]},
            )
            divs[data["code"]] = obj

        # ── Work Streams (representative examples per division) ────────────────
        ws_map = {
            "cmo": [
                ("Brand & Campaigns", "brand-campaigns"),
                ("Digital Marketing", "digital-mktg"),
                ("Customer Insights", "cx-insights"),
            ],
            "cso": [
                ("M&A & Partnerships", "ma-partnerships"),
                ("Strategic Planning", "strategic-planning"),
            ],
            "edops": [
                ("Content Production", "content-prod"),
                ("Journal Management", "journal-mgmt"),
                ("Author Services", "author-services"),
            ],
            "fah": [
                ("Financial Reporting", "fin-reporting"),
                ("FP&A", "fpa"),
                ("Analytics & BI", "analytics-bi"),
            ],
            "hrss": [
                ("Payroll & Benefits", "payroll-benefits"),
                ("HR Systems", "hr-systems"),
                ("Onboarding", "onboarding"),
            ],
            "sales": [
                ("Account Management", "account-mgmt"),
                ("Sales Operations", "sales-ops"),
                ("Renewals", "renewals"),
            ],
            "techops": [
                ("Platform Engineering", "platform-eng"),
                ("Security & Compliance", "security"),
                ("Infrastructure", "infrastructure"),
            ],
            "hr": [
                ("Talent Acquisition", "talent-acq"),
                ("L&D", "learning-dev"),
                ("Employee Relations", "emp-relations"),
            ],
            "corp-services": [
                ("Legal", "legal"),
                ("Procurement", "procurement"),
                ("Facilities", "facilities"),
            ],
        }
        wss = {}
        for div_code, streams in ws_map.items():
            div = divs[div_code]
            for ws_name, ws_code in streams:
                obj, _ = WorkStream.objects.update_or_create(
                    division=div, code=ws_code,
                    defaults={"name": ws_name},
                )
                wss[ws_code] = obj

        # ── Processes (examples for a few work streams) ────────────────────────
        proc_map = {
            "fin-reporting": [("Monthly Close", "monthly-close"), ("Board Pack", "board-pack")],
            "fpa": [("Budget Cycle", "budget-cycle"), ("Variance Analysis", "variance-analysis")],
            "platform-eng": [("Agent Deployment", "agent-deploy"), ("CI/CD Pipeline", "cicd")],
            "content-prod": [("Article Intake", "article-intake"), ("Peer Review", "peer-review")],
            "account-mgmt": [("Account Review", "account-review"), ("Upsell Identification", "upsell")],
            "sales-ops": [("CRM Hygiene", "crm-hygiene"), ("Pipeline Reporting", "pipeline-report")],
        }
        for ws_code, procs in proc_map.items():
            if ws_code not in wss:
                continue
            ws = wss[ws_code]
            for proc_name, proc_code in procs:
                OrgProcess.objects.update_or_create(
                    work_stream=ws, code=proc_code,
                    defaults={"name": proc_name},
                )

        # ── Link demo agents to org nodes ─────────────────────────────────────
        links = {
            "agent-deployment-advisor": {
                "org_unit": bus["relx"],
                "org_division": divs["techops"],
                "org_work_stream": wss.get("platform-eng"),
            },
            "contract-review-assistant": {
                "org_unit": bus["lnl"],
                "org_division": divs["corp-services"],
                "org_work_stream": wss.get("legal"),
            },
            "support-case-summarizer": {
                "org_unit": bus["elsevier"],
                "org_division": divs["sales"],
                "org_work_stream": wss.get("account-mgmt"),
            },
            "finance-variance-explainer": {
                "org_unit": bus["relx"],
                "org_division": divs["fah"],
                "org_work_stream": wss.get("fpa"),
            },
            "incident-triage-agent": {
                "org_unit": bus["relx"],
                "org_division": divs["techops"],
                "org_work_stream": wss.get("infrastructure"),
            },
        }
        for slug, org_fields in links.items():
            Agent.objects.filter(slug=slug).update(**{
                k: v.id if v else None for k, v in org_fields.items()
            })

        bu_count = BusinessUnit.objects.count()
        div_count = Division.objects.count()
        ws_count = WorkStream.objects.count()
        proc_count = OrgProcess.objects.count()
        self.stdout.write(self.style.SUCCESS(
            f"Seeded org hierarchy: {bu_count} BUs, {div_count} divisions, "
            f"{ws_count} work streams, {proc_count} processes."
        ))

        # Create a demo superuser if none exists.
        if not User.objects.filter(username="admin").exists():
            User.objects.create_superuser(
                username="admin",
                email="admin@reph.internal",
                password="admin",
            )
            self.stdout.write(self.style.WARNING("Created superuser: admin / admin — change password before any real deployment."))
        else:
            self.stdout.write("Superuser 'admin' already exists — skipped.")

    def _seed_rbac_groups(self):
        """Create the four platform RBAC groups if they don't exist."""
        groups = [
            "agent_viewer",
            "agent_builder",
            "agent_approver",
            "platform_admin",
        ]
        created = []
        for name in groups:
            _, was_created = Group.objects.get_or_create(name=name)
            if was_created:
                created.append(name)
        if created:
            self.stdout.write(self.style.SUCCESS(f"Created RBAC groups: {', '.join(created)}"))
        else:
            self.stdout.write("RBAC groups already exist — skipped.")

    def _seed_governance_for_production_agents(self):
        """Ensure every PRODUCTION agent has an approved GovernanceReview
        so the governance gate doesn't block re-seeding demo data."""
        production_agents = Agent.objects.filter(status=Agent.Status.PRODUCTION)
        seeded = 0
        for agent in production_agents:
            if not agent.governance_reviews.filter(status=GovernanceReview.Status.APPROVED).exists():
                GovernanceReview.objects.create(
                    agent=agent,
                    reviewer="seed_demo",
                    status=GovernanceReview.Status.APPROVED,
                    notes="Auto-approved by seed_demo for demo data.",
                )
                seeded += 1
        if seeded:
            self.stdout.write(self.style.SUCCESS(f"Auto-approved governance for {seeded} production agent(s)."))
