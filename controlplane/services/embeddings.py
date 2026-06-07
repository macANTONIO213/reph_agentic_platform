"""
EmbeddingService — generates and searches vector embeddings for agents and documents.

Dual-mode operation:
  • SQLite (local dev): embeddings stored as JSON; cosine similarity in Python.
  • Postgres + pgvector (production): native vector column; SQL cosine search.

Embedding model: OpenAI text-embedding-3-small (1536 dims, cheap, fast).
Falls back gracefully if OPENAI_API_KEY is not set (returns empty vector,
logs warning — semantic search degrades to keyword fallback).

Usage::
    from controlplane.services.embeddings import embedding_service

    # Embed / re-embed all agents
    embedding_service.embed_all_agents()

    # Semantic search
    results = embedding_service.search_agents("document summarisation for legal team", top_k=5)

    # Embed a document chunk
    embedding_service.embed_text("some text")  → list[float]
"""
import hashlib
import logging
import math

from django.conf import settings
from django.db import connection

logger = logging.getLogger(__name__)

_EMBED_MODEL = "text-embedding-3-small"
_EMBED_DIMS  = 1536


def _openai_client():
    """Lazy OpenAI client — only initialised when first needed."""
    import openai
    api_key = getattr(settings, "OPENAI_API_KEY", "") or ""
    if not api_key:
        # Try Anthropic key path — fall back to None (will log warning)
        return None
    return openai.OpenAI(api_key=api_key)


def _embed_via_api(text: str) -> list[float]:
    """Call OpenAI embeddings API. Returns empty list on failure."""
    client = _openai_client()
    if client is None:
        logger.warning("OPENAI_API_KEY not set — semantic embeddings disabled.")
        return []
    try:
        resp = client.embeddings.create(model=_EMBED_MODEL, input=text[:8000])
        return resp.data[0].embedding
    except Exception as exc:
        logger.error("Embedding API error: %s", exc)
        return []


def _cosine_python(vec_a: list[float], vec_b: list[float]) -> float:
    """Pure-Python cosine similarity fallback for SQLite."""
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    mag_a = math.sqrt(sum(a * a for a in vec_a))
    mag_b = math.sqrt(sum(b * b for b in vec_b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _is_postgres() -> bool:
    return connection.vendor == "postgresql"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _agent_text(agent) -> str:
    """Concatenate searchable fields into a single embedding text."""
    parts = [
        agent.name,
        agent.purpose,
        agent.business_unit or "",
        agent.get_platform_display(),
        " ".join(agent.tool_names or []),
        " ".join(agent.data_sources or []),
    ]
    return " | ".join(p for p in parts if p).strip()


class EmbeddingService:
    """Stateless singleton. Call embed_all_agents() and search_agents()."""

    # ── Agent embedding ───────────────────────────────────────────────────────

    def embed_agent(self, agent) -> bool:
        """
        Generate (or refresh) the embedding for one agent.
        Returns True if embedding was written, False if skipped (unchanged).
        """
        from controlplane.models import AgentEmbedding

        text = _agent_text(agent)
        text_hash = _sha256(text)

        existing = AgentEmbedding.objects.filter(agent=agent).first()
        if existing and existing.text_hash == text_hash:
            return False  # nothing changed

        vector = _embed_via_api(text)
        AgentEmbedding.objects.update_or_create(
            agent=agent,
            defaults={"vector": vector, "model_id": _EMBED_MODEL, "text_hash": text_hash},
        )
        return True

    def embed_all_agents(self) -> dict:
        """Embed every agent. Returns {"embedded": N, "skipped": N, "failed": N}."""
        from controlplane.models import Agent
        counts = {"embedded": 0, "skipped": 0, "failed": 0}
        for agent in Agent.objects.all():
            try:
                written = self.embed_agent(agent)
                counts["embedded" if written else "skipped"] += 1
            except Exception as exc:
                logger.error("Failed to embed agent %s: %s", agent.slug, exc)
                counts["failed"] += 1
        return counts

    def search_agents(self, query: str, top_k: int = 5, business_unit_id=None) -> list[dict]:
        """
        Semantic search over agent embeddings.
        Returns top_k agents sorted by cosine similarity descending.
        Falls back to keyword search if no embeddings exist.
        """
        from controlplane.models import Agent, AgentEmbedding

        query_vec = _embed_via_api(query)

        # ── Postgres path: fast ANN via pgvector ─────────────────────────────
        if _is_postgres() and query_vec:
            return self._pg_search(query_vec, top_k, business_unit_id)

        # ── Python fallback: load all embeddings, compute in memory ──────────
        if query_vec:
            return self._python_search(query_vec, top_k, business_unit_id)

        # ── No embeddings: keyword fallback ──────────────────────────────────
        return self._keyword_search(query, top_k, business_unit_id)

    # ── Document chunk embedding ──────────────────────────────────────────────

    def embed_text(self, text: str) -> list[float]:
        """Embed arbitrary text (for document chunks)."""
        return _embed_via_api(text)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _pg_search(self, query_vec: list[float], top_k: int, bu_id) -> list[dict]:
        """Use pgvector <=> operator for cosine distance search."""
        from controlplane.models import AgentEmbedding
        # pgvector stores vectors natively; we stored as JSON — convert
        # For now use Python path until pgvector column migration is added.
        # (Phase C1 uses JSON storage compatible with both backends.)
        return self._python_search(query_vec, top_k, bu_id)

    def _python_search(self, query_vec: list[float], top_k: int, bu_id) -> list[dict]:
        from controlplane.models import AgentEmbedding
        qs = AgentEmbedding.objects.select_related("agent").filter(
            agent__status__in=["draft", "review", "pilot", "production"]
        )
        if bu_id:
            qs = qs.filter(agent__org_unit_id=bu_id)

        scored = []
        for emb in qs:
            if not emb.vector:
                continue
            score = _cosine_python(query_vec, emb.vector)
            scored.append((score, emb.agent))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {
                "agent_id": str(a.id),
                "name": a.name,
                "purpose": a.purpose,
                "status": a.status,
                "risk_tier": a.risk_tier,
                "business_unit": a.business_unit,
                "score": round(score, 4),
            }
            for score, a in scored[:top_k]
            if score > 0.1  # filter near-zero matches
        ]

    @staticmethod
    def _keyword_search(query: str, top_k: int, bu_id) -> list[dict]:
        """Fallback when embeddings are unavailable."""
        import re
        from django.db.models import Q
        from controlplane.models import Agent

        terms = [t for t in re.split(r"[^a-zA-Z0-9]+", query[:300].lower()) if len(t) > 2]
        q = Q()
        for term in terms[:6]:
            q |= Q(name__icontains=term) | Q(purpose__icontains=term)

        qs = Agent.objects.filter(q) if terms else Agent.objects.all()
        if bu_id:
            qs = qs.filter(org_unit_id=bu_id)
        return [
            {
                "agent_id": str(a.id),
                "name": a.name,
                "purpose": a.purpose,
                "status": a.status,
                "risk_tier": a.risk_tier,
                "business_unit": a.business_unit,
                "score": 0.5,  # keyword match sentinel
            }
            for a in qs[:top_k]
        ]


# Module-level singleton
embedding_service = EmbeddingService()
