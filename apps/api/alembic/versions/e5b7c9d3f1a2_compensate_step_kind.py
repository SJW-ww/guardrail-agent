"""补偿步骤:step_kind 放开 COMPENSATE

W6 用负数 seq + `StepKind.COMPENSATE` 标记补偿步骤,但 51adc5dbfca5 建的
`step_kind` CHECK 只认 ('EXECUTE', 'APPROVAL') —— 模型改了、迁移没跟上,
往 agent_step.kind 插 COMPENSATE 会直接违反约束。

注意:集成测试的库是 `Base.metadata.create_all` 建的,约束来自当前模型,
所以这种「模型 vs 迁移」漂移在测试里天然测不出来,只能靠这条迁移补上。
`run_status` 已经含 COMPENSATED,不用动。

Revision ID: e5b7c9d3f1a2
Revises: c48f1a6d9e02
Create Date: 2026-09-13
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e5b7c9d3f1a2"
down_revision: str | None = "c48f1a6d9e02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    # 一条一条执行:asyncpg 走的是 prepared statement,一次只放一条语句
    # (写成一个多语句字符串会在 "cannot insert multiple commands" 上直接失败)。
    op.execute("ALTER TABLE agent_step DROP CONSTRAINT IF EXISTS step_kind")
    op.execute(
        "ALTER TABLE agent_step ADD CONSTRAINT step_kind "
        "CHECK (kind IN ('EXECUTE', 'APPROVAL', 'COMPENSATE'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE agent_step DROP CONSTRAINT IF EXISTS step_kind")
    op.execute(
        "ALTER TABLE agent_step ADD CONSTRAINT step_kind "
        "CHECK (kind IN ('EXECUTE', 'APPROVAL'))"
    )
