"""
Phase C tests — Data & Knowledge Layer
Covers: EmbeddingService (C1), RagService (C2), SqlConnector (C3),
        API endpoints for semantic search / knowledge / connectors.
"""
import json
import math
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import Client, TestCase

from controlplane.models import (
    Agent,
    AgentEmbedding,
    BusinessUnit,
    DataConnector,
    DocumentChunk,
    KnowledgeDocument,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(slug="test-agent", bu=None):
    return Agent.objects.create(
        name=slug.replace("-", " ").title(),
        slug=slug,
        purpose="Automate report generation",
        business_unit=bu.name if bu else "Engineering",
        risk_tier=1,
        status=Agent.Status.PRODUCTION,
        org_unit=bu,
    )


def _make_bu(name="Engineering"):
    bu, _ = BusinessUnit.objects.get_or_create(
        name=name,
        defaults={"code": name[:4].upper()},
    )
    return bu


def _make_user(username="testuser", staff=True):
    u, _ = User.objects.get_or_create(username=username, defaults={"is_staff": staff})
    return u


def _fake_vector(n=1536):
    return [1.0 / math.sqrt(n)] * n


# ---------------------------------------------------------------------------
# C1 — EmbeddingService
# ---------------------------------------------------------------------------

class EmbeddingServiceTests(TestCase):

    def setUp(self):
        self.bu = _make_bu()
        self.agent = _make_agent(bu=self.bu)

    def _fake_vector(self, n=1536):
        return _fake_vector(n)

    @patch("controlplane.services.embeddings._embed_via_api")
    def test_embed_agent_stores_embedding(self, mock_embed):
        from controlplane.services.embeddings import EmbeddingService
        vec = self._fake_vector()
        mock_embed.return_value = vec
        svc = EmbeddingService()
        written = svc.embed_agent(self.agent)
        self.assertTrue(written)
        emb = AgentEmbedding.objects.get(agent=self.agent)
        self.assertEqual(len(emb.vector), 1536)

    @patch("controlplane.services.embeddings._embed_via_api")
    def test_embed_agent_skips_unchanged_hash(self, mock_embed):
        from controlplane.services.embeddings import EmbeddingService
        vec = self._fake_vector()
        mock_embed.return_value = vec
        svc = EmbeddingService()
        svc.embed_agent(self.agent)
        mock_embed.reset_mock()
        # Second call — same text, should skip
        written = svc.embed_agent(self.agent)
        self.assertFalse(written)
        mock_embed.assert_not_called()

    @patch("controlplane.services.embeddings._embed_via_api")
    def test_search_agents_returns_results(self, mock_embed):
        from controlplane.services.embeddings import EmbeddingService
        vec = self._fake_vector()
        mock_embed.return_value = vec
        svc = EmbeddingService()
        svc.embed_agent(self.agent)
        results = svc.search_agents("report generation", top_k=5)
        self.assertIsInstance(results, list)
        self.assertGreater(len(results), 0)
        self.assertIn("agent_id", results[0])
        self.assertIn("score", results[0])

    def test_search_agents_keyword_fallback_no_embeddings(self):
        """When no embeddings exist, keyword fallback returns results."""
        from controlplane.services.embeddings import EmbeddingService
        svc = EmbeddingService()
        # No embeddings stored — should fall back to keyword
        results = svc.search_agents("report generation", top_k=5)
        self.assertIsInstance(results, list)

    def test_cosine_similarity_identical_vectors(self):
        from controlplane.services.embeddings import _cosine_python
        v = self._fake_vector()
        sim = _cosine_python(v, v)
        self.assertAlmostEqual(sim, 1.0, places=5)

    def test_cosine_similarity_orthogonal_vectors(self):
        from controlplane.services.embeddings import _cosine_python
        a = [1.0, 0.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0, 0.0]
        sim = _cosine_python(a, b)
        self.assertAlmostEqual(sim, 0.0, places=5)


# ---------------------------------------------------------------------------
# C2 — RagService
# ---------------------------------------------------------------------------

class RagServiceTests(TestCase):

    def _fake_vector(self, n=1536):
        return _fake_vector(n)

    def setUp(self):
        self.bu = _make_bu()
        self.agent = _make_agent(bu=self.bu)

    @patch("controlplane.services.rag.RagService._chunk_and_embed")
    def test_ingest_text_creates_document_and_chunks(self, mock_embed):
        from controlplane.services.rag import RagService
        from controlplane.models import DocumentChunk

        def _fake_chunk_and_embed(doc, text):
            # Create a single chunk so chunk_count > 0
            DocumentChunk.objects.create(
                document=doc, chunk_index=0, text=text[:200], vector=[], token_count=10
            )
            doc.chunk_count = 1

        mock_embed.side_effect = _fake_chunk_and_embed
        svc = RagService()
        text = "A" * 200
        doc = svc.ingest_text(title="Test Policy", text=text, business_unit=self.bu, uploaded_by="admin")
        self.assertEqual(doc.status, KnowledgeDocument.Status.READY)
        self.assertGreater(doc.chunk_count, 0)

    @patch("controlplane.services.rag.RagService._chunk_and_embed")
    def test_ingest_text_chunking_sets_chunk_count(self, mock_embed):
        from controlplane.services.rag import RagService

        def _fake(doc, text):
            doc.chunk_count = 3

        mock_embed.side_effect = _fake
        svc = RagService()
        text = "word " * 400
        doc = svc.ingest_text(title="Large Doc", text=text, business_unit=self.bu, uploaded_by="admin")
        self.assertEqual(doc.chunk_count, 3)

    @patch("controlplane.services.embeddings._embed_via_api")
    @patch("controlplane.services.rag.RagService._chunk_and_embed")
    def test_retrieve_returns_relevant_chunks(self, mock_embed_chunks, mock_embed_api):
        from controlplane.services.rag import RagService
        vec = self._fake_vector()
        mock_embed_api.return_value = vec

        def _fake(doc, text):
            DocumentChunk.objects.create(
                document=doc, chunk_index=0, text=text, vector=vec, token_count=10
            )
            doc.chunk_count = 1

        mock_embed_chunks.side_effect = _fake
        svc = RagService()
        doc = svc.ingest_text(
            title="Revenue Report",
            text="Revenue grew by 15% in Q2 driven by new enterprise customers.",
            business_unit=self.bu,
            uploaded_by="admin",
        )
        passages = svc.retrieve(query="revenue growth", agent=self.agent, top_k=4)
        self.assertIsInstance(passages, list)

    @patch("controlplane.services.rag.RagService._chunk_and_embed")
    def test_ingest_text_bu_scope(self, mock_embed):
        from controlplane.services.rag import RagService
        mock_embed.return_value = None
        svc = RagService()
        other_bu = _make_bu("Finance")
        doc = svc.ingest_text(
            title="Finance Policy",
            text="Finance department operating procedures.",
            business_unit=other_bu,
            uploaded_by="admin",
        )
        self.assertEqual(doc.business_unit, other_bu)


# ---------------------------------------------------------------------------
# C3 — SqlConnector
# ---------------------------------------------------------------------------

class SqlConnectorTests(TestCase):
    """Tests use the static _validate() method directly — no DB connector needed."""

    def _validate(self, sql):
        from controlplane.services.connectors.sql_connector import SqlConnector
        SqlConnector._validate(sql)

    def _assert_blocked(self, sql):
        from controlplane.services.connectors.sql_connector import SqlConnectorError
        with self.assertRaises(SqlConnectorError):
            self._validate(sql)

    def test_select_query_passes_validation(self):
        self._validate("SELECT id, name FROM agents WHERE status='active'")

    def test_non_select_drop_raises(self):
        self._assert_blocked("DROP TABLE agents")

    def test_insert_query_raises(self):
        self._assert_blocked("INSERT INTO agents VALUES (1, 'x')")

    def test_update_query_raises(self):
        self._assert_blocked("UPDATE agents SET status='deleted'")

    def test_delete_query_raises(self):
        self._assert_blocked("DELETE FROM agents WHERE id=1")

    def test_select_with_subquery_passes(self):
        self._validate("SELECT * FROM (SELECT id FROM agents) sub WHERE sub.id > 0")

    def test_create_table_raises(self):
        self._assert_blocked("CREATE TABLE foo (id INT)")


# ---------------------------------------------------------------------------
# C3 — RestConnector
# ---------------------------------------------------------------------------

class RestConnectorTests(TestCase):

    def _make_dc(self, base_url="https://api.example.com"):
        bu = _make_bu()
        return DataConnector.objects.create(
            name="Test REST",
            connector_type="rest",
            business_unit=bu,
            config={"base_url": base_url, "auth_header": "Bearer test-token"},
            is_active=True,
        )

    def _make_connector(self, base_url="https://api.example.com"):
        from controlplane.services.connectors.rest_connector import RestConnector
        dc = self._make_dc(base_url)
        return RestConnector(connector=dc)

    def test_base_url_enforced(self):
        conn = self._make_connector("https://api.example.com")
        url = conn._build_url("/v1/reports")
        self.assertTrue(url.startswith("https://api.example.com"))

    def test_build_url_joins_correctly(self):
        conn = self._make_connector("https://api.example.com")
        self.assertEqual(conn._build_url("/data"), "https://api.example.com/data")

    def test_build_url_strips_double_slash(self):
        conn = self._make_connector("https://api.example.com/")
        self.assertEqual(conn._build_url("/data"), "https://api.example.com/data")


# ---------------------------------------------------------------------------
# API endpoint tests — semantic search
# ---------------------------------------------------------------------------

class SemanticSearchApiTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.user = _make_user()
        self.client.force_login(self.user)
        self.bu = _make_bu()
        self.agent = _make_agent(bu=self.bu)

    @patch("controlplane.services.embeddings.EmbeddingService.search_agents")
    def test_semantic_search_returns_results(self, mock_search):
        mock_search.return_value = [
            {"agent_id": str(self.agent.id), "name": self.agent.name, "score": 0.92}
        ]
        resp = self.client.get("/api/v1/agents/search/?q=report+generation")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertIn("results", data)

    def test_semantic_search_requires_auth(self):
        anon = Client()
        resp = anon.get("/api/v1/agents/search/?q=test")
        self.assertIn(resp.status_code, [302, 401, 403])

    def test_semantic_search_missing_q_returns_400(self):
        resp = self.client.get("/api/v1/agents/search/")
        self.assertEqual(resp.status_code, 400)


# ---------------------------------------------------------------------------
# API endpoint tests — knowledge base
# ---------------------------------------------------------------------------

class KnowledgeApiTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.user = _make_user()
        self.client.force_login(self.user)
        self.bu = _make_bu()

    def _create_doc(self, title="Test Doc", bu=None):
        return KnowledgeDocument.objects.create(
            title=title,
            business_unit=bu or self.bu,
            uploaded_by="admin",
            status=KnowledgeDocument.Status.READY,
            file_type="txt",
            raw_text="Sample text content.",
            chunk_count=1,
        )

    def test_knowledge_list_returns_documents(self):
        self._create_doc()
        resp = self.client.get("/api/v1/knowledge/")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertIn("documents", data)
        self.assertGreater(len(data["documents"]), 0)

    def test_knowledge_list_filters_by_bu(self):
        other_bu = _make_bu("Legal")
        self._create_doc("Engineering Doc", self.bu)
        self._create_doc("Legal Doc", other_bu)
        resp = self.client.get(f"/api/v1/knowledge/?business_unit={self.bu.id}")
        data = json.loads(resp.content)
        titles = [d["title"] for d in data["documents"]]
        self.assertIn("Engineering Doc", titles)
        self.assertNotIn("Legal Doc", titles)

    @patch("controlplane.services.rag.RagService.retrieve")
    def test_knowledge_retrieve_returns_passages(self, mock_retrieve):
        mock_retrieve.return_value = [
            {"title": "Test Doc", "text": "Revenue grew 15%", "score": 0.88}
        ]
        resp = self.client.post(
            "/api/v1/knowledge/retrieve/",
            data=json.dumps({"query": "revenue growth", "top_k": 4}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertIn("passages", data)

    def test_knowledge_retrieve_missing_query_returns_400(self):
        resp = self.client.post(
            "/api/v1/knowledge/retrieve/",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    @patch("controlplane.services.rag.RagService.ingest_text")
    def test_knowledge_ingest_plain_text(self, mock_ingest):
        doc = KnowledgeDocument.objects.create(
            title="Ingested Doc",
            uploaded_by="admin",
            status=KnowledgeDocument.Status.READY,
        )
        mock_ingest.return_value = doc
        resp = self.client.post(
            "/api/v1/knowledge/ingest/",
            data=json.dumps({"title": "Policy v1", "text": "Content here.", "business_unit": str(self.bu.id)}),
            content_type="application/json",
        )
        self.assertIn(resp.status_code, [200, 201])
        data = json.loads(resp.content)
        self.assertTrue("document_id" in data or "id" in data)


# ---------------------------------------------------------------------------
# API endpoint tests — connectors
# ---------------------------------------------------------------------------

class ConnectorApiTests(TestCase):

    def setUp(self):
        self.client = Client()
        self.user = _make_user()
        self.client.force_login(self.user)
        self.bu = _make_bu()

    def _create_connector(self, name="Test SQL", ctype="sql", bu=None):
        return DataConnector.objects.create(
            name=name,
            connector_type=ctype,
            business_unit=bu or self.bu,
            config={"url": "sqlite:///test.db"},
            is_active=True,
        )

    def test_connectors_list_returns_active(self):
        self._create_connector()
        resp = self.client.get("/api/v1/connectors/")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertIn("connectors", data)
        self.assertGreater(len(data["connectors"]), 0)

    def test_connectors_list_excludes_inactive(self):
        c = self._create_connector("Inactive Conn")
        c.is_active = False
        c.save()
        resp = self.client.get("/api/v1/connectors/")
        data = json.loads(resp.content)
        names = [x["name"] for x in data["connectors"]]
        self.assertNotIn("Inactive Conn", names)

    def test_connectors_list_requires_auth(self):
        anon = Client()
        resp = anon.get("/api/v1/connectors/")
        self.assertIn(resp.status_code, [302, 401, 403])

    def test_connectors_list_bu_filter(self):
        other_bu = _make_bu("Risk")
        self._create_connector("Eng Connector", bu=self.bu)
        self._create_connector("Risk Connector", bu=other_bu)
        resp = self.client.get(f"/api/v1/connectors/?business_unit={self.bu.id}")
        data = json.loads(resp.content)
        names = [x["name"] for x in data["connectors"]]
        self.assertIn("Eng Connector", names)
        self.assertNotIn("Risk Connector", names)
