"""审计表的不可变约束(DDL)。

放在这里而不是只写在迁移里,是为了让**测试建的表**和**迁移建的表**拥有同一套约束。
否则会出现「测试全绿、线上能改」这种最糟的分裂。

迁移文件里有一份冻结的等价 SQL —— 迁移是历史快照,不应该 import 应用代码。
"""

AUDIT_GUARD_UPGRADE: tuple[str, ...] = (
    """
    CREATE OR REPLACE FUNCTION guardrail_audit_log_immutable()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'audit_log 是追加写表,不允许 % 操作', TG_OP
            USING ERRCODE = 'restrict_violation';
    END;
    $$ LANGUAGE plpgsql;
    """,
    """
    CREATE TRIGGER audit_log_append_only
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION guardrail_audit_log_immutable();
    """,
    """
    CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION guardrail_audit_log_immutable();
    """,
)

AUDIT_GUARD_DOWNGRADE: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log",
    "DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log",
    "DROP FUNCTION IF EXISTS guardrail_audit_log_immutable()",
)
