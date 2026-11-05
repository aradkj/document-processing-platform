"""Unit tests for OpenAPI specification generation, routes, tags, and security schemes."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_application


def test_openapi_schema_generation_and_metadata() -> None:
    """Verify OpenAPI documentation generates cleanly with expected title, version, and tags."""
    app = create_application()
    schema = app.openapi()

    assert schema["openapi"].startswith("3.")
    assert schema["info"]["title"] == "Document Processing Platform"
    assert schema["info"]["version"] == "0.2.0"
    assert "Document Processing Platform API" in schema["info"]["description"]
    assert "contact" in schema["info"]
    assert "license" in schema["info"]

    # Verify tags metadata
    tag_names = {tag["name"] for tag in schema.get("tags", [])}
    assert "Authentication" in tag_names
    assert "Health" in tag_names


def test_openapi_all_routes_registered() -> None:
    """Verify that all Phase 2 authentication and health routes are registered in OpenAPI paths."""
    app = create_application()
    schema = app.openapi()
    paths = schema["paths"]

    expected_routes = [
        ("/health", "get"),
        ("/ready", "get"),
        ("/api/v1/auth/register", "post"),
        ("/api/v1/auth/login", "post"),
        ("/api/v1/auth/refresh", "post"),
        ("/api/v1/auth/logout", "post"),
        ("/api/v1/auth/me", "get"),
        ("/api/v1/auth/me", "patch"),
        ("/api/v1/auth/verify-email", "post"),
        ("/api/v1/auth/resend-verification", "post"),
        ("/api/v1/auth/password-reset/request", "post"),
        ("/api/v1/auth/password-reset/confirm", "post"),
    ]

    for route_path, method in expected_routes:
        assert route_path in paths, f"Route {route_path} missing from OpenAPI specification."
        assert method in paths[route_path], (
            f"Method {method.upper()} for {route_path} missing from OpenAPI specification."
        )


def test_openapi_security_requirements() -> None:
    """Verify that protected routes require HTTPBearer security while public routes do not."""
    app = create_application()
    schema = app.openapi()
    paths = schema["paths"]

    # Components must define HTTPBearer scheme
    security_schemes = schema.get("components", {}).get("securitySchemes", {})
    assert "HTTPBearer" in security_schemes
    assert security_schemes["HTTPBearer"]["type"] == "http"
    assert security_schemes["HTTPBearer"]["scheme"] == "bearer"

    # Protected routes requiring Bearer token
    protected_ops = [
        ("/api/v1/auth/me", "get"),
        ("/api/v1/auth/me", "patch"),
        ("/api/v1/auth/resend-verification", "post"),
    ]
    for route_path, method in protected_ops:
        op = paths[route_path][method]
        assert "security" in op, f"Protected route {method.upper()} {route_path} missing security."
        assert any("HTTPBearer" in s for s in op["security"])

    # Public routes that must NOT require authentication
    public_ops = [
        ("/health", "get"),
        ("/ready", "get"),
        ("/api/v1/auth/register", "post"),
        ("/api/v1/auth/login", "post"),
        ("/api/v1/auth/refresh", "post"),
        ("/api/v1/auth/logout", "post"),
        ("/api/v1/auth/verify-email", "post"),
        ("/api/v1/auth/password-reset/request", "post"),
        ("/api/v1/auth/password-reset/confirm", "post"),
    ]
    for route_path, method in public_ops:
        op = paths[route_path][method]
        assert "security" not in op or len(op["security"]) == 0, (
            f"Public route {method.upper()} {route_path} unexpectedly requires security."
        )


def test_openapi_schemas_never_expose_sensitive_fields() -> None:
    """Verify that response schemas do not contain password hashes, raw tokens, or secrets."""
    app = create_application()
    schema = app.openapi()
    schemas = schema.get("components", {}).get("schemas", {})

    # Critical response schemas to audit
    response_schemas = [
        "UserRegisterResponse",
        "UserProfileResponse",
        "UserMeResponse",
        "TokenResponse",
        "LogoutResponse",
        "VerifyEmailResponse",
        "ResendVerificationResponse",
        "PasswordResetRequestResponse",
        "PasswordResetConfirmResponse",
    ]

    for name in response_schemas:
        assert name in schemas, f"Schema {name} not found in OpenAPI components."
        properties = schemas[name].get("properties", {})
        assert "password_hash" not in properties
        assert "hashed_password" not in properties
        assert "password" not in properties
        assert "secret" not in properties


def test_openapi_docs_endpoints_in_development() -> None:
    """Verify /docs, /redoc, and /openapi.json return 200 in development environment."""
    app = create_application()
    client = TestClient(app)

    docs_resp = client.get("/docs")
    assert docs_resp.status_code == 200

    redoc_resp = client.get("/redoc")
    assert redoc_resp.status_code == 200

    openapi_resp = client.get("/openapi.json")
    assert openapi_resp.status_code == 200
    assert openapi_resp.json()["info"]["title"] == "Document Processing Platform"


def test_openapi_docs_disabled_in_production() -> None:
    """Verify /docs, /redoc, and /openapi.json are disabled in production environment."""
    prod_env = {
        "APP_ENV": "production",
        "DATABASE_URL": "postgresql+asyncpg://prod_user:secret_prod_pass@prod-db.internal:5432/proddb",
        "JWT_SECRET_KEY": "a" * 48,
        "SMTP_HOST": "smtp.production.internal",
        "SMTP_PORT": "587",
        "SMTP_USE_TLS": "true",
        "SMTP_USERNAME": "smtp_prod_user",
        "SMTP_PASSWORD": "smtp_prod_password_12345",
        "SMTP_FROM_EMAIL": "noreply@platform.production.com",
        "FRONTEND_BASE_URL": "https://app.production.com",
    }
    with patch("app.main.get_settings") as mock_get_settings:
        mock_get_settings.return_value = Settings(_env_file=None, **prod_env)
        prod_app = create_application()
        assert prod_app.docs_url is None
        assert prod_app.redoc_url is None
        assert prod_app.openapi_url is None
