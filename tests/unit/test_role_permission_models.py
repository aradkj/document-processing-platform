"""Unit tests for Role, Permission, UserRole, and RolePermission models and constraints."""

import uuid

from sqlalchemy import Table, inspect

from app.db.models.permission import Permission
from app.db.models.role import Role
from app.db.models.role_permission import RolePermission
from app.db.models.user import User
from app.db.models.user_role import UserRole


def test_role_model_instantiation_and_defaults() -> None:
    """Verify default values and UUID generation upon Role instantiation."""
    role = Role(name="admin", description="Administrator role")

    assert isinstance(role.id, uuid.UUID)
    assert role.name == "admin"
    assert role.description == "Administrator role"
    assert "<Role" in repr(role)
    assert "admin" in repr(role)


def test_permission_model_instantiation_and_defaults() -> None:
    """Verify default values and UUID generation upon Permission instantiation."""
    perm = Permission(name="users:read", description="Read users permission")

    assert isinstance(perm.id, uuid.UUID)
    assert perm.name == "users:read"
    assert perm.description == "Read users permission"
    assert "<Permission" in repr(perm)
    assert "users:read" in repr(perm)


def test_user_role_instantiation_and_attributes() -> None:
    """Verify UserRole instantiation with composite foreign keys."""
    uid = uuid.uuid4()
    rid = uuid.uuid4()
    user_role = UserRole(user_id=uid, role_id=rid)

    assert user_role.user_id == uid
    assert user_role.role_id == rid
    assert "<UserRole" in repr(user_role)
    assert str(uid) in repr(user_role)
    assert str(rid) in repr(user_role)


def test_role_permission_instantiation_and_attributes() -> None:
    """Verify RolePermission instantiation with composite foreign keys."""
    rid = uuid.uuid4()
    pid = uuid.uuid4()
    role_perm = RolePermission(role_id=rid, permission_id=pid)

    assert role_perm.role_id == rid
    assert role_perm.permission_id == pid
    assert "<RolePermission" in repr(role_perm)
    assert str(rid) in repr(role_perm)
    assert str(pid) in repr(role_perm)


def test_roles_and_permissions_table_metadata() -> None:
    """Verify table structures, primary keys, and unique constraints."""
    # Roles table
    role_table = inspect(Role).persist_selectable
    assert isinstance(role_table, Table)
    assert role_table.name == "roles"
    assert [col.name for col in role_table.primary_key] == ["id"]
    assert bool(role_table.c.name.unique) is True
    assert not role_table.c.name.nullable

    # Permissions table
    perm_table = inspect(Permission).persist_selectable
    assert isinstance(perm_table, Table)
    assert perm_table.name == "permissions"
    assert [col.name for col in perm_table.primary_key] == ["id"]
    assert bool(perm_table.c.name.unique) is True
    assert not perm_table.c.name.nullable

    # UserRoles junction table
    ur_table = inspect(UserRole).persist_selectable
    assert isinstance(ur_table, Table)
    assert ur_table.name == "user_roles"
    assert {col.name for col in ur_table.primary_key} == {"user_id", "role_id"}
    ur_fks = {fk.column.table.name: fk.ondelete for fk in ur_table.foreign_keys}
    assert ur_fks.get("users") == "CASCADE"
    assert ur_fks.get("roles") == "CASCADE"

    # RolePermissions junction table
    rp_table = inspect(RolePermission).persist_selectable
    assert isinstance(rp_table, Table)
    assert rp_table.name == "role_permissions"
    assert {col.name for col in rp_table.primary_key} == {"role_id", "permission_id"}
    rp_fks = {fk.column.table.name: fk.ondelete for fk in rp_table.foreign_keys}
    assert rp_fks.get("roles") == "CASCADE"
    assert rp_fks.get("permissions") == "CASCADE"


def test_relationships_lazy_raise_configuration() -> None:
    """Verify that all cross-entity relationships are configured with lazy='raise'."""
    user_mapper = inspect(User)
    role_mapper = inspect(Role)
    perm_mapper = inspect(Permission)

    # User.roles
    user_roles_rel = user_mapper.relationships["roles"]
    assert user_roles_rel.lazy == "raise"
    assert isinstance(user_roles_rel.secondary, Table)
    assert user_roles_rel.secondary.name == "user_roles"

    # Role.users & Role.permissions
    role_users_rel = role_mapper.relationships["users"]
    assert role_users_rel.lazy == "raise"
    assert isinstance(role_users_rel.secondary, Table)
    assert role_users_rel.secondary.name == "user_roles"

    role_perms_rel = role_mapper.relationships["permissions"]
    assert role_perms_rel.lazy == "raise"
    assert isinstance(role_perms_rel.secondary, Table)
    assert role_perms_rel.secondary.name == "role_permissions"

    # Permission.roles
    perm_roles_rel = perm_mapper.relationships["roles"]
    assert perm_roles_rel.lazy == "raise"
    assert isinstance(perm_roles_rel.secondary, Table)
    assert perm_roles_rel.secondary.name == "role_permissions"
