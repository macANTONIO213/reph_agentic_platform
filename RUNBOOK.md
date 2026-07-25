# Operations Runbook — REPH Agentic Platform

> On-call reference for the control plane. Covers health, incident response,
> rollback, backup/restore, DR, and the monitoring dashboard spec. Companion to
> `PRODUCTION_HARDENING_PLAN.md` (roadmap) — this is the *operate it today* guide.

---

## 1. Service topology

| Component | What it is | Scale unit | Fails how |
|---|---|---|---|
| **web** | Gunicorn/Django (`agentic_platform.wsgi`) | stateless; scale on CPU/RPS | 5xx, readiness 503 |
| **worker** | Celery worker (`-A agentic_platform`) | stateless; scale on queue depth | tasks stuck PENDING/WORKING |
| **redis** | Celery broker + shared cache (rate-limit/breaker counters) | single (HA in prod) | tasks not dispatched; limits go per-process |
| **postgres** | System of record (runs, audit, registry) | HA primary + replica | readiness 503; hard outage |

Execution backend is selected by `EXECUTION_BACKEND` (`db` = inline/DB-queue, `celery` = worker). Production runs `celery`.

---

## 2. Health & probes

- `GET /healthz` — **liveness**, unauthenticated, no I/O. Non-200 ⇒ restart the instance.
- `GET /readyz` — **readiness**, unauthenticated. Checks Postgres (hard) + cache (soft). 503 ⇒ LB pulls the instance. DB down ⇒ not ready.
- `GET /api/v1/metrics/` — Prometheus exposition. Auth: session `platform_admin` **or** `Authorization: Bearer <METRICS_SCRAPE_TOKENS>`.

Every response carries `X-Request-ID` (correlation id). Logs are JSON when `LOG_FORMAT=json`, each line carrying `correlation_id` — grep it to trace one request across web + worker.

---

## 3. Incident response

**Triage order:** `/readyz` → dashboards (§7) → recent deploy? → logs by correlation id.

### 3.1 Elevated 5xx / latency
1. Check `/readyz` on each instance; pull unhealthy ones.
2. Dashboard: API error rate, p95 latency, DB timings.
3. If it began at a deploy → **roll back** (§4).
4. If DB-bound: check connections/slow queries; scale down worker concurrency to shed DB load.

### 3.2 Queue backlog (tasks not completing)
1. `GET /api/v1/metrics/` → queue pending/stale gauges; alert threshold `PLATFORM_QUEUE_PENDING_WARN_THRESHOLD`.
2. Confirm workers alive (Redis reachable, `celery ping`).
3. Stale RUNNING/WORKING are auto-recovered: requeued while attempts remain, **dead-lettered** after `WORKFLOW_RUN_MAX_ATTEMPTS` / `ASYNC_TASK_MAX_ATTEMPTS`.
4. Inspect dead-letters: `workflow_queue.list_dead_letters()`. Requeue after fixing root cause: `workflow_queue.requeue_dead_letter(run)`.

### 3.3 A failing agent hammering the fleet
Per-agent circuit breaker opens after `AGENT_CIRCUIT_BREAKER_FAILURE_THRESHOLD` consecutive failures (log: `Agent circuit breaker OPEN`). It auto-closes after cooldown. To force-close early, clear the cache keys `cb:agent:*:<agent_id>`.

### 3.4 Suspected abuse / SSRF / auth
- Rate limit returns 429 (per-user/IP) across `/api/*` and `/a2a/`. Tighten via `API_RATE_LIMIT_REQUESTS_PER_WINDOW`.
- SSRF: set `NET_GUARD_RESOLVE_DNS=True` and `NET_GUARD_BLOCK_PRIVATE=True` to reject internal/rebinding destinations platform-wide.
- Rotate interop tokens: update `A2A_ACCESS_TOKENS` / `MCP_SERVER_TOKENS` / `METRICS_SCRAPE_TOKENS` and redeploy; disable a surface entirely via `A2A_SERVER_ENABLED=False` / `MCP_SERVER_ENABLED=False`.
- Audit trail is append-only (`AuditLog`, immutable at the model layer) — query by actor/resource for forensics.

