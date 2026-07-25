# Phase 3 Technical Spec — Scanners (auto-discovery)

> **Scope:** Phase 3 of [AGENT_FABRIC_TRANSFORMATION_STRATEGY.md](AGENT_FABRIC_TRANSFORMATION_STRATEGY.md).
> Auto-discover agents from cloud platforms (Bedrock/Vertex/Agentforce/Copilot), normalise to
> the federated registry, and land them as **governed, non-live** entries pending approval —
> reusing the `PackageIngestor` "validate → normalise → sandbox-only" posture. Builds directly
> on Phase 2's `RegistryEntry` (the `source="scanner"` path was reserved). **Prepared:** 25 Jul 2026.

---

## 1. Design principles

1. **Discovered ≠ trusted.** A scanned agent lands as `review_status="discovered"` and is **not**
   surfaced in agent-facing discovery until a human approves it. This is the scanner analogue of
   sandbox-by-default — Fabric ingests ungoverned; we ingest governed.
2. **Pluggable, injectable clients.** Each platform scanner is a small class whose cloud client
   is injectable, so scanners are unit-testable offline and a new platform is one file.
3. **One catalog.** Scanners normalise to the same `RegistryEntry` table via
   `federation.catalog_scanned_agent` — no parallel store. Continuous re-scan is idempotent and
   never downgrades an already-approved entry.

---

## 2. Data model change

Add `review_status` to `RegistryEntry`: `discovered | approved | rejected` (default `approved`).
Projected/manual/external-registered entries stay `approved` (already governed sources);
scanned entries start `discovered`. Discovery search defaults to `approved` only.

## 3. Components

- `services/scanners/base.py` — `DiscoveredAgent` dataclass + `BaseScanner`.
- `services/scanners/bedrock.py` — AWS Bedrock scanner (`bedrock-agent list_agents`), client
  injectable. (Vertex/Agentforce/Copilot follow the same shape — step 2.)
- `services/scanners/service.py` — `run_scan(platform)` orchestrator + scanner registry.
- `federation.catalog_scanned_agent` — normalise a `DiscoveredAgent` → `RegistryEntry`
  (`source=scanner`, `review_status=discovered`, capability/model/data-access captured).
- API: `GET /scanners/`, `POST /scanners/<platform>/scan/` (admin), `POST /registry/<id>/approve/`
  (approver). Command: `run_scanner <platform>`.

## 4. Governance invariants

- Scanned agents are `discovered` and excluded from agent-facing `/a2a/registry/` until approved.
- Re-scanning never downgrades an approved entry back to discovered.
- Running a scanner requires `platform_admin`; approving requires `agent_approver`.
- Every scan and approval is audited.

## 5. Steps

1. ✅ `review_status` + scanner framework + Bedrock scanner + catalog + scan/approve API +
   command. *(done)*
2. ✅ Additional platform scanners — **Vertex** (Reasoning/Agent Engine, `vertex.py`),
   **Agentforce** (Salesforce Bot via REST, SSRF-guarded, `agentforce.py`) and **Copilot Studio**
   (Power Platform / Dataverse bots, SSRF-guarded, `copilot.py`), all registered in the scanner
   service. Same injectable-client, offline-testable shape as Bedrock. Plus a **scan-all runner**
   (`run_all_scans`, `POST /scanners/scan-all/`, `run_scanner all`, and a "Scan all" button in the
   operator UI) that is partial-tolerant — one unconfigured platform never aborts the sweep. *(done)*

**Optional deps:** Vertex uses `google-api-python-client` and Agentforce a Salesforce access
token — both lazy-loaded; absent config surfaces a clear `ScannerError`, never a crash.
