# Document Processing Platform

A modern, high-performance, and secure Document Processing Platform backend.

## Current Phase

**Phase 1.5 — Foundation Audit, Hardening & Phase 2 Readiness**

This phase audits, hardens, and prepares the backend foundation for Phase 2 (Authentication & Users):
- Modular FastAPI application architecture with global sanitized exception handling
- Strict Pydantic Settings configuration management with environment-based validation and production protections
- PostgreSQL async database access with SQLAlchemy 2.x and asyncpg
- Robust database connection pooling (`pool_size`, `max_overflow`, `pool_timeout`, `pool_pre_ping`)
- Explicit transaction boundary architecture for predictable state mutations and safe error handling
- Database schema migration workflow with Alembic
- Secret-safe structured JSON logging with automatic redaction of credentials, Bearer tokens, and sensitive query parameters
- Liveness (`/health`) and timeout-shielded Readiness (`/ready`) health checks
- Containerization with Docker and Docker Compose running under a non-root user (`appuser`)
- Quality gates with Pytest, Ruff, and Mypy
- Automated CI pipeline with GitHub Actions

Business domain features (users, authentication, documents, OCR, search, workers) are deferred to subsequent phases.

---

## Technology Stack

- **Language:** Python 3.12+
- **API Framework:** FastAPI
- **ASGI Server:** Uvicorn
- **Database:** PostgreSQL
- **ORM / Query Engine:** SQLAlchemy 2.x (Async)
- **Database Driver:** asyncpg
- **Migrations:** Alembic
- **Testing:** Pytest, pytest-asyncio, HTTPX
- **Linter & Formatter:** Ruff
- **Type Checking:** Mypy (Strict)
- **Containerization:** Docker & Docker Compose
- **Continuous Integration:** GitHub Actions

---

## Project Structure

```text
.
├── .github/
│   └── workflows/
│       └── ci.yml             # GitHub Actions CI workflow
├── alembic/                   # Database migrations configuration & versions
│   ├── versions/              # Migration revision scripts
│   ├── env.py                 # Alembic async environment
│   └── script.py.mako         # Migration script template
├── app/
│   ├── __init__.py
│   ├── main.py                # FastAPI application entrypoint, lifespan & error handlers
│   ├── api/
│   │   ├── __init__.py
│   │   ├── health.py          # Liveness (/health) & Readiness (/ready)
│   │   └── v1/                # Placeholder router for Phase 2+ APIs
│   │       └── __init__.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py          # Pydantic Settings configuration & validation
│   │   └── logging.py         # Structured JSON logging & secret redaction
│   └── db/
│       ├── __init__.py
│       ├── base.py            # SQLAlchemy DeclarativeBase & naming conventions
│       └── session.py         # Async engine, connection pool & session lifecycle
├── tests/
│   ├── __init__.py
│   ├── conftest.py            # Test fixtures & database isolation
│   ├── unit/                  # Unit tests (config, health, logging, main)
│   └── integration/           # Integration tests (database, readiness, migrations)
├── .env.example               # Example environment configuration
├── .gitignore                 # Git ignore rules protecting secrets
├── alembic.ini                # Alembic CLI configuration
├── Dockerfile                 # Production-conscious multi-stage Dockerfile
├── docker-compose.yml         # Compose configuration (API & PostgreSQL)
├── pyproject.toml             # Project metadata, dependencies, tooling configs
└── README.md                  # Project documentation
```

---

## Local Development Setup

### Prerequisites

- Python 3.12+
- Docker and Docker Compose (or local PostgreSQL)

### 1. Clone & Set Up Virtual Environment

```bash
# Create and activate virtual environment
python -m venv .venv

# On Linux/macOS:
source .venv/bin/activate
# On Windows:
.venv\Scripts\activate

# Install dependencies in editable mode with development tools
pip install -e ".[dev]"
```

### 2. Environment Configuration

```bash
# Copy example configuration
cp .env.example .env
```

Adjust `.env` variables if your local database credentials differ.

---

## Docker Setup

### Start Services

Start the API and PostgreSQL containers in detached mode:

```bash
docker compose up -d
```

### View Logs

```bash
docker compose logs -f api
```

### Stop Services

```bash
docker compose down
```

---

## Local Email Verification & Testing (Mailpit)

