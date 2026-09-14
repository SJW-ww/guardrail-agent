"""身份表:principal · login_session

在这之前,「谁在提议、谁在签字」来自请求头 —— 调用方说自己是谁就是谁。
两张表把身份变成数据库事实:口令只存 scrypt 散列,会话只存令牌散列,
且**可撤销**(这是自包含 JWT 给不了的:签出去的令牌收不回来)。

`disabled` 用的是「停用」而不是删除:审计里的 actor 必须一直能查到对应的人。

Revision ID: f1a2b3c4d5e6
Revises: e5b7c9d3f1a2
Create Date: 2026-09-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e5b7c9d3f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "principal",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username", name="uq_principal_username"),
    )

    op.create_table(
        "login_session",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("principal_id", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["principal_id"], ["principal.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_hash"),
    )
    op.create_index("ix_login_session_principal_id", "login_session", ["principal_id"])


def downgrade() -> None:
    op.drop_index("ix_login_session_principal_id", table_name="login_session")
    op.drop_table("login_session")
    op.drop_table("principal")
