# Phase 2 Technical Spec — Federated Registry

> **Scope:** Phase 2 of [AGENT_FABRIC_TRANSFORMATION_STRATEGY.md](AGENT_FABRIC_TRANSFORMATION_STRATEGY.md).
> Turn the internal registry into a **federated catalog of any agent/tool endpoint** —
> first-party agents, external A2A agents, and MCP servers — searchable by humans *and* other
> agents. This is MuleSoft Agent Fabric's "Agent Registry" pillar.
>
> **Builds on Phase 1:** reuses `AgentCard` (first-party projection), `RemoteMcpServer` (MCP),
> and the `/a2a/` surface. **Prepared:** 25 Jul 2026.

---

## 1. Design principles

1. **Materialised catalog, continuously synced.** A single `RegistryEntry` table is the canonical
   federated record (normalised to A2A-card shape). First-party agents and MCP servers are
   *projected* into it; Phase 3 scanners will *sync* into the same table. This mirrors Fabric's
   "continuously synced to the Registry" model and gives one thing to search.
2. **Provenance, not duplication.** An entry points back to its source (`agent`/`mcp_server` FK,
   or an external URL). The source of truth stays the source object; the entry is a denormalised,
   searchable projection.
3. **Governance visible.** Every entry carries a `governance` block (risk tier, guardrail level,
   status) so the catalog advertises posture — the differentiator vs a bare registry.
4. **Sandbox/visibility gated.** Only published/pilot/production first-party agents and active
   MCP servers project. External entries default to `private` visibility.

---

## 2. Data model — `RegistryEntry`

| Field | Purpose |
|---|---|
| `kind` | `first_party_agent` \| `external_a2a_agent` \| `mcp_server` (extensible) |
| `identifier` | stable key within a kind (agent slug, server slug, external id) — unique per kind |
| `name`, `description` | display |
| `protocol` | `a2a` \| `mcp` |
| `endpoint_url` | A2A rpc URL or MCP base URL |
| `domain` | business domain (sales/service/finance/hr…) — for Phase 4 broker routing |
| `provider_org` | owning org / business unit |
| `capabilities` | normalised skills/tools `[{id,name,description}]` |
| `card_json` | full A2A card when applicable |
| `governance` | `{risk_tier, guardrail_level, governance_level, status}` |
| `visibility` | `private` \| `public` |
| `source` | `projection` \| `manual` \| `scanner` |
| `agent` / `mcp_server` | provenance FKs (nullable) |
| `is_active`, `last_synced_at`, timestamps | lifecycle |

Unique on `(kind, identifier)`.

---

## 3. Steps

1. ✅ **RegistryEntry + projection service + list/detail/sync API + `sync_registry` command.**
   Projects first-party published agents and active MCP servers into the catalog; kept current
   by projecting on publish/sync. *(done)*
2. ✅ **External A2A agent registration** — `register-by-URL` fetches the remote card
   (SSRF-guarded via `a2a_client`), catalogs it as `external_a2a_agent`; `POST /registry/external/`
   + `DELETE /registry/<id>/`. Curated public list deferred. *(done)*
3. ✅ **Federated discovery/search** — shared `search_entries` (text/kind/domain/**capability**);
   agent-facing `GET /a2a/registry/` so other agents and the future broker query the catalog.
   *(done)* Dashboard UI surfacing deferred as front-end polish.

---

## 4. Governance invariants

- A first-party agent only appears in the catalog while its card is published (unpublish →
  entry deactivated).
- An MCP server only projects while active with a synced catalog.
- External registrations are SSRF-guarded and default to `private`.
- Projection never elevates governance state — it copies the source's posture verbatim.