For local development and testing without paid email providers or external internet connectivity, the project includes [Mailpit](https://github.com/axllent/mailpit).

### 1. Start Mailpit

```bash
docker compose up -d mailpit
```

Mailpit exposes:
- **SMTP Server:** `localhost:1025` (receives outgoing emails from FastAPI)
- **Web UI:** `http://localhost:8025` (browser inbox for previewing verification emails)

### 2. Local Environment Configuration (`.env`)

```env
SMTP_HOST=localhost
SMTP_PORT=1025
SMTP_USE_TLS=false
SMTP_USE_SSL=false
SMTP_FROM_EMAIL=noreply@docplatform.local
SMTP_FROM_NAME="Document Processing Platform"
FRONTEND_BASE_URL=http://localhost:3000
EMAIL_VERIFICATION_TOKEN_EXPIRE_MINUTES=30
```

### 3. End-to-End Verification Walkthrough

1. **Register a test account:**
   ```bash
   curl -X POST http://localhost:8000/api/v1/auth/register \
     -H "Content-Type: application/json" \
     -d '{"email": "alice@example.com", "username": "alice", "password": "SuperSecretPassword123!"}'
   ```
2. **Inspect the verification email:**
   Open `http://localhost:8025` in your browser. Click on the latest message sent to `alice@example.com`.
3. **Extract verification token:**
   The verification link uses a URI fragment to prevent token leakage in server access logs:
   `http://localhost:3000/verify-email#token=<RAW_VERIFICATION_TOKEN>`
4. **Consume verification token:**
   ```bash
   curl -X POST http://localhost:8000/api/v1/auth/verify-email \
     -H "Content-Type: application/json" \
     -d '{"token": "<RAW_VERIFICATION_TOKEN>"}'
   ```
   *Response (200 OK):*
   ```json
   {
     "message": "Email verified successfully",
     "email": "alice@example.com",
     "is_verified": true
   }
   ```
5. **Verify account status:**
   Log in and inspect `GET /api/v1/auth/me` to confirm `"is_verified": true`.
6. **Request a new verification email (Resend):**
   ```bash
   curl -X POST http://localhost:8000/api/v1/auth/resend-verification \
     -H "Authorization: Bearer <ACCESS_TOKEN>"
   ```

### 4. Frontend Integration Contract

Verification tokens are delivered in the URL fragment (`#token=<raw_token>`):
- **Why URL Fragment:** The fragment is never transmitted in HTTP requests to the web server, CDN, or reverse proxy, preventing raw tokens from leaking into access logs or `Referer` headers.
- **Frontend Flow:**
  1. The client router reads the token from `window.location.hash`.
  2. The client immediately removes the token from the browser address bar (`history.replaceState(null, "", window.location.pathname)`).
  3. The client submits the token in the request body to `POST /api/v1/auth/verify-email`.

### 5. Production SMTP Configuration

In production (`APP_ENV=production`), development defaults (`localhost`, port `1025`, `localhost` frontend URLs) are strictly rejected at startup. Required production environment variables:
- `SMTP_HOST`: Fully qualified domain name or IP of production SMTP relay (e.g., `smtp.company.com`).
- `SMTP_PORT`: Port number (e.g., `587` for STARTTLS, `465` for SSL).
- `SMTP_USE_TLS`: Set `true` if using STARTTLS (port 587).
- `SMTP_USE_SSL`: Set `true` if using implicit SSL (port 465). Note: TLS and SSL are mutually exclusive.
- `SMTP_USERNAME` & `SMTP_PASSWORD`: Optional credentials (required together if SMTP authentication is enabled; omitted if using an authenticated internal network relay).
- `SMTP_FROM_EMAIL`: Verified production sender address (e.g., `noreply@platform.example.com`).
- `FRONTEND_BASE_URL`: Public production frontend URL (e.g., `https://app.example.com`).


---

## Database Migrations (Alembic)

Database migrations use Alembic with the async SQLAlchemy engine.

### Running Migrations Locally

```bash
alembic upgrade head
```

### Checking Migration Status

```bash
alembic current
alembic heads
alembic history
```

### Running Migrations via Docker Compose

```bash
# Ensure postgres is running
docker compose up -d postgres

# Run migrations using the API container
docker compose run --rm api alembic upgrade head

# Start API service
docker compose up -d api
```

---

## Health & Readiness Endpoints

The application provides two operational health endpoints:

### 1. Liveness Check: `GET /health`

Determines if the FastAPI application process is alive and responsive. Does not query external dependencies.

**Response (200 OK):**
```json
{
  "status": "ok",
  "app": "Document Processing Platform",
  "env": "development"
}
```

### 2. Readiness Check: `GET /ready`

Verifies that the application can communicate with its required PostgreSQL database dependency. Executes a lightweight `SELECT 1` query shielded with a 3.0-second timeout.

**Success Response (200 OK):**
```json
{
  "status": "ready",
  "database": "connected"
}
```

**Failure Response (503 Service Unavailable):**
```json
{
  "status": "unhealthy",
  "database": "disconnected"
}
```
*Note: Connection errors, timeouts, and internal database traces are safely redacted and never leaked to the client.*

---

## Architecture & Transaction Strategy

### Transaction Boundaries (Phase 1.5 Hardening)
- **Explicit Transactions:** State mutations (such as user creation or document registration in Phase 2) must explicitly execute `await session.commit()`.
- **Accidental Commit Prevention:** If a route handler returns a non-200 HTTP response (such as a 400 validation error) without raising an exception, uncommitted changes are safely rolled back rather than silently committed.
- **Guaranteed Cleanup:** `get_db_session()` automatically rolls back on exceptions and guarantees session closure in all scenarios.

---

## Code Quality & Testing

### Running Tests

Run the full test suite with Pytest:

```bash
pytest -v
```

Run unit tests only:

```bash
pytest tests/unit
```

Run integration tests:

```bash
pytest tests/integration
```

### Linting & Formatting Check

Run Ruff to verify code style and detect common bugs:

```bash
ruff check .
```

To verify code formatting:

```bash
ruff format --check .
```

To automatically format or fix issues:

```bash
ruff format .
ruff check --fix .
```

### Type Checking

Run Mypy in strict mode:

```bash
mypy app tests
```

---

## Security Considerations

- **Secret Handling:** Credentials and secrets are never hardcoded and are loaded strictly from environment variables.
- **Production Guardrails:** `Settings` strictly prohibits `DEBUG=True` and default development database URLs when `APP_ENV=production`.
- **Redaction in Logs:** Custom structured JSON logging sanitizes database passwords, Authorization Bearer tokens, URL query parameters, and sensitive dictionary keys.
- **Error Shielding:** A global exception handler catches unhandled errors and returns standardized JSON (`500 Internal server error`) without leaking stack traces or internal paths.
- **Least Privilege Container:** The Docker image runs as a non-privileged `appuser` (UID 1000).
- **Least Privilege CI:** CI workflows run with minimal required read permissions (`contents: read`).
