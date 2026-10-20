# Document Processing Platform

A modern, high-performance, and secure Document Processing Platform backend.

## Current Phase

**Phase 1 — Foundation & Architecture**

This phase establishes the production-oriented backend foundation:
- Modular FastAPI application architecture
- Environment-based configuration with Pydantic Settings
- PostgreSQL database access using async SQLAlchemy 2.x and asyncpg
- Database schema migration workflow using Alembic
- Lightweight structured JSON logging with secret sanitization
- Liveness (`/health`) and Readiness (`/ready`) health checks
- Containerization with Docker and Docker Compose
- Quality gates with Pytest, Ruff, and Mypy
- Automated CI pipeline with GitHub Actions

Business domain features (users, documents, OCR, search, workers) are deferred to subsequent phases.

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
│   ├── main.py                # FastAPI application entrypoint & lifespan
│   ├── api/
│   │   ├── __init__.py
│   │   ├── health.py          # Liveness (/health) & Readiness (/ready)
│   │   └── v1/                # Placeholder router for Phase 2+ APIs
│   │       └── __init__.py
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py          # Pydantic Settings configuration
│   │   └── logging.py         # Structured JSON logging & secret redaction
│   └── db/
│       ├── __init__.py
│       ├── base.py            # SQLAlchemy DeclarativeBase
│       └── session.py         # Async engine & session lifecycle
├── tests/
│   ├── __init__.py
│   ├── conftest.py            # Test fixtures & database isolation
│   ├── unit/                  # Unit tests (config, health, logging)
│   └── integration/           # Integration tests (database, readiness)
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

The application provides two health endpoints:

### 1. Liveness Check: `GET /health`

Determines if the FastAPI application process is alive and responsive. Does not check database connectivity.

**Response (200 OK):**
```json
{
  "status": "ok",
  "app": "Document Processing Platform",
  "env": "development"
}
```

### 2. Readiness Check: `GET /ready`

Verifies that the application can communicate with its required PostgreSQL database dependency. Executes a lightweight `SELECT 1` query.

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
*Note: Connection errors and internal database traces are safely redacted and never leaked to the client.*

---

## Code Quality & Testing

### Running Tests

Run the full test suite with Pytest:

```bash
pytest
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

## Security Considerations (Phase 1 Baseline)

- **Secret Handling:** Credentials and secrets are never hardcoded and are loaded strictly from environment variables.
- **Redaction in Logs:** Custom structured JSON logging sanitizes database passwords, authorization tokens, and API keys.
- **Error Shielding:** Internal database errors and stack traces are caught and safely reported to clients without exposing connection strings or system paths.
- **Principle of Least Privilege:** CI workflows run with minimal required read permissions (`contents: read`).
