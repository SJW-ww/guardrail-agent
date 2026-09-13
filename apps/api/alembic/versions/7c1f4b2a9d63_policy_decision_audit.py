"""audit_log 记录策略裁决

审计只记「谁把什么改成了什么」还不够:复盘时要能回答「当时凭什么允许它写」。
所以把裁决结论(policy_decision)和裁决理由(policy_reason)一并落到审计行上。

历史行留空(NULL)是刻意的:这些写操作发生在策略引擎上线之前,
补一个编造的裁决结论比留空更糟。NULL 的含义是「此行的裁决信息不可考」。

Revision ID: 7c1f4b2a9d63
Revises: 51adc5dbfca5
Create Date: 2026-09-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c1f4b2a9d63"
down_revision: str | None = "51adc5dbfca5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audit_log", sa.Column("policy_decision", sa.String(length=24), nullable=True))
    op.add_column("audit_log", sa.Column("policy_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_log", "policy_reason")
    op.drop_column("audit_log", "policy_decision")
