"""Create roles, permissions, user_roles, and role_permissions tables.

Revision ID: 0003_create_roles_permissions
Revises: 0002_create_users_table
Create Date: 2026-10-31 18:53:00.000000+00:00

"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_create_roles_permissions"
down_revision: str | None = "0002_create_users_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Deterministic namespace for foundational seed entities
SEED_NAMESPACE = uuid.UUID("e7b3017a-5db8-4a9c-9c9e-2c8f6f59c111")


def upgrade() -> None:
    """Create roles and permissions tables with associations and seed defaults."""
    # 1. Roles table
    op.create_table(
        "roles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
        sa.UniqueConstraint("name", name=op.f("uq_roles_name")),
    )

    # 2. Permissions table
    op.create_table(
        "permissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permissions")),
        sa.UniqueConstraint("name", name=op.f("uq_permissions_name")),
    )

    # 3. UserRoles association table (Composite PK already guarantees uniqueness)
    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name=op.f("fk_user_roles_role_id_roles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_roles_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "role_id", name=op.f("pk_user_roles")),
    )

    # 4. RolePermissions association table (Composite PK already guarantees uniqueness)
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.Column("permission_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permissions.id"],
            name=op.f("fk_role_permissions_permission_id_permissions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            name=op.f("fk_role_permissions_role_id_roles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("role_id", "permission_id", name=op.f("pk_role_permissions")),
    )

    # 5. Seed baseline foundational roles (Idempotent via ON CONFLICT DO NOTHING)
    roles_table = sa.table(
        "roles",
        sa.column("id", sa.Uuid),
        sa.column("name", sa.String),
        sa.column("description", sa.String),
    )
    admin_role_id = uuid.uuid5(SEED_NAMESPACE, "role:admin")
    user_role_id = uuid.uuid5(SEED_NAMESPACE, "role:user")

    roles_stmt = (
        pg_insert(roles_table)
        .values(
            [
                {
                    "id": admin_role_id,
                    "name": "admin",
                    "description": "System administrator with comprehensive permissions",
                },
                {
                    "id": user_role_id,
                    "name": "user",
                    "description": "Standard platform user with default self-service permissions",
                },
            ]
        )
        .on_conflict_do_nothing(index_elements=["name"])
    )
    op.execute(roles_stmt)

    # 6. Seed baseline foundational permissions (Idempotent via ON CONFLICT DO NOTHING)
    permissions_table = sa.table(
        "permissions",
        sa.column("id", sa.Uuid),
        sa.column("name", sa.String),
        sa.column("description", sa.String),
    )
    perm_read_id = uuid.uuid5(SEED_NAMESPACE, "permission:users:read")
    perm_create_id = uuid.uuid5(SEED_NAMESPACE, "permission:users:create")
    perm_update_id = uuid.uuid5(SEED_NAMESPACE, "permission:users:update")
    perm_delete_id = uuid.uuid5(SEED_NAMESPACE, "permission:users:delete")

    perms_stmt = (
        pg_insert(permissions_table)
        .values(
            [
                {
                    "id": perm_read_id,
                    "name": "users:read",
                    "description": "Read user profiles and public account metadata",
                },
                {
                    "id": perm_create_id,
                    "name": "users:create",
                    "description": "Register and provision new user accounts",
                },
                {
                    "id": perm_update_id,
                    "name": "users:update",
                    "description": "Modify user profile settings and credentials",
                },
                {
                    "id": perm_delete_id,
                    "name": "users:delete",
                    "description": "Deactivate or purge user accounts",
                },
            ]
        )
        .on_conflict_do_nothing(index_elements=["name"])
    )
    op.execute(perms_stmt)

    # 7. Seed role-permission associations (Admin gets all, User gets users:read)
    role_perms_table = sa.table(
        "role_permissions",
        sa.column("role_id", sa.Uuid),
        sa.column("permission_id", sa.Uuid),
    )
    role_perms_stmt = (
        pg_insert(role_perms_table)
        .values(
            [
                {"role_id": admin_role_id, "permission_id": perm_read_id},
                {"role_id": admin_role_id, "permission_id": perm_create_id},
                {"role_id": admin_role_id, "permission_id": perm_update_id},
                {"role_id": admin_role_id, "permission_id": perm_delete_id},
                {"role_id": user_role_id, "permission_id": perm_read_id},
            ]
        )
        .on_conflict_do_nothing(index_elements=["role_id", "permission_id"])
    )
    op.execute(role_perms_stmt)


def downgrade() -> None:
    """Drop role_permissions, user_roles, permissions, and roles tables."""
    op.drop_table("role_permissions")
    op.drop_table("user_roles")
    op.drop_table("permissions")
    op.drop_table("roles")
