"""双人复核:一步一签 -> 一步多签

大额操作不该由一个人拍板,所以 `approvals` 的值从「一个人」变成「一串签名」:

    {"1": "human:supervisor-01"}  ->  {"1": ["human:supervisor-01", "human:finance-01"]}

历史数据是单人签名,升级时包成单元素数组;降级时只保留第一个签名。
刻意不做"降级即丢弃多人签名"的静默处理 —— 降级本身就不该是常走的路径。

Revision ID: c48f1a6d9e02
Revises: b3d5e8f21a47
Create Date: 2026-09-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c48f1a6d9e02"
down_revision: str | None = "b3d5e8f21a47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UPGRADE = """
UPDATE agent_run
SET approvals = COALESCE(
    (SELECT jsonb_object_agg(key, jsonb_build_array(value)) FROM jsonb_each(approvals)),
    '{}'::jsonb
)
WHERE approvals <> '{}'::jsonb
"""

DOWNGRADE = """
UPDATE agent_run
SET approvals = COALESCE(
    (SELECT jsonb_object_agg(key, value -> 0) FROM jsonb_each(approvals)),
    '{}'::jsonb
)
WHERE approvals <> '{}'::jsonb
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
