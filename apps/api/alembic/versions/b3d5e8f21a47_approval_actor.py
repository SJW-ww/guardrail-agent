"""记录审批人

`approved_seqs` 只存了「哪几步被批过」,答不了复盘时最常问的那个问题:**谁批的**。
这一版把它升级成 `approvals = {"1": "supervisor-01"}` —— 步骤到人的对应关系。

历史行的批准人已经不可考,回填成显式的 `unknown(...)` 而不是空串:
「当时没记」和「记下来是空」是两件事,前者要能被看见。

Revision ID: b3d5e8f21a47
Revises: 7c1f4b2a9d63
Create Date: 2026-09-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b3d5e8f21a47"
down_revision: str | None = "7c1f4b2a9d63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BACKFILL = """
UPDATE agent_run
SET approvals = (
    SELECT jsonb_object_agg(seq, 'unknown(本版之前未记录批准人)')
    FROM jsonb_array_elements_text(approved_seqs) AS seq
)
WHERE approved_seqs <> '[]'::jsonb
"""


def upgrade() -> None:
    op.add_column(
        "agent_run",
        sa.Column(
            "approvals",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    # 已有的批准记录不能丢:回填成"批准人不可考",而不是当作没批过
    op.execute(BACKFILL)
    op.drop_column("agent_run", "approved_seqs")


def downgrade() -> None:
    op.add_column(
        "agent_run",
        sa.Column(
            "approved_seqs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.execute(
        """
        UPDATE agent_run
        SET approved_seqs = COALESCE(
            (SELECT jsonb_agg(seq::int ORDER BY seq::int) FROM jsonb_object_keys(approvals) AS seq),
            '[]'::jsonb
        )
        """
    )
    op.drop_column("agent_run", "approvals")