---

## 4. Rollback strategy

**App code (Render/containers):** redeploy the previous image/commit. Web + worker are stateless — no state migration needed.

**Migrations:** ship expand/contract only (additive first, destructive later), so the previous release runs against the new schema. This release's migration `0020` is **purely additive** (new nullable columns + indexes + a new state value) → the prior release is forward-compatible; rolling back code needs no DB rollback. If a migration must be reverted: `python manage.py migrate controlplane <previous_number>`.

**Feature flags:** all hardening behaviours default to prior behaviour and are env-toggled (`EXECUTION_BACKEND`, `NET_GUARD_*`, `EVAL_GATE_REQUIRE_SUITE_MIN_TIER`, `ORCHESTRATOR_MAX_PARALLEL`). Disable a suspect control by flipping its var and restarting — no redeploy of code required.

---

## 5. Backup strategy

| Data | Method | Cadence | Retention |
|---|---|---|---|
| Postgres | Managed automated backups + PITR (WAL) | continuous + daily snapshot | 30 days |
| Redis | Broker/cache is **not** the source of truth — `WorkflowRun`/`AsyncAgentTask` persist in Postgres. AOF for in-flight only. | AOF | n/a |
| Compliance evidence | `python manage.py export_compliance_evidence` | scheduled (nightly) | immutable store, ≥1 yr |
| Static assets | Rebuilt from source at deploy (`collectstatic`) | per deploy | n/a |

**Retention/erasure:** `python manage.py enforce_retention` applies `RETENTION_*_DAYS` windows.

---

## 6. Disaster recovery (RPO/RTO targets)

- **RPO ≤ 5 min** (Postgres PITR/WAL). **RTO ≤ 1 hr** (restore snapshot + redeploy stateless services).
- **Restore drill (quarterly):**
  1. Provision a fresh Postgres from the latest snapshot.
  2. Point `DATABASE_URL` at it; deploy web + worker.
  3. `python manage.py migrate` (no-op if snapshot current).
  4. Verify `/readyz` 200, run a smoke workflow, confirm audit continuity.
  5. Record restore time vs RTO; file the evidence.
- **Broker loss:** acknowledged work is recoverable because run/task rows are the source of truth — on restart, `recover_stale_running_runs` + `recover_stale_working_tasks` requeue in-flight work.
- **Region loss:** promote the Postgres replica; redeploy stateless tier in the standby region.

---

## 7. Monitoring dashboard specification

**Golden signals** (from `/api/v1/metrics/`, OTel spans, `platform_maturity`):

1. **Traffic** — requests/sec by endpoint; A2A vs internal.
2. **Errors** — 5xx rate, guardrail blocks, circuit-breaker-open events, 429 rate.
3. **Latency** — API p50/p95/p99 vs `PLATFORM_SLO_P95_LATENCY_MS_TARGET`; LLM call latency by provider.
4. **Saturation** — queue pending/stale depth vs `PLATFORM_QUEUE_PENDING_WARN_THRESHOLD`; worker concurrency; DB connections/timings.
5. **Cost/quality** — per-agent/BU spend, budget alerts, quality-drift alerts.
6. **Reliability** — dead-letter count (workflow + task), stale-recovery rate, success rate vs `PLATFORM_SLO_SUCCESS_RATE_TARGET`.

**Alerts (page):** readiness 503 > 1 min · 5xx > 2% for 5 min · queue backlog > threshold for 10 min · dead-letter count increasing · any circuit breaker open > 5 min · budget/quality-drift alert. Each alert links back to the matching §3 procedure.

---

## 8. Routine ops

- **Token rotation:** update the env var, redeploy, confirm old token 401s. Quarterly.
- **Dependency updates:** bump `constraints.txt`, run suite + `pip-audit`, deploy. CI blocks on known-vuln deps.
- **Stale-run sweep:** runs automatically in the worker loop / `process_workflow_runs`; verify `recovered`/`recovered_tasks` counters trend to 0.
