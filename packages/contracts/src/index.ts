/**
 * 前后端共享契约。
 *
 * 类型不是手写的,而是从 FastAPI 的 OpenAPI 生成(`npm run contracts:export`)。
 * 所以后端一改响应模型,前端 `tsc` 立刻报错 —— 契约漂移在编译期就被拦住,
 * 前端不会出现手写的接口类型。
 */
import type { components } from "./generated/schema";

export type { components, operations, paths } from "./generated/schema";

type Schemas = components["schemas"];

// --- 平台 ---

export type HealthResponse = Schemas["HealthResponse"];
export type ReadyResponse = Schemas["ReadyResponse"];

// --- 订单 ---

export type OrderStatus = Schemas["OrderStatus"];
export type OrderView = Schemas["OrderView"];
export type OrderSummary = Schemas["OrderSummary"];
export type OrderItemView = Schemas["OrderItemView"];
export type OrderListResponse = Schemas["OrderListResponse"];
export type CancelOrderRequest = Schemas["CancelOrderRequest"];

// --- 售后 ---

export type TicketStatus = Schemas["TicketStatus"];
export type TicketSummary = Schemas["TicketSummary"];
export type TicketListResponse = Schemas["TicketListResponse"];
export type RejectTicketRequest = Schemas["RejectTicketRequest"];

// --- 工具与提议 ---

export type ToolDescription = Schemas["ToolDescription"];
export type ToolInvocationResponse = Schemas["ToolInvocationResponse"];
export type Proposal = Schemas["Proposal"];
export type DraftProposalRequest = Schemas["DraftProposalRequest"];

// --- 治理层:执行记录与审计 ---

export type RunStatus = Schemas["RunStatus"];
export type StepStatus = Schemas["StepStatus"];
export type StepKind = Schemas["StepKind"];
export type AuditOutcome = Schemas["AuditOutcome"];
export type RunView = Schemas["RunView"];
export type RunDetail = Schemas["RunDetail"];
export type RunListResponse = Schemas["RunListResponse"];
export type StepView = Schemas["StepView"];
export type ExecuteRunResponse = Schemas["ExecuteResponse"];
export type RetryStepResponse = Schemas["RetryStepResponse"];
export type PlannedStepIn = Schemas["PlannedStepIn"];
export type CreateRunRequest = Schemas["CreateRunRequest"];
export type AuditEntry = Schemas["AuditEntry"];
export type AuditListResponse = Schemas["AuditListResponse"];

// --- 领域常量(不属于 API 契约,前后端共用同一份定义)---

/** L0 只读 → L1 建议 → L2 低风险自动 → L3 审批 → L4 受限自主 */
export type TrustLevel = "L0" | "L1" | "L2" | "L3" | "L4";

export type PolicyDecision = "ALLOW" | "REQUIRE_APPROVAL" | "DENY";

export type RiskLevel = "read_only" | "low" | "high";

export const TRUST_LEVELS: { level: TrustLevel; label: string; executor: string }[] = [
  { level: "L0", label: "只读:Agent 只能查和答", executor: "—" },
  { level: "L1", label: "建议:Agent 出卡片,人点执行", executor: "人" },
  { level: "L2", label: "低风险可逆操作自动执行", executor: "Agent + 审计" },
  { level: "L3", label: "高风险操作:Agent 备好 → 人审批 → 系统执行", executor: "人批,系统落地" },
  { level: "L4", label: "额度 / 白名单内受限自主,超限自动升级", executor: "Agent" },
];

export const ORDER_STATUS_LABELS: Record<OrderStatus, string> = {
  CREATED: "待支付",
  PAID: "已支付",
  SHIPPED: "已发货",
  COMPLETED: "已完成",
  CANCELLED: "已取消",
};

export const TICKET_STATUS_LABELS: Record<TicketStatus, string> = {
  PENDING: "待审批",
  APPROVED: "已批准",
  REJECTED: "已拒绝",
  REFUNDED: "已退款",
  CLOSED: "已关闭",
};

export const REASON_CODE_LABELS: Record<string, string> = {
  QUALITY_ISSUE: "质量问题",
  WRONG_ITEM: "发错货",
  DAMAGED_IN_TRANSIT: "运输破损",
  LATE_DELIVERY: "到货太慢",
  NO_LONGER_NEEDED: "不再需要",
};

export const RISK_LEVEL_LABELS: Record<RiskLevel, string> = {
  read_only: "只读",
  low: "低风险",
  high: "高风险",
};

export const RUN_STATUS_LABELS: Record<RunStatus, string> = {
  PENDING: "待执行",
  RUNNING: "执行中",
  WAITING_APPROVAL: "等审批",
  SUCCEEDED: "已完成",
  FAILED: "失败",
  COMPENSATED: "已补偿",
};

export const STEP_STATUS_LABELS: Record<StepStatus, string> = {
  RUNNING: "执行中",
  SUCCEEDED: "成功",
  FAILED: "失败",
};
