"""Integration tests for User, Role, Permission, and associations against live PostgreSQL."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.db.models.permission import Permission
from app.db.models.role import Role
from app.db.models.user import User
from app.db.models.user_role import UserRole
from app.db.session import get_db_session
from tests.integration.test_database import is_postgres_available


@pytest.mark.asyncio
async def test_seeded_roles_and_permissions_exist() -> None:
    """Verify that migration 0003 successfully seeded foundational roles and permissions."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        # Check roles
        roles_result = await session.execute(
            select(Role).options(selectinload(Role.permissions)).order_by(Role.name)
        )
        roles = {r.name: r for r in roles_result.scalars().all()}
        assert "admin" in roles
        assert "user" in roles

        # Verify admin permissions
        admin_perms = {p.name for p in roles["admin"].permissions}
        assert admin_perms == {
            "users:read",
            "users:create",
            "users:update",
            "users:delete",
        }

        # Verify standard user permissions
        user_perms = {p.name for p in roles["user"].permissions}
        assert user_perms == {"users:read"}
    finally:
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_user_role_assignment_and_composite_pk_rejection() -> None:
    """Verify role assignment, eager loading, and duplicate assignment rejection."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    session_gen = get_db_session()
    session = await anext(session_gen)
    unique_suffix = uuid.uuid4().hex[:8]
    test_user = User(
        email=f"tester_{unique_suffix}@example.com",
        username=f"tester_{unique_suffix}",
        password_hash="argon2id$mock_hash_val",
    )
    try:
        session.add(test_user)
        await session.flush()

        # Find 'user' role
        role_result = await session.execute(select(Role).where(Role.name == "user"))
        user_role_entity = role_result.scalar_one()

        # Assign role to user
        assignment = UserRole(user_id=test_user.id, role_id=user_role_entity.id)
        session.add(assignment)
        await session.commit()

        # Query user with eager loaded roles
        loaded_user_result = await session.execute(
            select(User).where(User.id == test_user.id).options(selectinload(User.roles))
        )
        loaded_user = loaded_user_result.scalar_one()
        assert len(loaded_user.roles) == 1
        assert loaded_user.roles[0].name == "user"

        # Attempt duplicate assignment (must violate composite primary key in PostgreSQL)
        session.expunge_all()
        duplicate_assignment = UserRole(user_id=test_user.id, role_id=user_role_entity.id)
        session.add(duplicate_assignment)
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    finally:
        # Cleanup
        try:
            del_result = await session.execute(select(User).where(User.id == test_user.id))
            existing_user = del_result.scalar_one_or_none()
            if existing_user is not None:
                await session.delete(existing_user)
                await session.commit()
        except Exception:
            await session.rollback()

        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_user_deletion_cascades_user_roles_without_deleting_role() -> None:
    """Verify that deleting a User cascades and drops user_roles without deleting the Role."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    session_gen = get_db_session()
    session = await anext(session_gen)
    unique_suffix = uuid.uuid4().hex[:8]
    test_user = User(
        email=f"cascade_{unique_suffix}@example.com",
        username=f"cascade_{unique_suffix}",
        password_hash="argon2id$mock_hash_val",
    )
    try:
        session.add(test_user)
        await session.flush()

        role_result = await session.execute(select(Role).where(Role.name == "user"))
        user_role_entity = role_result.scalar_one()

        assignment = UserRole(user_id=test_user.id, role_id=user_role_entity.id)
        session.add(assignment)
        await session.commit()

        # Verify assignment exists
        ur_check = await session.execute(
            select(UserRole).where(
                UserRole.user_id == test_user.id,
                UserRole.role_id == user_role_entity.id,
            )
        )
        assert ur_check.scalar_one_or_none() is not None

        # Delete user
        await session.delete(test_user)
        await session.commit()

        # Verify user_role entry is deleted via CASCADE
        ur_after = await session.execute(select(UserRole).where(UserRole.user_id == test_user.id))
        assert ur_after.scalar_one_or_none() is None

        # Verify Role still exists
        role_still_exists = await session.execute(
            select(Role).where(Role.id == user_role_entity.id)
        )
        assert role_still_exists.scalar_one_or_none() is not None

    finally:
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass


@pytest.mark.asyncio
async def test_permission_uniqueness_constraint_in_postgres() -> None:
    """Verify that inserting duplicate permission names violates unique constraint."""
    if not await is_postgres_available():
        pytest.skip("PostgreSQL is not reachable.")

    session_gen = get_db_session()
    session = await anext(session_gen)
    duplicate_perm = Permission(
        name="users:read",  # Already seeded
        description="Duplicate attempt",
    )
    session.add(duplicate_perm)
    try:
        with pytest.raises(IntegrityError):
            await session.commit()
    finally:
        await session.rollback()
        try:
            await anext(session_gen)
        except StopAsyncIteration:
            pass
