import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from qdrant_client.http.exceptions import ResponseHandlingException
from sqlalchemy.exc import SQLAlchemyError

from app.main import app
from app.api import routes
from app.api.deps import current_user
from app.db.session import get_db


class ArchitectureCorsTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.pop(current_user, None)
        app.dependency_overrides.pop(get_db, None)

    def test_development_origins_pass_preflight(self):
        for origin in (
            "http://localhost:5173", "http://localhost:5174",
            "http://127.0.0.1:5173", "http://127.0.0.1:5174",
        ):
            with self.subTest(origin=origin):
                response = self.client.options(
                    "/api/repositories/3/architecture-graph",
                    headers={
                        "Origin": origin,
                        "Access-Control-Request-Method": "GET",
                        "Access-Control-Request-Headers": "authorization,content-type",
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers.get("access-control-allow-origin"), origin)
                self.assertEqual(response.headers.get("access-control-allow-credentials"), "true")

    def test_actual_unauthenticated_endpoint_response_keeps_cors_header(self):
        response = self.client.get(
            "/api/repositories/3/architecture-graph",
            headers={"Origin": "http://localhost:5173"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://localhost:5173")

    def test_generation_error_is_logged_and_returned_with_cors(self):
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=1)
        app.dependency_overrides[get_db] = lambda: object()
        repository = SimpleNamespace(
            id=3, owner="owner", name="repo", status="ready", index_generation="g1", commit_sha="abc"
        )
        with patch.object(routes, "owned_repo", return_value=repository), \
             patch.object(routes, "repository_points", side_effect=RuntimeError("vector store unavailable")), \
             self.assertLogs(routes.logger, level="ERROR") as logs:
            response = self.client.get(
                "/api/repositories/3/architecture-graph",
                headers={"Origin": "http://localhost:5173"},
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://localhost:5173")
        self.assertIn("Architecture graph request failed", "\n".join(logs.output))

    def test_validation_failure_logs_exact_graph_object_without_exposing_it_to_client(self):
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=1)
        app.dependency_overrides[get_db] = lambda: object()
        repository = SimpleNamespace(id=3, owner="owner", name="repo", status="ready", index_generation="g1", commit_sha="abc")
        cause = ValueError("Architecture edge 'edge:4' citation is absent from graph sources: 'routes/api.py' lines 22-31")
        with patch.object(routes, "owned_repo", return_value=repository), \
             patch.object(routes, "repository_points", return_value=[object()]), \
             patch.object(routes, "cached_architecture_graph", side_effect=cause), \
             self.assertLogs(routes.logger, level="ERROR") as logs:
            response = self.client.get("/api/repositories/3/architecture-graph", headers={"Origin":"http://localhost:5173"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("Indexed architecture evidence", response.json()["detail"])
        self.assertNotIn("routes/api.py", response.json()["detail"])
        self.assertIn("edge:4", "\n".join(logs.output))
        self.assertIn("routes/api.py", "\n".join(logs.output))

    def test_successful_architecture_response_keeps_cors(self):
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=1)
        app.dependency_overrides[get_db] = lambda: object()
        repository = SimpleNamespace(
            id=3, owner="owner", name="repo", status="ready", index_generation="g1", commit_sha="abc"
        )
        graph = {"nodes": [], "edges": [], "sources": [], "metadata": {"ai_architecture_status": "deterministic"}}
        with patch.object(routes, "owned_repo", return_value=repository), \
             patch.object(routes, "repository_points", return_value=[object()]), \
             patch.object(routes, "cached_architecture_graph", return_value=graph):
            response = self.client.get(
                "/api/repositories/3/architecture-graph",
                headers={"Origin": "http://localhost:5173"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), graph)
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://localhost:5173")

    def test_qdrant_failure_returns_safe_vector_store_category(self):
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=1)
        app.dependency_overrides[get_db] = lambda: object()
        repository = SimpleNamespace(id=3, owner="owner", name="repo", status="ready", index_generation="g1", commit_sha="abc")
        with patch.object(routes, "owned_repo", return_value=repository), \
             patch.object(routes, "repository_points", side_effect=ResponseHandlingException(ConnectionError("qdrant down"))):
            response = self.client.get("/api/repositories/3/architecture-graph", headers={"Origin":"http://localhost:5173"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("Qdrant", response.json()["detail"])
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://localhost:5173")

    def test_database_failure_returns_safe_database_category(self):
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=1)
        app.dependency_overrides[get_db] = lambda: object()
        with patch.object(routes, "owned_repo", side_effect=SQLAlchemyError("database unavailable")):
            response = self.client.get("/api/repositories/3/architecture-graph", headers={"Origin":"http://localhost:5173"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("PostgreSQL", response.json()["detail"])
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://localhost:5173")

    def test_database_dependency_failure_is_logged_and_cors_readable(self):
        from fastapi import Depends
        def authenticated_user(db=Depends(get_db)):
            return SimpleNamespace(id=1)
        app.dependency_overrides[current_user] = authenticated_user
        def broken_db():
            raise SQLAlchemyError("database unavailable")
            yield
        app.dependency_overrides[get_db] = broken_db
        with self.assertLogs("app.main", level="ERROR") as logs:
            response = self.client.get("/api/repositories/3/architecture-graph", headers={"Origin":"http://localhost:5173"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("PostgreSQL", response.json()["detail"])
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://localhost:5173")
        self.assertIn("Database request failed", "\n".join(logs.output))

    def test_missing_indexed_source_returns_actionable_422(self):
        app.dependency_overrides[current_user] = lambda: SimpleNamespace(id=1)
        app.dependency_overrides[get_db] = lambda: object()
        repository = SimpleNamespace(id=3, owner="owner", name="repo", status="ready", index_generation="g1", commit_sha="abc")
        with patch.object(routes, "owned_repo", return_value=repository), patch.object(routes, "repository_points", return_value=[]):
            response = self.client.get("/api/repositories/3/architecture-graph", headers={"Origin":"http://localhost:5173"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("No indexed source", response.json()["detail"])
        self.assertEqual(response.headers.get("access-control-allow-origin"), "http://localhost:5173")


if __name__ == "__main__":
    unittest.main()
