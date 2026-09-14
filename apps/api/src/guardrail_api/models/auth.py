"""身份模型(W8)。

治理层整条链路的可信度,最后都压在一个问题上:**「谁」是谁**。

在此之前,执行体来自请求头 `X-Actor` —— 调用方说自己是谁,系统就信它是谁。
那意味着任何人都能以 `human:supervisor-01` 的名义签掉一笔退款,
W4 的「记名审批」「职责分离」就只是纸面规则。这两张表把身份变成数据库里的事实:

- `principal`      一个人:用户名、角色、口令散列。口令永不落明文。
- `login_session`  一次登录的**不透明令牌**(库里只存散列,可撤销)。

刻意**不签 JWT**:自包含令牌一旦签发就撤不回。审计系统必须能立刻踢人下线
(比如某人离职、某台设备丢了),这是「不可撤销的凭据」给不了的。
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from guardrail_api.db import Base
from guardrail_api.models.mixins import TimestampMixin


class Principal(TimestampMixin, Base):
    """一个自然人主体。

    `role` 是组织里的角色(主管 / 财务),它回答的不是「能不能」而是
    「该不该是他」—— 双人复核要求两个**不同角色**签字时才用得上。
    """

    __tablename__ = "principal"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    # 格式见 auth.passwords:scrypt$n$r$p$salt$hash
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # 停用而不是删除:历史审计里的 actor 必须仍然能查到对应的人
    disabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class LoginSession(TimestampMixin, Base):
    """一次登录。

    主键是令牌的 **sha256 散列**,不是令牌本身:库被读走也换不回一个能用的会话。
    过期与撤销分开记 —— `expires_at` 是自动过期,`revoked_at` 是人主动踢下线,
    复盘时这两件事的结论完全不同。
    """

    __tablename__ = "login_session"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    principal_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("principal.id", ondelete="CASCADE"), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
