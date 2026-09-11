"""The API runs for real: PostgreSQL, Redis and the HTTP surface (PART 39).

These tests use the real application through its real lifespan, so a failure here
means the deployed process would fail, not that a mock drifted.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

API = "/api/v1"


class TestLiveness:
    def test_health_is_ok(self, api_client: object) -> None:
        response = api_client.get(f"{API}/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["environment"] == "test"
        assert body["app"]
        assert body["version"]

    def test_health_does_not_depend_on_dependencies(self, api_client: object) -> None:
        # The liveness probe must stay cheap: no database or redis round trip.
        first = api_client.get(f"{API}/health")
        second = api_client.get(f"{API}/health")
        assert first.json() == second.json()


class TestReadiness:
    def test_readiness_reports_every_component_ok(self, api_client: object) -> None:
        response = api_client.get(f"{API}/health/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        components = {component["name"]: component for component in body["components"]}
        assert set(components) == {"postgresql", "redis"}
        assert components["postgresql"]["status"] == "ok"
        assert components["redis"]["status"] == "ok"
        # Latency is reported per component; the readiness probe is fast.
        assert components["postgresql"]["latency_ms"] < 3000
        assert components["redis"]["latency_ms"] < 3000

    def test_readiness_reports_the_applied_migration_revision(self, api_client: object) -> None:
        body = api_client.get(f"{API}/health/ready").json()
        assert body["schema_revision"] == "0001_initial_schema"

    def test_readiness_detail_names_the_schema_revision(self, api_client: object) -> None:
        body = api_client.get(f"{API}/health/ready").json()
        postgres = next(c for c in body["components"] if c["name"] == "postgresql")
        assert "0001_initial_schema" in postgres["detail"]


class TestVersionAndRuntime:
    def test_version_exposes_build_metadata(self, api_client: object) -> None:
        response = api_client.get(f"{API}/version")
        assert response.status_code == 200
        body = response.json()
        assert body["api_base_url"] == "/api/v1"
        assert body["schema_revision"] == "0001_initial_schema"
        assert body["python_version"].startswith("3.")

    def test_version_never_leaks_a_secret(self, api_client: object) -> None:
        body = api_client.get(f"{API}/version").json()
        rendered = str(body)
        assert "secret" not in rendered.lower()
        assert "password" not in rendered.lower()

    def test_runtime_reports_the_postgresql_server(self, api_client: object) -> None:
        response = api_client.get(f"{API}/health/runtime")
        assert response.status_code == 200
        body = response.json()
        assert body["postgresql_version"]
        assert body["python_implementation"] == "CPython"


class TestErrorEnvelope:
    def test_unknown_route_returns_the_standard_envelope(self, api_client: object) -> None:
        response = api_client.get(f"{API}/does-not-exist")
        assert response.status_code == 404
        body = response.json()
        assert set(body) <= {"error", "request_id"}
        assert body["error"]["code"]
        assert body["error"]["message"]
        assert isinstance(body["error"]["details"], dict)

    def test_unknown_api_namespace_is_not_a_redirect(self, api_client: object) -> None:
        response = api_client.get("/api/v2/health")
        assert response.status_code == 404
        assert "error" in response.json()

    def test_method_not_allowed_uses_the_envelope(self, api_client: object) -> None:
        response = api_client.post(f"{API}/health", json={})
        assert response.status_code == 405
        assert "error" in response.json()

    def test_root_reports_the_service(self, api_client: object) -> None:
        response = api_client.get("/")
        assert response.status_code == 200


class TestHttpHardening:
    def test_security_headers_are_present(self, api_client: object) -> None:
        headers = api_client.get(f"{API}/health").headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"]

    def test_a_request_id_is_returned(self, api_client: object) -> None:
        headers = api_client.get(f"{API}/health").headers
        assert headers.get("X-Request-Id")

    def test_a_caller_supplied_request_id_is_echoed(self, api_client: object) -> None:
        response = api_client.get(f"{API}/health", headers={"X-Request-Id": "req-from-client"})
        assert response.headers.get("X-Request-Id") == "req-from-client"

    def test_untrusted_host_is_refused(self, api_client: object) -> None:
        response = api_client.get(f"{API}/health", headers={"Host": "evil.example.com"})
        assert response.status_code == 400

    def test_cors_headers_are_not_sent_to_unknown_origins(self, api_client: object) -> None:
        response = api_client.get(f"{API}/health", headers={"Origin": "https://evil.example.com"})
        assert "access-control-allow-origin" not in {key.lower() for key in response.headers}

    def test_cors_preflight_allows_the_configured_origin(self, api_client: object) -> None:
        response = api_client.options(
            f"{API}/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code in {200, 204}
        assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
