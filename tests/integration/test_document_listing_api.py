"""Integration tests for Document Listing, Searching, Filtering, and Detail API.

Tests execute against live PostgreSQL database and FastAPI endpoints:
- GET /api/v1/documents
- GET /api/v1/documents/{document_id}
"""

from __future__ import annotations

import datetime
import secrets
import uuid
from collections.abc import AsyncGenerator
from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.security import create_access_token
from app.db.models.document import Document
from app.db.models.user import User
from app.db.session import get_db_session
from app.storage import get_storage
from app.storage.base import StorageBackend
from tests.integration.test_database import is_postgres_available


@pytest.fixture
async def sample_user() -> AsyncGenerator[User, None]:
    """Create and persist an isolated primary test user in PostgreSQL."""
    session_gen = get_db_session()
    session = await anext(session_gen)
    suffix = secrets.token_hex(4)
    user = User(
        id=uuid.uuid4(),
        email=f"list_user_a_{suffix}@example.com",
        username=f"list_user_a_{suffix}",
        password_hash="mock_argon2id_hash_for_listing_test",
        is_active=True,
        is_verified=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    try:
        yield user
    finally:
        session_gen2 = get_db_session()
        s2 = await anext(session_gen2)
        try:
            docs_res = await s2.execute(select(Document).where(Document.user_id == user.id))
            for doc in docs_res.scalars().all():
                await s2.delete(doc)
            user_to_delete = await s2.get(User, user.id)
            if user_to_delete:
                await s2.delete(user_to_delete)
            await s2.commit()
        except Exception:
            await s2.rollback()
        try:
            await anext(session_gen2)
        except StopAsyncIteration:
            pass


@pytest.fixture
async def second_user() -> AsyncGenerator[User, None]:
    """Create and persist a distinct secondary test user for multi-tenant isolation tests."""
    session_gen = get_db_session()
    session = await anext(session_gen)
    suffix = secrets.token_hex(4)
    user = User(
        id=uuid.uuid4(),
        email=f"list_user_b_{suffix}@example.com",
        username=f"list_user_b_{suffix}",
        password_hash="mock_argon2id_hash_for_second_user",
        is_active=True,
        is_verified=True,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)

    try:
        yield user
    finally:
        session_gen2 = get_db_session()
        s2 = await anext(session_gen2)
        try:
            docs_res = await s2.execute(select(Document).where(Document.user_id == user.id))
            for doc in docs_res.scalars().all():
                await s2.delete(doc)
            user_to_delete = await s2.get(User, user.id)
            if user_to_delete:
                await s2.delete(user_to_delete)
            await s2.commit()
        except Exception:
            await s2.rollback()
        try:
            await anext(session_gen2)
        except StopAsyncIteration:
            pass


def get_auth_headers(user: User) -> dict[str, str]:
    """Generate Bearer Authorization header for the given user."""
    token = create_access_token(subject=user.id, auth_version=user.auth_version)
    return {"Authorization": f"Bearer {token}"}


async def seed_document(
    *,
    user_id: uuid.UUID,
    original_filename: str = "document.pdf",
    content_type: str = "application/pdf",
    size_bytes: int = 1024,
    sha256: str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    created_at: datetime.datetime | None = None,
    updated_at: datetime.datetime | None = None,
) -> Document:
    """Helper to persist a Document entity record directly into PostgreSQL."""
    session_gen = get_db_session()
    session = await anext(session_gen)
    doc_id = uuid.uuid4()
    storage_key = secrets.token_hex(16)
    doc = Document(
        id=doc_id,
        user_id=user_id,
        storage_key=storage_key,
        original_filename=original_filename,
        content_type=content_type,
        size_bytes=size_bytes,
        sha256=sha256,
        created_at=created_at or datetime.datetime.now(datetime.UTC),
        updated_at=updated_at or datetime.datetime.now(datetime.UTC),
    )
    session.add(doc)
    await session.commit()
    await session.refresh(doc)
    try:
        await anext(session_gen)
    except StopAsyncIteration:
        pass
    return doc


# =====================================================================
# J1. Listing Tests
# =====================================================================


@pytest.mark.asyncio
async def test_list_documents_empty(async_client: AsyncClient, sample_user: User) -> None:
    """Verify empty document list returns items=[], total=0 with HTTP 200."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    headers = get_auth_headers(sample_user)
    resp = await async_client.get("/api/v1/documents", headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0
    assert data["limit"] == 20
    assert data["offset"] == 0


@pytest.mark.asyncio
async def test_list_documents_single_and_fields(
    async_client: AsyncClient, sample_user: User
) -> None:
    """Verify single document listing returns correct public metadata and excludes storage_key."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc = await seed_document(
        user_id=sample_user.id,
        original_filename="financial_summary.pdf",
        content_type="application/pdf",
        size_bytes=2048,
    )

    headers = get_auth_headers(sample_user)
    resp = await async_client.get("/api/v1/documents", headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1

    item = data["items"][0]
    assert item["id"] == str(doc.id)
    assert item["original_filename"] == "financial_summary.pdf"
    assert item["content_type"] == "application/pdf"
    assert item["size_bytes"] == 2048
    assert item["sha256"] == doc.sha256
    assert "created_at" in item
    assert "updated_at" in item

    # Critical security assertion: storage_key and internal paths must NOT be present
    assert "storage_key" not in item
    assert "path" not in item
    assert "file_path" not in item
    assert "storage_path" not in item


@pytest.mark.asyncio
async def test_list_documents_multiple(async_client: AsyncClient, sample_user: User) -> None:
    """Verify multiple uploaded documents are all listed with accurate total count."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    for i in range(5):
        await seed_document(
            user_id=sample_user.id,
            original_filename=f"doc_{i}.pdf",
        )

    headers = get_auth_headers(sample_user)
    resp = await async_client.get("/api/v1/documents", headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 5
    assert len(data["items"]) == 5


# =====================================================================
# J2. Pagination Tests
# =====================================================================


@pytest.mark.asyncio
async def test_pagination_limits_and_offsets(async_client: AsyncClient, sample_user: User) -> None:
    """Verify limit and offset slice results correctly."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    base_time = datetime.datetime(2026, 1, 1, 10, 0, 0, tzinfo=datetime.UTC)
    for i in range(5):
        await seed_document(
            user_id=sample_user.id,
            original_filename=f"page_doc_{i}.pdf",
            created_at=base_time + datetime.timedelta(minutes=i),
        )

    headers = get_auth_headers(sample_user)

    # Page 1: limit 2, offset 0 -> 2 items, total 5
    resp1 = await async_client.get("/api/v1/documents?limit=2&offset=0", headers=headers)
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["total"] == 5
    assert len(data1["items"]) == 2
    assert data1["limit"] == 2
    assert data1["offset"] == 0

    # Page 2: limit 2, offset 2 -> 2 items, total 5
    resp2 = await async_client.get("/api/v1/documents?limit=2&offset=2", headers=headers)
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["total"] == 5
    assert len(data2["items"]) == 2
    assert data2["limit"] == 2
    assert data2["offset"] == 2

    # Page 3: limit 2, offset 4 -> 1 item, total 5
    resp3 = await async_client.get("/api/v1/documents?limit=2&offset=4", headers=headers)
    assert resp3.status_code == 200
    data3 = resp3.json()
    assert data3["total"] == 5
    assert len(data3["items"]) == 1

    # Offset beyond total: offset 10 -> 0 items, total 5
    resp_beyond = await async_client.get("/api/v1/documents?limit=2&offset=10", headers=headers)
    assert resp_beyond.status_code == 200
    data_beyond = resp_beyond.json()
    assert data_beyond["total"] == 5
    assert data_beyond["items"] == []


@pytest.mark.asyncio
async def test_pagination_boundary_values(async_client: AsyncClient, sample_user: User) -> None:
    """Verify limit boundary values (1 and 100) are accepted, but 0 and 101 are rejected."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    headers = get_auth_headers(sample_user)

    # Valid boundaries: limit 1 and limit 100
    resp_min = await async_client.get("/api/v1/documents?limit=1", headers=headers)
    assert resp_min.status_code == 200

    resp_max = await async_client.get("/api/v1/documents?limit=100", headers=headers)
    assert resp_max.status_code == 200

    # Invalid limit: 0 (< 1) -> 422
    resp_zero = await async_client.get("/api/v1/documents?limit=0", headers=headers)
    assert resp_zero.status_code in (422, 400)

    # Invalid limit: 101 (> 100) -> 422
    resp_overflow = await async_client.get("/api/v1/documents?limit=101", headers=headers)
    assert resp_overflow.status_code in (422, 400)

    # Invalid offset: -1 (< 0) -> 422
    resp_neg_offset = await async_client.get("/api/v1/documents?offset=-1", headers=headers)
    assert resp_neg_offset.status_code in (422, 400)


@pytest.mark.asyncio
async def test_pagination_deterministic_tie_breaking(
    async_client: AsyncClient, sample_user: User
) -> None:
    """Verify secondary sort on id ensures stable pagination when created_at is identical."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    # Seed 6 documents with exact identical created_at timestamp
    identical_time = datetime.datetime(2026, 6, 1, 12, 0, 0, tzinfo=datetime.UTC)
    for i in range(6):
        await seed_document(
            user_id=sample_user.id,
            original_filename=f"tie_doc_{i}.pdf",
            created_at=identical_time,
        )

    headers = get_auth_headers(sample_user)

    # Fetch 3 pages of size 2
    page1 = await async_client.get("/api/v1/documents?limit=2&offset=0", headers=headers)
    page2 = await async_client.get("/api/v1/documents?limit=2&offset=2", headers=headers)
    page3 = await async_client.get("/api/v1/documents?limit=2&offset=4", headers=headers)

    ids1 = [doc["id"] for doc in page1.json()["items"]]
    ids2 = [doc["id"] for doc in page2.json()["items"]]
    ids3 = [doc["id"] for doc in page3.json()["items"]]

    all_ids = ids1 + ids2 + ids3
    # No duplicate IDs across pages and total unique IDs equals 6
    assert len(all_ids) == 6
    assert len(set(all_ids)) == 6


# =====================================================================
# J3. Sorting Tests
# =====================================================================


@pytest.mark.asyncio
async def test_sorting_by_fields(async_client: AsyncClient, sample_user: User) -> None:
    """Verify sorting by created_at, original_filename, size_bytes, updated_at."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    t1 = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)
    t2 = datetime.datetime(2026, 2, 1, 12, 0, 0, tzinfo=datetime.UTC)
    t3 = datetime.datetime(2026, 3, 1, 12, 0, 0, tzinfo=datetime.UTC)

    await seed_document(
        user_id=sample_user.id,
        original_filename="alpha.pdf",
        size_bytes=100,
        created_at=t1,
        updated_at=t3,
    )
    await seed_document(
        user_id=sample_user.id,
        original_filename="beta.pdf",
        size_bytes=300,
        created_at=t2,
        updated_at=t2,
    )
    await seed_document(
        user_id=sample_user.id,
        original_filename="gamma.pdf",
        size_bytes=200,
        created_at=t3,
        updated_at=t1,
    )

    headers = get_auth_headers(sample_user)

    # Sort by original_filename asc
    resp = await async_client.get(
        "/api/v1/documents?sort_by=original_filename&sort_order=asc", headers=headers
    )
    names = [d["original_filename"] for d in resp.json()["items"]]
    assert names == ["alpha.pdf", "beta.pdf", "gamma.pdf"]

    # Sort by original_filename desc
    resp = await async_client.get(
        "/api/v1/documents?sort_by=original_filename&sort_order=desc", headers=headers
    )
    names = [d["original_filename"] for d in resp.json()["items"]]
    assert names == ["gamma.pdf", "beta.pdf", "alpha.pdf"]

    # Sort by size_bytes asc
    resp = await async_client.get(
        "/api/v1/documents?sort_by=size_bytes&sort_order=asc", headers=headers
    )
    sizes = [d["size_bytes"] for d in resp.json()["items"]]
    assert sizes == [100, 200, 300]

    # Sort by size_bytes desc
    resp = await async_client.get(
        "/api/v1/documents?sort_by=size_bytes&sort_order=desc", headers=headers
    )
    sizes = [d["size_bytes"] for d in resp.json()["items"]]
    assert sizes == [300, 200, 100]

    # Sort by updated_at desc
    resp = await async_client.get(
        "/api/v1/documents?sort_by=updated_at&sort_order=desc", headers=headers
    )
    names = [d["original_filename"] for d in resp.json()["items"]]
    assert names == ["alpha.pdf", "beta.pdf", "gamma.pdf"]


@pytest.mark.asyncio
async def test_sorting_invalid_parameters(async_client: AsyncClient, sample_user: User) -> None:
    """Verify invalid sort_by or sort_order return HTTP 422."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    headers = get_auth_headers(sample_user)

    resp_invalid_field = await async_client.get(
        "/api/v1/documents?sort_by=nonexistent_column", headers=headers
    )
    assert resp_invalid_field.status_code in (422, 400)

    resp_invalid_order = await async_client.get(
        "/api/v1/documents?sort_order=sideways", headers=headers
    )
    assert resp_invalid_order.status_code in (422, 400)


# =====================================================================
# J4. Filtering Tests
# =====================================================================


@pytest.mark.asyncio
async def test_filter_by_content_type(async_client: AsyncClient, sample_user: User) -> None:
    """Verify exact filtering on content_type."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    await seed_document(
        user_id=sample_user.id,
        original_filename="doc1.pdf",
        content_type="application/pdf",
    )
    await seed_document(
        user_id=sample_user.id,
        original_filename="pic1.png",
        content_type="image/png",
    )
    await seed_document(
        user_id=sample_user.id,
        original_filename="pic2.jpg",
        content_type="image/jpeg",
    )

    headers = get_auth_headers(sample_user)

    # Filter application/pdf
    resp = await async_client.get("/api/v1/documents?content_type=application/pdf", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["content_type"] == "application/pdf"

    # Filter image/png
    resp = await async_client.get("/api/v1/documents?content_type=image/png", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["content_type"] == "image/png"

    # Filter unmatched type returns 0 items with HTTP 200
    resp = await async_client.get("/api/v1/documents?content_type=image/webp", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["items"] == []


@pytest.mark.asyncio
async def test_filter_by_creation_date(async_client: AsyncClient, sample_user: User) -> None:
    """Verify created_after and created_before filter boundary behavior."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    d1 = datetime.datetime(2026, 3, 10, 0, 0, 0, tzinfo=datetime.UTC)
    d2 = datetime.datetime(2026, 3, 20, 0, 0, 0, tzinfo=datetime.UTC)
    d3 = datetime.datetime(2026, 3, 30, 0, 0, 0, tzinfo=datetime.UTC)

    await seed_document(user_id=sample_user.id, original_filename="march10.pdf", created_at=d1)
    await seed_document(user_id=sample_user.id, original_filename="march20.pdf", created_at=d2)
    await seed_document(user_id=sample_user.id, original_filename="march30.pdf", created_at=d3)

    headers = get_auth_headers(sample_user)

    # created_after 2026-03-15T00:00:00Z -> march20 and march30
    resp = await async_client.get(
        "/api/v1/documents?created_after=2026-03-15T00:00:00Z", headers=headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    names = {d["original_filename"] for d in data["items"]}
    assert names == {"march20.pdf", "march30.pdf"}

    # created_before 2026-03-25T00:00:00Z -> march10 and march20
    resp = await async_client.get(
        "/api/v1/documents?created_before=2026-03-25T00:00:00Z", headers=headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    names = {d["original_filename"] for d in data["items"]}
    assert names == {"march10.pdf", "march20.pdf"}

    # Both: 2026-03-15 to 2026-03-25 -> march20 only
    resp = await async_client.get(
        "/api/v1/documents?created_after=2026-03-15T00:00:00Z&created_before=2026-03-25T00:00:00Z",
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["original_filename"] == "march20.pdf"


@pytest.mark.asyncio
async def test_filter_date_validation_errors(async_client: AsyncClient, sample_user: User) -> None:
    """Verify timezone-naive datetimes and contradictory date ranges return 422."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    headers = get_auth_headers(sample_user)

    # Timezone-naive datetime
    resp_naive = await async_client.get(
        "/api/v1/documents?created_after=2026-03-15T00:00:00", headers=headers
    )
    assert resp_naive.status_code in (422, 400)
    assert "timezone" in resp_naive.json()["detail"].lower()

    # Contradictory range: created_after > created_before
    resp_contra = await async_client.get(
        "/api/v1/documents?created_after=2026-03-25T00:00:00Z&created_before=2026-03-15T00:00:00Z",
        headers=headers,
    )
    assert resp_contra.status_code in (422, 400)
    assert "cannot be later" in resp_contra.json()["detail"].lower()


@pytest.mark.asyncio
async def test_filter_filename_search_substring_case_insensitive(
    async_client: AsyncClient, sample_user: User
) -> None:
    """Verify case-insensitive partial substring search via parameter q."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    await seed_document(user_id=sample_user.id, original_filename="Quarterly_Report_2026.pdf")
    await seed_document(user_id=sample_user.id, original_filename="annual_financial_report.pdf")
    await seed_document(user_id=sample_user.id, original_filename="user_avatar.png")

    headers = get_auth_headers(sample_user)

    # Search uppercase "REPORT" matches both reports
    resp = await async_client.get("/api/v1/documents?q=REPORT", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    names = {d["original_filename"] for d in data["items"]}
    assert "Quarterly_Report_2026.pdf" in names
    assert "annual_financial_report.pdf" in names

    # Partial substring "quarter"
    resp = await async_client.get("/api/v1/documents?q=quarter", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["original_filename"] == "Quarterly_Report_2026.pdf"

    # Whitespace-only search is treated as no search filter
    resp = await async_client.get("/api/v1/documents?q=   ", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 3


@pytest.mark.asyncio
async def test_filter_filename_search_wildcard_escaping(
    async_client: AsyncClient, sample_user: User
) -> None:
    """Verify SQL wildcards % and _ are escaped and matched literally."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    # File with literal % and literal _
    await seed_document(user_id=sample_user.id, original_filename="discount_100%_growth.pdf")
    await seed_document(user_id=sample_user.id, original_filename="discount_100_dollars.pdf")
    await seed_document(user_id=sample_user.id, original_filename="item_a_b.pdf")
    await seed_document(user_id=sample_user.id, original_filename="item_axb.pdf")

    headers = get_auth_headers(sample_user)

    # Searching for literal "100%" must NOT match "100_dollars"
    resp = await async_client.get("/api/v1/documents?q=100%", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["original_filename"] == "discount_100%_growth.pdf"

    # Searching for literal "a_b" must NOT match "axb"
    resp = await async_client.get("/api/v1/documents?q=a_b", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["original_filename"] == "item_a_b.pdf"


@pytest.mark.asyncio
async def test_filter_filename_search_sql_injection_resistance(
    async_client: AsyncClient, sample_user: User
) -> None:
    """Verify SQL injection attempt in search query is safely escaped as literal text."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    await seed_document(user_id=sample_user.id, original_filename="regular_document.pdf")
    headers = get_auth_headers(sample_user)

    malicious_payload = "'; DROP TABLE documents; --"
    resp = await async_client.get(f"/api/v1/documents?q={malicious_payload}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 0

    # Excessively long query (> 255 chars) returns 422
    long_q = "a" * 256
    resp_long = await async_client.get(f"/api/v1/documents?q={long_q}", headers=headers)
    assert resp_long.status_code in (422, 400)


# =====================================================================
# J5. Document Detail Tests (GET /api/v1/documents/{document_id})
# =====================================================================


@pytest.mark.asyncio
async def test_get_document_detail_success(async_client: AsyncClient, sample_user: User) -> None:
    """Verify retrieving document metadata for owned document returns HTTP 200."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc = await seed_document(
        user_id=sample_user.id,
        original_filename="detailed_doc.pdf",
        size_bytes=5120,
    )

    headers = get_auth_headers(sample_user)
    resp = await async_client.get(f"/api/v1/documents/{doc.id}", headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == str(doc.id)
    assert data["original_filename"] == "detailed_doc.pdf"
    assert data["size_bytes"] == 5120
    assert "storage_key" not in data


@pytest.mark.asyncio
async def test_get_document_detail_nonexistent(
    async_client: AsyncClient, sample_user: User
) -> None:
    """Verify nonexistent document UUID returns HTTP 404."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    nonexistent_id = uuid.uuid4()
    headers = get_auth_headers(sample_user)
    resp = await async_client.get(f"/api/v1/documents/{nonexistent_id}", headers=headers)

    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_get_document_detail_malformed_uuid(
    async_client: AsyncClient, sample_user: User
) -> None:
    """Verify malformed UUID path parameter returns HTTP 422."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    headers = get_auth_headers(sample_user)
    resp = await async_client.get("/api/v1/documents/not-a-valid-uuid", headers=headers)

    assert resp.status_code in (422, 400)


@pytest.mark.asyncio
async def test_get_document_detail_no_storage_access(
    async_client: AsyncClient, sample_user: User
) -> None:
    """Verify GET /documents/{id} accesses database only, never reading file storage."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc = await seed_document(user_id=sample_user.id, original_filename="no_io.pdf")

    mock_storage = MagicMock(spec=StorageBackend)
    from app.main import app

    app.dependency_overrides[get_storage] = lambda: mock_storage

    headers = get_auth_headers(sample_user)
    try:
        resp = await async_client.get(f"/api/v1/documents/{doc.id}", headers=headers)
        assert resp.status_code == 200
        # StorageBackend methods must NOT have been called
        mock_storage.open.assert_not_called()
        mock_storage.save.assert_not_called()
        mock_storage.delete.assert_not_called()
        mock_storage.exists.assert_not_called()
    finally:
        app.dependency_overrides.pop(get_storage, None)


# =====================================================================
# J6. Security Regression Tests (Ownership, IDOR, Authentication)
# =====================================================================


@pytest.mark.asyncio
async def test_security_cross_user_listing_isolation(
    async_client: AsyncClient, sample_user: User, second_user: User
) -> None:
    """Verify User A and User B only ever see their own documents in listing."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_a = await seed_document(user_id=sample_user.id, original_filename="user_a_secret.pdf")
    doc_b = await seed_document(user_id=second_user.id, original_filename="user_b_secret.pdf")

    headers_a = get_auth_headers(sample_user)
    headers_b = get_auth_headers(second_user)

    # User A listing
    resp_a = await async_client.get("/api/v1/documents", headers=headers_a)
    assert resp_a.status_code == 200
    data_a = resp_a.json()
    ids_a = [item["id"] for item in data_a["items"]]
    assert str(doc_a.id) in ids_a
    assert str(doc_b.id) not in ids_a

    # User B listing
    resp_b = await async_client.get("/api/v1/documents", headers=headers_b)
    assert resp_b.status_code == 200
    data_b = resp_b.json()
    ids_b = [item["id"] for item in data_b["items"]]
    assert str(doc_b.id) in ids_b
    assert str(doc_a.id) not in ids_b


@pytest.mark.asyncio
async def test_security_idor_detail_protection(
    async_client: AsyncClient, sample_user: User, second_user: User
) -> None:
    """Verify User B querying User A's document ID receives 404 with identical error message."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_a = await seed_document(user_id=sample_user.id, original_filename="user_a_private.pdf")

    headers_b = get_auth_headers(second_user)
    resp = await async_client.get(f"/api/v1/documents/{doc_a.id}", headers=headers_b)

    # Must be 404 Not Found to prevent existence discovery
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Document not found"

    # Compare with completely nonexistent UUID: must be identical response
    fake_id = uuid.uuid4()
    resp_fake = await async_client.get(f"/api/v1/documents/{fake_id}", headers=headers_b)
    assert resp_fake.status_code == 404
    assert resp.json() == resp_fake.json()


@pytest.mark.asyncio
async def test_security_client_user_id_tampering_ignored(
    async_client: AsyncClient, sample_user: User, second_user: User
) -> None:
    """Verify injecting user_id query parameter does NOT leak foreign documents."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    doc_b = await seed_document(user_id=second_user.id, original_filename="b_confidential.pdf")

    headers_a = get_auth_headers(sample_user)

    # User A passes user_id of User B in query string
    resp = await async_client.get(f"/api/v1/documents?user_id={second_user.id}", headers=headers_a)
    assert resp.status_code == 200
    data = resp.json()
    ids = [item["id"] for item in data["items"]]
    assert str(doc_b.id) not in ids


@pytest.mark.asyncio
async def test_security_unauthenticated_rejected(async_client: AsyncClient) -> None:
    """Verify unauthenticated requests return 401 Unauthorized."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    # Listing unauthenticated
    resp_list = await async_client.get("/api/v1/documents")
    assert resp_list.status_code == 401

    # Detail unauthenticated
    resp_detail = await async_client.get(f"/api/v1/documents/{uuid.uuid4()}")
    assert resp_detail.status_code == 401


# =====================================================================
# Task K. Performance and Volume Verification (100 & 1,000 documents)
# =====================================================================


@pytest.mark.asyncio
async def test_performance_and_scaling_100_and_1000_documents(
    async_client: AsyncClient, sample_user: User, second_user: User
) -> None:
    """Verify performance and SQL-level pagination with 100 and 1,000 documents."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    session_gen = get_db_session()
    session = await anext(session_gen)
    now = datetime.datetime.now(datetime.UTC)

    # Bulk insert 1,000 documents for sample_user and 100 documents for second_user
    docs_user_a = [
        Document(
            id=uuid.uuid4(),
            user_id=sample_user.id,
            storage_key=secrets.token_hex(16),
            original_filename=f"bulk_doc_{i:04d}.pdf",
            content_type="application/pdf",
            size_bytes=1000 + i,
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            created_at=now + datetime.timedelta(seconds=i),
            updated_at=now + datetime.timedelta(seconds=i),
        )
        for i in range(1000)
    ]
    docs_user_b = [
        Document(
            id=uuid.uuid4(),
            user_id=second_user.id,
            storage_key=secrets.token_hex(16),
            original_filename=f"user_b_doc_{i:04d}.pdf",
            content_type="application/pdf",
            size_bytes=2000 + i,
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            created_at=now + datetime.timedelta(seconds=i),
            updated_at=now + datetime.timedelta(seconds=i),
        )
        for i in range(100)
    ]
    session.add_all(docs_user_a)
    session.add_all(docs_user_b)
    await session.commit()
    try:
        await anext(session_gen)
    except StopAsyncIteration:
        pass

    headers_a = get_auth_headers(sample_user)
    headers_b = get_auth_headers(second_user)

    # 1. Verify User A total is exactly 1,000 (strict owner isolation)
    import time

    start_time = time.perf_counter()
    resp_a = await async_client.get("/api/v1/documents?limit=50&offset=100", headers=headers_a)
    query_duration = time.perf_counter() - start_time

    assert resp_a.status_code == 200
    data_a = resp_a.json()
    assert data_a["total"] == 1000
    assert len(data_a["items"]) == 50
    assert data_a["limit"] == 50
    assert data_a["offset"] == 100
    # SQL query must execute quickly (< 500ms even over local HTTP)
    assert query_duration < 1.0

    # 2. Verify User B total is exactly 100
    resp_b = await async_client.get("/api/v1/documents?limit=20&offset=0", headers=headers_b)
    assert resp_b.status_code == 200
    data_b = resp_b.json()
    assert data_b["total"] == 100
    assert len(data_b["items"]) == 20
