"""
RagService — document ingestion and retrieval for the knowledge base.

Ingestion pipeline:
  1. Extract text from PDF / DOCX / TXT / MD
  2. Split into chunks (≤400 tokens, 50-token overlap)
  3. Embed each chunk via EmbeddingService
  4. Store as DocumentChunk records

Retrieval:
  1. Embed the query
  2. Cosine-rank all chunks for the agent's accessible BU scope
  3. Return top-k chunks as context passages

Usage::
    from controlplane.services.rag import rag_service

    # Ingest a document from raw text
    doc = rag_service.ingest_text(
        title="GDPR Policy",
        text="...",
        uploaded_by="admin",
        business_unit=bu,
    )

    # Retrieve relevant passages for an agent
    passages = rag_service.retrieve(
        query="What is the data retention policy?",
        agent=agent,
        top_k=4,
    )
"""
import logging
import math

logger = logging.getLogger(__name__)

# Chunk size in characters (≈400 tokens at ~4 chars/token)
_CHUNK_CHARS   = 1600
_OVERLAP_CHARS = 200


def _chunk_text(text: str) -> list[str]:
    """Split text into overlapping chunks."""
    chunks = []
    start = 0
    while start < len(text):
        end = start + _CHUNK_CHARS
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start += _CHUNK_CHARS - _OVERLAP_CHARS
    return chunks


def _extract_pdf(file_bytes: bytes) -> str:
    try:
        import pypdf, io
        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
        return "\n\n".join(
            page.extract_text() or "" for page in reader.pages
        )
    except Exception as exc:
        logger.error("PDF extraction failed: %s", exc)
        return ""


def _extract_docx(file_bytes: bytes) -> str:
    try:
        import docx, io
        doc = docx.Document(io.BytesIO(file_bytes))
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception as exc:
        logger.error("DOCX extraction failed: %s", exc)
        return ""


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    ma  = math.sqrt(sum(x * x for x in a))
    mb  = math.sqrt(sum(x * x for x in b))
    return dot / (ma * mb) if ma and mb else 0.0


class RagService:
    """Stateless singleton."""

    # ── Ingestion ─────────────────────────────────────────────────────────────

    def ingest_text(
        self,
        *,
        title: str,
        text: str,
        uploaded_by: str,
        business_unit=None,
        source_url: str = "",
        description: str = "",
        file_type: str = "txt",
    ):
        """Ingest pre-extracted text. Returns the KnowledgeDocument."""
        from controlplane.models import KnowledgeDocument
        doc = KnowledgeDocument.objects.create(
            title=title,
            description=description,
            source_url=source_url,
            business_unit=business_unit,
            uploaded_by=uploaded_by,
            status=KnowledgeDocument.Status.PROCESSING,
            file_type=file_type,
            raw_text=text,
        )
        try:
            self._chunk_and_embed(doc, text)
            doc.status = KnowledgeDocument.Status.READY
        except Exception as exc:
            doc.status = KnowledgeDocument.Status.ERROR
            doc.error_detail = str(exc)
            logger.error("Ingestion failed for doc %s: %s", doc.id, exc)
        doc.save(update_fields=["status", "error_detail", "chunk_count", "updated_at"])
        return doc

    def ingest_file(
        self,
        *,
        title: str,
        file_bytes: bytes,
        file_type: str,
        uploaded_by: str,
        business_unit=None,
        source_url: str = "",
    ):
        """Extract text from a file then ingest. file_type: pdf | docx | txt | md."""
        if file_type == "pdf":
            text = _extract_pdf(file_bytes)
        elif file_type in ("docx", "doc"):
            text = _extract_docx(file_bytes)
        else:
            text = file_bytes.decode("utf-8", errors="replace")

        return self.ingest_text(
            title=title, text=text, uploaded_by=uploaded_by,
            business_unit=business_unit, source_url=source_url,
            file_type=file_type,
        )

    def reindex_document(self, doc):
        """Re-chunk and re-embed an existing document (e.g. after text edit)."""
        from controlplane.models import DocumentChunk
        DocumentChunk.objects.filter(document=doc).delete()
        doc.status = "processing"
        doc.chunk_count = 0
        doc.save(update_fields=["status", "chunk_count"])
        try:
            self._chunk_and_embed(doc, doc.raw_text)
            doc.status = "ready"
        except Exception as exc:
            doc.status = "error"
            doc.error_detail = str(exc)
        doc.save(update_fields=["status", "error_detail", "chunk_count", "updated_at"])

    # ── Retrieval ─────────────────────────────────────────────────────────────

    def retrieve(self, *, query: str, agent, top_k: int = 4) -> list[dict]:
        """
        Return top_k relevant document passages for the agent's BU scope.

        Each result: {title, chunk_index, text, score}
        """
        from controlplane.services.embeddings import embedding_service
        from controlplane.models import DocumentChunk

        query_vec = embedding_service.embed_text(query)

        # Scope: agent's BU + platform-wide docs (business_unit=None)
        bu_id = agent.org_unit_id if hasattr(agent, "org_unit_id") else None
        qs = DocumentChunk.objects.select_related("document").filter(
            document__status="ready"
        ).filter(
            models.Q(document__business_unit__isnull=True)
            | (models.Q(document__business_unit_id=bu_id) if bu_id else models.Q())
        )

        if not query_vec:
            # No embeddings → keyword fallback
            return self._keyword_retrieve(query, qs, top_k)

        scored = []
        for chunk in qs:
            if not chunk.vector:
                continue
            score = _cosine(query_vec, chunk.vector)
            scored.append((score, chunk))

        scored.sort(key=lambda x: x[0], reverse=True)

        return [
            {
                "title":       chunk.document.title,
                "chunk_index": chunk.chunk_index,
                "text":        chunk.text,
                "score":       round(score, 4),
                "doc_id":      str(chunk.document_id),
            }
            for score, chunk in scored[:top_k]
            if score > 0.15
        ]

    # ── Internal ─────────────────────────────────────────────────────────────

    def _chunk_and_embed(self, doc, text: str):
        from controlplane.models import DocumentChunk
        from controlplane.services.embeddings import embedding_service

        chunks = _chunk_text(text)
        bulk = []
        for idx, chunk_text in enumerate(chunks):
            vec = embedding_service.embed_text(chunk_text)
            token_count = max(1, len(chunk_text) // 4)
            bulk.append(DocumentChunk(
                document=doc,
                chunk_index=idx,
                text=chunk_text,
                vector=vec,
                token_count=token_count,
            ))

        DocumentChunk.objects.bulk_create(bulk, ignore_conflicts=True)
        doc.chunk_count = len(bulk)

    @staticmethod
    def _keyword_retrieve(query: str, qs, top_k: int) -> list[dict]:
        import re
        terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 2]
        results = []
        for chunk in qs:
            text_lower = chunk.text.lower()
            hits = sum(1 for t in terms if t in text_lower)
            if hits:
                results.append((hits, chunk))
        results.sort(key=lambda x: x[0], reverse=True)
        return [
            {
                "title":       chunk.document.title,
                "chunk_index": chunk.chunk_index,
                "text":        chunk.text[:600],
                "score":       0.3,
                "doc_id":      str(chunk.document_id),
            }
            for _, chunk in results[:top_k]
        ]


# Lazy import guard for models.Q used inside retrieve()
from django.db import models

# Module-level singleton
rag_service = RagService()
